"""Smart Ledger 起動スクリプト

python app.py                 # http://localhost:5000
python app.py --open-browser  # 起動後にブラウザを開く（start.cmd が使用）

.env の SMART_LEDGER_LAN=1 のときだけ 0.0.0.0 で待ち受け、自宅 LAN のスマホから PIN を入力して開けるようにする
（--open-browser では「スマホで開く」画面を開く。FLASK_DEBUG=1 との併用は起動を拒否する）。
LAN モードでないときの --host は 127.0.0.1 / localhost だけ受け付ける（PIN なしで LAN に公開しないため）。

同じポートで既に Smart Ledger が動いている場合は 2 つ目を起動せず、既存のものをブラウザで開いて終了する
（Windows では同じポートに 2 つのサーバーが同居でき、古い方が応答し続ける事故が起きるため）。
判定は 2 段階で行う。まずソケットの bind でポートが空いているかを確かめ（空いていればそのまま起動）、
bind できないときだけ /health で Smart Ledger かどうかを問い合わせる。空きポートへの接続が「接続拒否」ではなく
タイムアウトになる Windows 環境があり、接続確認だけでは空きポートを使用中と誤判定するため。
bind できず Smart Ledger とも確認できない場合（別のプログラム、応答しないインスタンス、予約済みポート）は
メッセージを出して終了コード 1 で終わる
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

from smart_ledger import create_app
from smart_ledger.config import load_config
from smart_ledger.services.lan_access import lan_ip


def port_is_free(host: str, port: int) -> bool:
    """werkzeug のサーバーがそのアドレスに bind できるか（待ち受け中のサーバーが無いか）を確かめる

    接続を試す方法だと、環境によって空きポートへの接続が「接続拒否」ではなくタイムアウトになり
    使用中と誤判定するため、bind の成否で判断する。ソケットオプションは OS で分ける。
    Windows 以外では werkzeug と同じく SO_REUSEADDR を付ける（停止直後の TIME_WAIT が残っていても werkzeug は
    bind できるので空きと判定する。待ち受け中のソケットがあれば SO_REUSEADDR 付きでも失敗する）。
    Windows では werkzeug と違って付けない（付けると待ち受け中のサーバーがいても bind が成功して
    二重起動を検出できない。Windows は TIME_WAIT だけなら素の bind が成功するので付けなくても再起動できる）

    Args:
        host: bind するアドレス（Windows では 127.0.0.1 と 0.0.0.0 が別アドレス扱いで互いに衝突せず、
            ホストが違うと待ち受け中でも空きと判定されるため、main は両方を確かめる）
        port: ポート番号

    Returns:
        True: 空いている / False: 何かが待ち受け中（または bind 権限が無い）
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if os.name != 'nt':
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            sock.bind((host, port))
    except OSError:
        return False

    return True


def running_instance(url: str, timeout: float = 2.0, app_id: str = 'smart-ledger') -> bool | None:
    """そのポートで応答しているのが Smart Ledger かどうかを /health の app 識別子で確かめる

    Args:
        url: 'http://127.0.0.1:5000' のようなベース URL
        timeout: 接続タイムアウト秒
        app_id: /health が返す app の値（routes.common.health と一致させる）

    Returns:
        True: Smart Ledger が応答した /
        False: 接続は拒否されないが Smart Ledger と確認できない（別のプログラムが応答した、または timeout 内に応答が無い）/
        None: 接続が拒否された（誰も待ち受けていない）。空きポートへの接続がタイムアウトになる環境では
        空きでも False になるので、空きかどうかの判定には port_is_free を先に使う
    """
    try:
        with urllib.request.urlopen(f'{url}/health', timeout=timeout) as response:
            body = response.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError:
        return False  # 応答はあるが /health が無い → 別のプログラム
    except http.client.HTTPException:
        return False  # HTTP として解釈できない応答 → 別のプログラム
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return None  # 接続拒否 → ポートは空き

        return False  # 接続段階のタイムアウトなど → 空きとは判断しない
    except ConnectionRefusedError:
        return None
    except OSError:
        return False  # 接続後の読み取りタイムアウトなど → 何かが待ち受けているので空きとは判断しない

    try:
        payload = json.loads(body)
    except ValueError:
        return False  # JSON でない応答 → 別のプログラム

    return isinstance(payload, dict) and payload.get('app') == app_id


