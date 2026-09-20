"""Smart Ledger 起動スクリプト

python app.py                 # http://localhost:5000
python app.py --open-browser  # 起動後にブラウザを開く（start.ps1 が使用）

同じポートで既に Smart Ledger が動いている場合は 2 つ目を起動せず、既存のものをブラウザで開いて終了する
（Windows では同じポートに 2 つのサーバーが同居でき、古い方が応答し続ける事故が起きるため）。
ポートが使用中で Smart Ledger と確認できない場合（別のプログラム、応答しないインスタンス）はメッセージを出して終了コード 1 で終わる
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

from smart_ledger import create_app
from smart_ledger.config import load_config


def running_instance(url: str, timeout: float = 2.0, app_id: str = 'smart-ledger') -> bool | None:
    """そのポートで応答しているのが Smart Ledger かどうかを /health の app 識別子で確かめる

    Args:
        url: 'http://127.0.0.1:5000' のようなベース URL
        timeout: 接続タイムアウト秒
        app_id: /health が返す app の値（routes.health と一致させる）

    Returns:
        True: Smart Ledger が応答した /
        False: ポートは使用中だが Smart Ledger と確認できない（別のプログラム、または timeout 内に応答しない）/
        None: 接続が拒否された（誰も待ち受けていない）
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


def main(browser_delay_seconds: float = 1.2) -> int:
    """引数を解釈して Flask を起動する

    Args:
        browser_delay_seconds: --open-browser 指定時にブラウザを開くまでの待ち秒数

    Returns:
        終了コード（既に起動済みなら 0、ポートが使用中で Smart Ledger と確認できなければ 1）
    """
    parser = argparse.ArgumentParser(description='Smart Ledger')
    parser.add_argument('--open-browser', action='store_true', help='起動後にブラウザで開く')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()

    config = load_config()
    port = args.port or config.port
    url = f'http://localhost:{port}'

    # debug リローダーの子プロセスは親が同じポートを確認済みなので飛ばす
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        probe_host = '127.0.0.1' if args.host in ('', '0.0.0.0') else args.host  # 0.0.0.0 には接続できない
        already = running_instance(f'http://{probe_host}:{port}')
        if already is True:
            print(f'Smart Ledger は既に起動しています: {url}  (このウィンドウは閉じて構いません)')
            if args.open_browser:
                webbrowser.open(url)

            return 0

        if already is False:
            print(
                f'ポート {port} は使用中ですが Smart Ledger の応答を確認できませんでした'
                '(別のプログラム、または応答しない起動中のインスタンス)。'
                '起動中のウィンドウを閉じるか、.env の SMART_LEDGER_PORT を変更してください。'
            )
            return 1

    app = create_app(config)
    if args.open_browser and (not config.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true'):
        threading.Timer(browser_delay_seconds, lambda: webbrowser.open(url)).start()

    print(f'Smart Ledger: {url}  (終了は Ctrl+C)')
    app.run(host=args.host, port=port, debug=config.debug, use_reloader=config.debug)
    return 0


if __name__ == '__main__':
    sys.exit(main())
