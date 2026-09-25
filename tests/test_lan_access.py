"""スマホからの LAN 接続（PIN の照合と総当たり対策・LAN の IP 取得・QR コード・PIN 認証の画面フロー）のテスト"""

from __future__ import annotations

import dataclasses
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from smart_ledger import create_app
from smart_ledger.config import Config
from smart_ledger.routes import common as common_routes
from smart_ledger.services import lan_access
from smart_ledger.services.lan_access import LanAccess, PinResult, lan_ip, qr_svg


class FakeClock:
    """テスト用の手動で進める時計"""

    def __init__(self) -> None:
        """時刻を 1000 秒から始める"""
        self.now = 1000.0

    def __call__(self) -> float:
        """現在時刻を返す"""
        return self.now


def wrong_pin(access: LanAccess) -> str:
    """現在の PIN と異なる 4 桁の PIN を返す

    Args:
        access: 照合対象の LanAccess
    """
    return '0000' if access.pin != '0000' else '1111'


@pytest.fixture
def lan_app(test_config: Config, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """LAN モードのテスト用アプリ（LAN の IP は 192.168.1.10 に固定する）

    Args:
        test_config: 一時ディレクトリを使う設定
        monkeypatch: LAN の IP の取得を差し替える
    """
    monkeypatch.setattr(common_routes, 'lan_ip', lambda: '192.168.1.10')
    app = create_app(dataclasses.replace(test_config, lan=True))
    app.config['TESTING'] = True
    return app


def lan_state(app: Flask) -> LanAccess:
    """アプリの LanAccess を返す

    Args:
        app: LAN モードのアプリ
    """
    lan = app.extensions['smart_ledger'].lan
    if lan is None:
        raise AssertionError('LAN モードではありません')

    return lan


def phone_client(app: Flask, remote_addr: str = '192.168.1.20') -> FlaskClient:
    """LAN 上のスマホを装うテストクライアント

    Args:
        app: テスト対象のアプリ
        remote_addr: 接続元アドレス
    """
    client = app.test_client()
    client.environ_base['REMOTE_ADDR'] = remote_addr
    return client


def phone_request(
    client: FlaskClient, path: str, data: dict[str, str] | None = None, base_url: str = 'http://192.168.1.10:5000'
) -> TestResponse:
    """スマホが PC の LAN の URL へ送るリクエスト（data があれば POST）

    Args:
        client: phone_client のクライアント
        path: パス
        data: POST するフォーム（None なら GET）
        base_url: スマホが開く URL
    """
    if data is None:
        return client.get(path, base_url=base_url)

    return client.post(path, data=data, base_url=base_url, headers={'Origin': base_url})


# ------------------------------------------------------------------ LanAccess
def test_pin_is_four_digits_and_verified() -> None:
    """PIN は 4 桁の数字で、一致すれば OK、違えば WRONG"""
    access = LanAccess()
    assert len(access.pin) == 4
    assert access.pin.isascii() and access.pin.isdigit()
    assert access.verify(wrong_pin(access)) is PinResult.WRONG
    assert access.verify(f' {access.pin} ') is PinResult.OK


@pytest.mark.parametrize('submitted', ['', '１２３４', 'abcd', '12345'])
def test_invalid_pin_input_is_wrong_without_error(submitted: str) -> None:
    """空・全角・英字・桁違いの入力も例外にならず WRONG になる

    Args:
        submitted: 入力された PIN
    """
    access = LanAccess()
    assert access.verify(submitted) is PinResult.WRONG


def test_too_many_failures_regenerate_pin_and_lock() -> None:
    """5 回続けて間違えると PIN を作り直し、一時停止中は正しい PIN でも受け付けない"""
    clock = FakeClock()
    access = LanAccess(clock=clock)
    old_pin = access.pin
    results = [access.verify(wrong_pin(access)) for _ in range(5)]
    assert results == [PinResult.WRONG] * 4 + [PinResult.REGENERATED]
    assert access.locked_seconds() == 30
    new_pin = access.pin
    assert access.verify(new_pin) is PinResult.LOCKED

    clock.now += 30
    assert access.locked_seconds() == 0
    if new_pin != old_pin:
        assert access.verify(old_pin) is PinResult.WRONG

    assert access.verify(new_pin) is PinResult.OK


def test_correct_pin_resets_failures_and_lockout() -> None:
    """正しい PIN で間違いの回数と一時停止の秒数が戻る（散発的な打ち間違いを合算しない）"""
    clock = FakeClock()
    access = LanAccess(clock=clock)
    for _ in range(4):
        access.verify(wrong_pin(access))

    assert access.verify(access.pin) is PinResult.OK
    assert [access.verify(wrong_pin(access)) for _ in range(4)] == [PinResult.WRONG] * 4

    access.verify(wrong_pin(access))  # 5 回目で作り直し（最初の 30 秒）
    clock.now += 30
    for _ in range(5):
        access.verify(wrong_pin(access))

    assert access.locked_seconds() == 60  # 続けて間違えたので倍の 60 秒
    clock.now += 60
    assert access.verify(access.pin) is PinResult.OK
    for _ in range(5):
        access.verify(wrong_pin(access))

    assert access.locked_seconds() == 30  # 成功で最初の 30 秒に戻る


def test_lockout_doubles_up_to_limit() -> None:
    """PIN を作り直すたびに一時停止の秒数を倍にし、上限で止める"""
    clock = FakeClock()
    access = LanAccess(clock=clock, lockout_seconds=30, max_lockout_seconds=100)
    lockouts = []
    for _ in range(4):
        for _ in range(5):
            access.verify(wrong_pin(access))

        lockouts.append(access.locked_seconds())
        clock.now += 1000

    assert lockouts == [30, 60, 100, 100]


def test_session_token_is_per_instance() -> None:
    """認証済みの印は起動（インスタンス）ごとに異なり、別の起動の印や不正な値は認めない"""
    first, second = LanAccess(), LanAccess()
    assert first.is_authenticated(first.session_token) is True
    assert first.is_authenticated(second.session_token) is False
    assert first.is_authenticated(None) is False
    assert first.is_authenticated(123) is False
    assert first.is_authenticated('トークン') is False


def test_lan_ip_returns_route_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDP ソケットの送信元アドレスを返し、ループバックや取得失敗は None にする

    Args:
        monkeypatch: socket.socket を差し替える
    """

    class FakeSocket:
        """getsockname が固定アドレスを返すソケット"""

        address = '192.168.1.10'

        def __init__(self, *args: object) -> None:
            """
            Args:
                args: socket.socket の引数（使わない）
            """

        def __enter__(self) -> FakeSocket:
            """with 文で自身を返す"""
            return self

        def __exit__(self, *args: object) -> None:
            """何もしない

            Args:
                args: 例外情報（使わない）
            """
            return None

        def connect(self, address: tuple[str, int]) -> None:
            """接続しない

            Args:
                address: 宛先
            """
            if FakeSocket.address == 'error':
                raise OSError('network is unreachable')

        def getsockname(self) -> tuple[str, int]:
            """固定の送信元アドレスを返す"""
            return FakeSocket.address, 50000

    monkeypatch.setattr(lan_access.socket, 'socket', FakeSocket)
    assert lan_ip() == '192.168.1.10'

    FakeSocket.address = '127.0.1.1'
    assert lan_ip() is None

    FakeSocket.address = 'error'
    assert lan_ip() is None


def test_qr_svg_is_embeddable_svg() -> None:
    """QR コードは XML 宣言の無い <svg> 要素として返す"""
    svg = qr_svg('http://192.168.1.10:5000/')
    assert svg.startswith('<svg')
    assert svg.rstrip().endswith('</svg>')


# ------------------------------------------------------------------ 画面フロー
def test_localhost_needs_no_pin_and_sees_lan_page(lan_app: Flask) -> None:
    """PC 自身からは PIN なしで開け、「スマホで開く」画面に QR コード・URL・PIN が出る

    Args:
        lan_app: LAN モードのアプリ
    """
    client = lan_app.test_client()
    assert client.get('/').status_code == 200
    html = client.get('/lan').get_data(as_text=True)
    assert '<svg' in html
    assert 'http://192.168.1.10:80/' in html  # テストクライアントの Host は localhost（ポートなし）
    assert lan_state(lan_app).pin in html
    assert 'プライベート ネットワーク' in html
    assert 'スマホで開く</a>' in client.get('/').get_data(as_text=True)


def test_lan_page_uses_request_port(lan_app: Flask) -> None:
    """QR コードの URL には PC のブラウザが開いているポートを使う

    Args:
        lan_app: LAN モードのアプリ
    """
    html = lan_app.test_client().get('/lan', base_url='http://localhost:6123').get_data(as_text=True)
    assert 'http://192.168.1.10:6123/' in html


def test_phone_is_redirected_to_pin_login(lan_app: Flask) -> None:
    """localhost 以外からの未認証のリクエストは元のパスを添えて PIN 入力画面へ回す

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    response = phone_request(phone, '/transactions?q=abc')
    assert response.status_code == 302
    location = urlsplit(response.headers['Location'])
    assert location.path == '/lan/login'
    assert parse_qs(location.query) == {'back': ['/transactions?q=abc']}

    for path in ('/', '/lan', '/health', '/no-such-page', '/rules/add'):  # 404 や 405 になるパスも中身を返さない
        assert phone_request(phone, path).status_code == 302

    assert phone.options('/', base_url='http://192.168.1.10:5000').status_code == 302

    login = phone_request(phone, '/lan/login')
    html = login.get_data(as_text=True)
    assert login.status_code == 200
    assert lan_state(lan_app).pin not in html
    assert 'household.xlsx' not in html  # 認証前の端末に正本のパスを見せない
    assert phone_request(phone, '/static/style.css').status_code == 200


def test_unauthenticated_post_is_not_applied(lan_app: Flask) -> None:
    """未認証の端末からの更新系 POST は正しい Origin 付きでも実行せず、PIN 入力画面へ回す

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    response = phone_request(phone, '/rules/add', {'merchant_pattern': 'X', 'category': 'その他'})
    assert response.status_code == 302
    assert urlsplit(response.headers['Location']).path == '/lan/login'
    assert lan_app.extensions['smart_ledger'].repo.load().merchant_rules == []


@pytest.mark.parametrize(
    ('referer', 'expected'),
    [
        ('http://192.168.1.10:5000/transactions?q=abc', '/transactions?q=abc'),
        ('http://evil.example/transactions', '/'),
        (None, '/'),
    ],
)
def test_unauthenticated_post_returns_to_viewable_page(lan_app: Flask, referer: str | None, expected: str) -> None:
    """未認証の POST の戻り先は POST 専用のパスではなく、同じサイトの送信元の画面（無ければトップ）にする

    Args:
        lan_app: LAN モードのアプリ
        referer: POST の Referer（None なら付けない）
        expected: PIN 入力画面に渡す戻り先
    """
    phone = phone_client(lan_app)
    headers = {'Origin': 'http://192.168.1.10:5000'}
    if referer is not None:
        headers['Referer'] = referer

    data = {'merchant_pattern': 'X', 'category': 'その他'}
    response = phone.post('/rules/add', data=data, base_url='http://192.168.1.10:5000', headers=headers)
    back = parse_qs(urlsplit(response.headers['Location']).query)['back'][0]
    assert back == expected

    login = phone_request(phone, '/lan/login', {'pin': lan_state(lan_app).pin, 'back': back})
    assert login.headers['Location'] == expected
    assert phone_request(phone, expected).status_code == 200


def test_head_request_is_not_counted_as_failure(lan_app: Flask) -> None:
    """HEAD /lan/login（curl -I など）は PIN の間違いとして数えず、何度来ても PIN を作り直さない

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    state = lan_state(lan_app)
    pin = state.pin
    for _ in range(6):
        assert phone.head('/lan/login', base_url='http://192.168.1.10:5000').status_code == 200

    assert state.pin == pin
    assert state.locked_seconds() == 0
    assert [state.verify(wrong_pin(state)) for _ in range(4)] == [PinResult.WRONG] * 4  # 失敗回数は 0 のまま


def test_phone_login_with_correct_pin(lan_app: Flask) -> None:
    """正しい PIN で元の画面へ戻り、以降は PIN なしで開ける。「スマホで開く」画面は認証後も 404

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    wrong = phone_request(phone, '/lan/login', {'pin': wrong_pin(lan_state(lan_app)), 'back': '/'})
    assert wrong.status_code == 401
    assert 'PIN が違います' in wrong.get_data(as_text=True)

    response = phone_request(phone, '/lan/login', {'pin': lan_state(lan_app).pin, 'back': '/transactions'})
    assert response.status_code == 302
    assert response.headers['Location'] == '/transactions'
    assert phone_request(phone, '/lan/login?back=/rules').headers['Location'] == '/rules'  # 認証済みなら入力させない
    cookie = response.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Expires=' in cookie
    assert phone_request(phone, '/').status_code == 200
    assert phone_request(phone, '/transactions').status_code == 200
    assert phone_request(phone, '/lan').status_code == 404
    assert 'スマホで開く</a>' not in phone_request(phone, '/').get_data(as_text=True)


def test_phone_login_rejects_external_back(lan_app: Flask) -> None:
    """ログイン後の戻り先に外部 URL を指定されてもサイト内へ戻す

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    data = {'pin': lan_state(lan_app).pin, 'back': 'https://evil.example/'}
    response = phone_request(phone, '/lan/login', data)
    assert response.status_code == 302
    assert response.headers['Location'].startswith('/')


def test_phone_post_from_other_site_is_rejected(lan_app: Flask) -> None:
    """IP アドレスで開いた画面からの POST は通り、別サイトからの POST は 403 になる

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    phone_request(phone, '/lan/login', {'pin': lan_state(lan_app).pin, 'back': '/'})
    data = {'merchant_pattern': 'X', 'category': 'その他'}
    assert phone_request(phone, '/rules/add', data).status_code == 302
    evil = phone.post(
        '/rules/add', data=data, base_url='http://192.168.1.10:5000', headers={'Origin': 'http://evil.example'}
    )
    assert evil.status_code == 403


def test_brute_force_regenerates_pin_shown_on_pc(lan_app: Flask) -> None:
    """5 回続けて間違えると PIN を作り直し、PC の画面には新しい PIN を、スマホには一時停止を表示する

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    state = lan_state(lan_app)
    statuses = [phone_request(phone, '/lan/login', {'pin': wrong_pin(state)}).status_code for _ in range(5)]
    assert statuses == [401] * 5

    locked = phone_request(phone, '/lan/login', {'pin': state.pin})
    assert locked.status_code == 429
    assert phone_request(phone, '/').status_code == 302

    html = lan_app.test_client().get('/lan').get_data(as_text=True)
    assert state.pin in html
    assert '入力を受け付けません' in html


def test_untrusted_host_is_rejected_in_lan_mode(lan_app: Flask) -> None:
    """Host が localhost・127.0.0.1・LAN の IP 以外（DNS リバインディング）なら 400、Host を localhost にしたスマホは PIN を求める

    Args:
        lan_app: LAN モードのアプリ
    """
    client, phone = lan_app.test_client(), phone_client(lan_app)
    for base_url in ('http://evil.example:5000', 'http://evil@localhost:5000'):
        assert client.get('/lan', base_url=base_url).status_code == 400
        for path in ('/', '/static/style.css', '/no-such-page'):
            assert phone.get(path, base_url=base_url).status_code == 400

    assert client.get('/lan', base_url='http://127.0.0.1:5000').status_code == 200
    assert phone.get('/lan', base_url='http://localhost:5000').status_code == 302


def test_other_session_token_is_not_accepted(lan_app: Flask) -> None:
    """別の起動の認証済みの印を持つセッションは PIN 入力画面へ回す

    Args:
        lan_app: LAN モードのアプリ
    """
    phone = phone_client(lan_app)
    with phone.session_transaction() as session:
        session['lan_auth'] = LanAccess().session_token

    assert phone_request(phone, '/').status_code == 302


def test_lan_disabled_keeps_current_behavior(test_config: Config) -> None:
    """LAN モードでなければ PIN を求めず、「スマホで開く」画面と PIN 入力画面は 404、Host は localhost・127.0.0.1 だけ

    Args:
        test_config: 一時ディレクトリを使う設定（LAN モード無効）
    """
    app = create_app(test_config)
    app.config['TESTING'] = True
    assert app.test_client().get('/', base_url='http://127.0.0.1:5000').status_code == 200
    assert app.test_client().get('/', base_url='http://192.168.1.10:5000').status_code == 400
    assert app.test_client().get('/lan').status_code == 404
    assert app.test_client().get('/lan/login').status_code == 404
    assert 'スマホで開く</a>' not in app.test_client().get('/').get_data(as_text=True)