def main(browser_delay_seconds: float = 1.2, loopback_hosts: tuple[str, ...] = ('127.0.0.1', 'localhost')) -> int:
    """引数を解釈して Flask を起動する

    Args:
        browser_delay_seconds: --open-browser 指定時にブラウザを開くまでの待ち秒数
        loopback_hosts: LAN モードでなくても --host に指定できるアドレス（PC 自身からしか届かないもの）

    Returns:
        終了コード（既に起動済みなら 0、ポートを bind できず Smart Ledger とも確認できない、
        LAN モードでデバッグが有効、または LAN モードでないのに --host が PC 自身以外なら 1）
    """
    parser = argparse.ArgumentParser(description='Smart Ledger')
    parser.add_argument('--open-browser', action='store_true', help='起動後にブラウザで開く')
    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='待ち受けるアドレス（127.0.0.1 か localhost。LAN モードでは 0.0.0.0 に固定）',
    )
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()

    config = load_config()
    port = args.port or config.port
    url = f'http://localhost:{port}'
    if config.lan and config.debug:
        # Werkzeug のデバッガーはブラウザからコードを実行できるため、LAN に公開しない
        print('SMART_LEDGER_LAN=1 と FLASK_DEBUG=1 は同時に使えません。.env の FLASK_DEBUG を 0 にしてください。')
        return 1

    if not config.lan and args.host not in loopback_hosts:
        # PIN 認証は LAN モードでしか働かないため、PIN なしで LAN に公開しない
        print(
            f'--host {args.host} で LAN に公開するには、.env に SMART_LEDGER_LAN=1 を設定してください(PIN 認証が有効になります)。'
        )
        return 1

    host = '0.0.0.0' if config.lan else args.host
    open_url = f'{url}/lan' if config.lan else url  # LAN モードでは QR コードと PIN の「スマホで開く」画面を開く

    # debug リローダーの子プロセスは親が同じポートを確認済みなので飛ばす。
    # bind できれば空きポートなので接続確認はせず起動する（接続確認は環境によりタイムアウトで空きを使用中と誤判定するため）。
    # Windows では 0.0.0.0 と 127.0.0.1 が別アドレス扱いで互いに衝突しないため、LAN モードかどうかに関係なく両方確かめる
    # （LAN モードと通常の起動が同じ Excel を別々に保存しないようにする）
    reloader_child = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    if not reloader_child and not all(port_is_free(bind_host, port) for bind_host in ('0.0.0.0', '127.0.0.1')):
        already = running_instance(f'http://127.0.0.1:{port}')  # 0.0.0.0 には接続できない
        if already is True:
            print(f'Smart Ledger は既に起動しています: {url}  (このウィンドウは閉じて構いません)')
            if config.lan:
                print(
                    '起動中の Smart Ledger が LAN モードでない場合は、そのウィンドウを閉じてから起動し直してください。'
                )

            if args.open_browser:
                webbrowser.open(open_url)

            return 0

        if already is None:
            # bind はできないのに接続は拒否される: OS や他のプログラムがポートを予約している、または権限が無い
            print(
                f'ポート {port} を使用できません(予約済み、または権限がありません)。.env の SMART_LEDGER_PORT を変更してください。'
            )
            return 1

        print(
            f'ポート {port} は使用中ですが Smart Ledger の応答を確認できませんでした'
            '(別のプログラム、または応答しない起動中のインスタンス)。'
            '起動中のウィンドウを閉じるか、.env の SMART_LEDGER_PORT を変更してください。'
        )
        return 1

    app = create_app(config)
    if args.open_browser and (not config.debug or reloader_child):
        threading.Timer(browser_delay_seconds, lambda: webbrowser.open(open_url)).start()

    print(f'Smart Ledger: {url}  (終了は Ctrl+C)')
    if config.lan:
        address = lan_ip()
        if address:
            print(f'スマホから: http://{address}:{port}/  (PIN は PC の「スマホで開く」画面 {url}/lan に表示)')
        else:
            print('LAN の IP アドレスを取得できませんでした。Wi-Fi への接続を確認してください。')

    app.run(host=host, port=port, debug=config.debug, use_reloader=config.debug)
    return 0


if __name__ == '__main__':
    sys.exit(main())
