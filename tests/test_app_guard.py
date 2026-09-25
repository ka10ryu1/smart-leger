"""app.py の二重起動ガード（bind による空き判定と /health による起動済み判定）のテスト"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from typing import Callable, Iterator

import pytest

import app as app_module
from app import main, port_is_free, running_instance


def free_port() -> int:
    """空いている TCP ポート番号を返す"""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture
def http_server(request: pytest.FixtureRequest) -> Iterator[tuple[str, int]]:
    """/health に固定レスポンスを返すローカル HTTP サーバー

    Args:
        request: `param` に (HTTP ステータス, ボディ) を受け取る

    Returns:
        (ベース URL, ポート)
    """
    status, body = request.param

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # メソッド名は http.server の規約
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body.encode('utf-8'))

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    yield f'http://127.0.0.1:{port}', port
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize(
    'http_server', [(200, json.dumps({'status': 'ok', 'app': 'smart-ledger', 'time': 'x'}))], indirect=True
)
def test_running_instance_detects_smart_ledger(http_server: tuple[str, int]) -> None:
    """/health が app=smart-ledger を返せば起動済みと判定する

    Args:
        http_server: ローカル HTTP サーバー
    """
    assert running_instance(http_server[0]) is True


@pytest.mark.parametrize(
    'http_server',
    [(404, 'not found'), (200, '<html>other app</html>'), (200, json.dumps({'status': 'ok'}))],
    indirect=True,
)
def test_running_instance_detects_other_program(http_server: tuple[str, int]) -> None:
    """404、JSON でない応答、app 識別子の無い JSON は別プログラムと判定する

    Args:
        http_server: ローカル HTTP サーバー
    """
    assert running_instance(http_server[0]) is False


def test_port_is_free_when_nobody_listens() -> None:
    """誰も待ち受けていないポートは空きと判定する"""
    assert port_is_free('127.0.0.1', free_port()) is True


def test_port_is_free_false_when_listener_exists() -> None:
    """待ち受け中のポートは空きと判定しない（SO_REUSEADDR 付きの werkzeug 相当のリスナーでも同じ。
    Windows では 127.0.0.1 と 0.0.0.0 が別アドレス扱いなので、サーバーと同じホストで確かめる）"""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert port_is_free('127.0.0.1', port) is False
    finally:
        listener.close()


def test_port_is_free_after_server_closed_with_time_wait() -> None:
    """サーバーが先に close してサーバー側に TIME_WAIT が残ったポートは空きと判定する（Ctrl+C 直後の再起動）"""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(('127.0.0.1', port))
    server_side, _ = listener.accept()
    server_side.close()  # サーバーが先に FIN を送る
    client.recv(1)  # FIN を受け取るまで待つ
    client.close()  # クライアントも閉じた時点でサーバー側が TIME_WAIT になる
    listener.close()
    assert port_is_free('127.0.0.1', port) is True


@pytest.mark.skipif(
    os.name == 'nt', reason='Windows では空きポートへの接続が接続拒否でなくタイムアウトになる環境がある'
)
def test_running_instance_none_when_port_closed() -> None:
    """誰も待ち受けていないポート（接続拒否）は None（起動してよい）"""
    assert running_instance(f'http://127.0.0.1:{free_port()}', timeout=0.5) is None


def test_running_instance_false_for_non_http_listener() -> None:
    """HTTP を話さないプログラムが待ち受けていても例外にならず、別プログラムと判定する"""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve_garbage() -> None:
        conn, _ = listener.accept()
        conn.sendall(b'NOT HTTP AT ALL\r\n')
        conn.close()

    thread = threading.Thread(target=serve_garbage, daemon=True)
    thread.start()
    try:
        assert running_instance(f'http://127.0.0.1:{port}', timeout=2.0) is False
    finally:
        listener.close()


def test_running_instance_false_when_listener_is_silent() -> None:
    """接続は受け付けるが応答しないリスナー（応答の遅い既存インスタンスなど）は空きとみなさない"""
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    connections: list[socket.socket] = []

    def hold_connection() -> None:
        conn, _ = listener.accept()
        connections.append(conn)  # 何も返さず接続を保持する

    thread = threading.Thread(target=hold_connection, daemon=True)
    thread.start()
    try:
        assert running_instance(f'http://127.0.0.1:{port}', timeout=0.5) is False
    finally:
        for conn in connections:
            conn.close()

        listener.close()


def test_main_runs_app_when_port_is_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """空きポートなら起動済み確認を行わず、CLI の host / port で Flask を起動する

    Args:
        monkeypatch: 起動依存をテスト用に差し替える
    """
    calls: dict[str, object] = {}

    class FakeApp:
        """app.run の呼び出しを記録するテスト用アプリ"""

        def run(self, **kwargs: object) -> None:
            """Flask を起動せず引数だけ記録する"""
            calls['run'] = kwargs

    config = SimpleNamespace(port=5000, debug=False, lan=False)
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py', '--host', 'localhost', '--port', '6123'])
    monkeypatch.setattr(app_module, 'load_config', lambda: config)
    monkeypatch.setattr(
        app_module, 'port_is_free', lambda host, port: host in ('0.0.0.0', '127.0.0.1') and port == 6123
    )
    monkeypatch.setattr(app_module, 'running_instance', lambda url: pytest.fail(f'不要な接続確認: {url}'))
    monkeypatch.setattr(app_module, 'create_app', lambda received: FakeApp())

    assert main() == 0
    assert calls['run'] == {'host': 'localhost', 'port': 6123, 'debug': False, 'use_reloader': False}


@pytest.mark.parametrize(('lan', 'expected'), [(False, 'http://localhost:6123'), (True, 'http://localhost:6123/lan')])
def test_main_opens_existing_instance_without_creating_app(
    lan: bool, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """既存の Smart Ledger が応答するときはブラウザを開き（LAN モードなら「スマホで開く」画面）、2 つ目を起動しない

    Args:
        lan: LAN モードかどうか
        expected: ブラウザで開く URL
        monkeypatch: 起動依存をテスト用に差し替える
    """
    opened: list[str] = []
    config = SimpleNamespace(port=5000, debug=False, lan=lan)
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py', '--open-browser', '--port', '6123'])
    monkeypatch.setattr(app_module, 'load_config', lambda: config)
    monkeypatch.setattr(app_module, 'port_is_free', lambda host, port: False)
    monkeypatch.setattr(app_module, 'running_instance', lambda url: url == 'http://127.0.0.1:6123')
    monkeypatch.setattr(app_module.webbrowser, 'open', opened.append)
    monkeypatch.setattr(app_module, 'create_app', lambda received: pytest.fail('2 つ目のアプリを作成した'))

    assert main() == 0
    assert opened == [expected]


@pytest.mark.parametrize('probe_result', [False, None])
def test_main_rejects_unusable_port(probe_result: bool | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Smart Ledger と確認できない使用中・予約済みポートでは終了コード1にする

    Args:
        probe_result: running_instance の戻り値
        monkeypatch: 起動依存をテスト用に差し替える
    """
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py'])
    monkeypatch.setattr(app_module, 'load_config', lambda: SimpleNamespace(port=5000, debug=False, lan=False))
    monkeypatch.setattr(app_module, 'port_is_free', lambda host, port: False)
    monkeypatch.setattr(app_module, 'running_instance', lambda url: probe_result)
    monkeypatch.setattr(app_module, 'create_app', lambda received: pytest.fail('アプリを作成した'))

    assert main() == 1


