"""Smart Ledger 起動スクリプト

python app.py                 # http://localhost:5000
python app.py --open-browser  # 起動後にブラウザを開く（start.ps1 が使用）
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser

from smart_ledger import create_app
from smart_ledger.config import load_config


def main(browser_delay_seconds: float = 1.2) -> None:
    """引数を解釈して Flask を起動する

    Args:
        browser_delay_seconds: --open-browser 指定時にブラウザを開くまでの待ち秒数
    """
    parser = argparse.ArgumentParser(description='Smart Ledger')
    parser.add_argument('--open-browser', action='store_true', help='起動後にブラウザで開く')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args()

    config = load_config()
    port = args.port or config.port
    app = create_app(config)
    url = f'http://localhost:{port}'

    # debug リローダー使用時は子プロセスでのみブラウザを開く
    if args.open_browser and (not config.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true'):
        threading.Timer(browser_delay_seconds, lambda: webbrowser.open(url)).start()

    print(f'Smart Ledger: {url}  (終了は Ctrl+C)')
    app.run(host=args.host, port=port, debug=config.debug, use_reloader=config.debug)


if __name__ == '__main__':
    main()
