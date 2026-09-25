"""Smart Ledger - クレジットカード利用明細ベースの個人用家計簿アプリ"""

from __future__ import annotations

import logging
import secrets

from flask import Flask

from .config import Config, load_config
from .logging_setup import setup_logging


def create_app(config: Config | None = None, loopback_hosts: tuple[str, ...] = ('localhost', '127.0.0.1')) -> Flask:
    """Flask アプリケーションを組み立てる

    Args:
        config: 設定（None なら .env / 環境変数から読み込む）
        loopback_hosts: PC 自身を指す Host 名（TRUSTED_HOSTS の基本。LAN モードでは起動時の LAN の IP を足す）
    """
    config = config or load_config()
    setup_logging(config)

    app = Flask(__name__, static_folder=str(config.static_dir))
    app.config['SECRET_KEY'] = config.secret_key or secrets.token_hex(32)
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # 別サイトからの POST に LAN モードの認証済みセッションを載せない

    from .routes import bp, build_services

    services = build_services(config)
    # DNS リバインディングで別サイトのページから届いたリクエストを 400 にする（LAN モードでは起動時の LAN の IP も許可する）
    app.config['TRUSTED_HOSTS'] = [
        *loopback_hosts,
        *([services.lan.address] if services.lan and services.lan.address else []),
    ]
    app.extensions['smart_ledger'] = services
    app.register_blueprint(bp)

    logging.getLogger(__name__).info(
        'app started: excel=%s dropbox=%s jev=%s lan=%s',
        config.excel_path,
        'enabled' if config.dropbox_path else 'disabled',
        'enabled' if config.typesafe_api_key else 'no api key',
        'enabled' if config.lan else 'disabled',
    )
    return app
