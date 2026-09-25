"""スマホからの LAN 接続（「スマホで開く」画面・PIN 入力画面・localhost 以外からのリクエストの PIN 認証）

LAN モード（SMART_LEDGER_LAN=1）のときだけ働く。PC 自身（localhost）からのアクセスは PIN 不要で、
「スマホで開く」画面（QR コードと PIN）は localhost からしか開けない
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from flask import abort, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from ..services.lan_access import PinResult, lan_ip, qr_svg
from .common import bp, safe_back, svc

logger = logging.getLogger(__name__)


def is_local_request(
    loopback_addresses: tuple[str, ...] = ('127.0.0.1', '::1'),
    loopback_hosts: tuple[str, ...] = ('localhost', '127.0.0.1', '[::1]'),
) -> bool:
    """PC 自身からのリクエストか（接続元がループバックで、Host もループバックの名前のとき）

    Host も確かめるのは、DNS リバインディングで別サイトのページから localhost に届いたリクエストを PC 自身の操作と
    みなさないため

    Args:
        loopback_addresses: ループバックとみなす接続元アドレス
        loopback_hosts: ループバックとみなす Host（ポートを除いた部分）
    """
    host = request.host.lower()
    name, separator, port = host.rpartition(':')
    if not separator or not port.isdigit():  # ポートの無い Host（'[::1]' の ':' はポートの区切りではない）
        name = host

    return request.remote_addr in loopback_addresses and name in loopback_hosts


@bp.before_app_request
def require_lan_pin() -> WerkzeugResponse | None:
    """LAN モードで localhost 以外からの未認証のリクエストを PIN 入力画面へ回す"""
    lan = svc().lan
    if lan is None or is_local_request():
        return None

    if request.endpoint in ('ledger.lan_login', 'static'):  # 入力画面の CSS / JS は認証前にも返す
        return None

    if lan.is_authenticated(session.get('lan_auth')):
        return None

    return redirect(url_for('ledger.lan_login', back=login_back()))


def login_back() -> str:
    """PIN 入力後の戻り先を返す

    GET / HEAD ならそのパス（クエリ付き）。POST 専用のパスへ GET で戻ると 405 になるため、それ以外では同じサイトの
    Referer のパス（送信元の画面）、無ければトップへ戻す（送信内容は再送しない）
    """
    if request.method in ('GET', 'HEAD'):
        return request.full_path if request.query_string else request.path

    referrer = urlsplit(request.referrer or '')
    if referrer.netloc == request.host and referrer.path:
        return f'{referrer.path}?{referrer.query}' if referrer.query else referrer.path

    return '/'


@bp.app_context_processor
def inject_lan() -> dict[str, object]:
    """ナビゲーションに「スマホで開く」を出すかどうかを注入する（LAN モードで PC 自身が開いたときだけ）"""
    return {'lan_page_available': svc().lan is not None and is_local_request()}


@bp.route('/lan')
def lan_page() -> str:
    """「スマホで開く」画面（QR コード・PIN・注意事項）。LAN モードで PC 自身が開いたとき以外は 404"""
    lan = svc().lan
    if lan is None or not is_local_request():
        abort(404)

    address = lan_ip()
    port = request.environ.get('SERVER_PORT', '80')  # Host ではなく待ち受けているポートを使う
    url = f'http://{address}:{port}/' if address else None
    return render_template(
        'lan.html',
        lan_url=url,
        qr=qr_svg(url) if url else None,
        pin=lan.pin,
        locked_seconds=lan.locked_seconds(),
    )


@bp.route('/lan/login', methods=['GET', 'POST'])
def lan_login() -> str | WerkzeugResponse | tuple[str, int]:
    """PIN 入力画面（正しい PIN でセッションを認証済みにして元の画面へ戻る）"""
    lan = svc().lan
    if lan is None:
        abort(404)

    if is_local_request():
        return redirect(safe_back())

    if request.method != 'POST':  # Flask が GET に自動で付ける HEAD は PIN の間違いとして数えない
        return render_template('lan_login.html', back=safe_back(), error=None)

    result = lan.verify(request.form.get('pin', ''))
    if result is PinResult.OK:
        session['lan_auth'] = lan.session_token
        session.permanent = True  # スマホのブラウザを閉じてもサーバーを止めるまでは入り直さなくてよい
        logger.info('lan login succeeded: remote=%s', request.remote_addr)
        return redirect(safe_back())

    if result is PinResult.LOCKED:
        message = f'PIN を続けて間違えたため、一時的に入力を受け付けていません。約 {lan.locked_seconds()} 秒後に、PC の「スマホで開く」画面に表示される新しい PIN を入力してください。'
        return render_template('lan_login.html', back=safe_back(), error=message), 429

    if result is PinResult.REGENERATED:
        message = 'PIN が違います。間違いが続いたため PIN を作り直しました。PC の「スマホで開く」画面で新しい PIN を確認してください。'
    else:
        message = 'PIN が違います。PC の「スマホで開く」画面に表示されている 4 桁の PIN を入力してください。'

    return render_template('lan_login.html', back=safe_back(), error=message), 401
