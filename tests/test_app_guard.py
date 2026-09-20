"""app.py の二重起動ガード（bind による空き判定と /health による起動済み判定）のテスト"""

from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Iterator

import pytest

from app import port_is_free, running_instance


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
    """サーバーが先に close して自分側に TIME_WAIT が残ったポートは空きと判定する（Ctrl+C 直後の再起動）"""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(('127.0.0.1', port))
    server_side, _ = listener.accept()
    server_side.close()  # サーバーが先に FIN を送る → サーバー側が TIME_WAIT になる
    client.recv(1)
    client.close()
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