def test_main_reloader_child_skips_port_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Werkzeug リローダーの子プロセスはポート確認を繰り返さない

    Args:
        monkeypatch: 起動依存をテスト用に差し替える
    """
    calls: list[dict[str, object]] = []

    class FakeApp:
        """app.run の呼び出しを記録するテスト用アプリ"""

        def run(self, **kwargs: object) -> None:
            """Flask を起動せず引数だけ記録する"""
            calls.append(kwargs)

    config = SimpleNamespace(port=5000, debug=True, lan=False)
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    monkeypatch.setattr(sys, 'argv', ['app.py'])
    monkeypatch.setattr(app_module, 'load_config', lambda: config)
    monkeypatch.setattr(app_module, 'port_is_free', lambda host, port: pytest.fail('ポート確認を行った'))
    monkeypatch.setattr(app_module, 'create_app', lambda received: FakeApp())

    assert main() == 0
    assert calls == [{'host': '127.0.0.1', 'port': 5000, 'debug': True, 'use_reloader': True}]


@pytest.mark.parametrize(('lan', 'free_host'), [(True, '0.0.0.0'), (False, '127.0.0.1')])
def test_main_detects_instance_on_other_address(lan: bool, free_host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.0.0.0 と 127.0.0.1 の両方の待ち受けを確かめる（Windows では別アドレス扱いで衝突しないため、
    通常の起動中に LAN モードで起動する場合も、その逆も検出する）

    Args:
        lan: LAN モードかどうか（True なら 0.0.0.0、False なら 127.0.0.1 で起動しようとする）
        free_host: bind できるアドレス（もう一方は既存のインスタンスが待ち受け中）
        monkeypatch: 起動依存をテスト用に差し替える
    """
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py', '--port', '6123'])
    monkeypatch.setattr(app_module, 'load_config', lambda: SimpleNamespace(port=5000, debug=False, lan=lan))
    monkeypatch.setattr(app_module, 'port_is_free', lambda host, port: host == free_host)
    monkeypatch.setattr(app_module, 'running_instance', lambda url: url == 'http://127.0.0.1:6123')
    monkeypatch.setattr(app_module, 'create_app', lambda received: pytest.fail('2 つ目のアプリを作成した'))

    assert main() == 0


def test_main_lan_mode_binds_all_addresses_and_opens_lan_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """LAN モードでは --host に関係なく 0.0.0.0 で待ち受け、LAN の URL を出して「スマホで開く」画面を開く

    Args:
        monkeypatch: 起動依存をテスト用に差し替える
        capsys: コンソール出力を確認する
    """
    calls: dict[str, object] = {}
    opened: list[str] = []

    class FakeApp:
        """app.run の呼び出しを記録するテスト用アプリ"""

        def run(self, **kwargs: object) -> None:
            """Flask を起動せず引数だけ記録する"""
            calls['run'] = kwargs

    class ImmediateTimer:
        """threading.Timer の代わりに start で即座に呼び出す"""

        def __init__(self, delay: float, function: Callable[[], object]) -> None:
            """
            Args:
                delay: 待ち秒数（使わない）
                function: 呼び出す関数
            """
            self.function = function

        def start(self) -> None:
            """関数を呼び出す"""
            self.function()

    config = SimpleNamespace(port=5000, debug=False, lan=True)
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py', '--open-browser', '--port', '6123'])
    monkeypatch.setattr(app_module, 'load_config', lambda: config)
    monkeypatch.setattr(app_module, 'port_is_free', lambda host, port: True)
    monkeypatch.setattr(app_module, 'lan_ip', lambda: '192.168.1.10')
    monkeypatch.setattr(app_module.threading, 'Timer', ImmediateTimer)
    monkeypatch.setattr(app_module.webbrowser, 'open', opened.append)
    monkeypatch.setattr(app_module, 'create_app', lambda received: FakeApp())

    assert main() == 0
    assert calls['run'] == {'host': '0.0.0.0', 'port': 6123, 'debug': False, 'use_reloader': False}
    assert opened == ['http://localhost:6123/lan']
    assert 'http://192.168.1.10:6123/' in capsys.readouterr().out


def test_main_rejects_lan_mode_with_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """LAN モードで FLASK_DEBUG=1 のときは Werkzeug のデバッガーを公開しないよう起動を拒否する

    Args:
        monkeypatch: 起動依存をテスト用に差し替える
    """
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py'])
    monkeypatch.setattr(app_module, 'load_config', lambda: SimpleNamespace(port=5000, debug=True, lan=True))
    monkeypatch.setattr(app_module, 'create_app', lambda received: pytest.fail('アプリを作成した'))

    assert main() == 1


@pytest.mark.parametrize('host', ['0.0.0.0', '192.168.1.10', ''])
def test_main_rejects_lan_host_without_lan_mode(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """LAN モードでないのに --host で PC 自身以外のアドレスを指定したら、PIN なしで公開しないよう起動を拒否する

    Args:
        host: --host の値
        monkeypatch: 起動依存をテスト用に差し替える
    """
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.setattr(sys, 'argv', ['app.py', '--host', host])
    monkeypatch.setattr(app_module, 'load_config', lambda: SimpleNamespace(port=5000, debug=False, lan=False))
    monkeypatch.setattr(app_module, 'port_is_free', lambda host, port: pytest.fail('ポート確認を行った'))
    monkeypatch.setattr(app_module, 'create_app', lambda received: pytest.fail('アプリを作成した'))

    assert main() == 1
