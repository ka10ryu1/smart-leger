"""Smart Ledger - クレジットカード利用明細ベースの個人用家計簿アプリ"""

from __future__ import annotations

import logging
import secrets

from flask import Flask

from .config import Config, load_config
from .logging_setup import setup_logging


def create_app(config: Config | None = None) -> Flask:
    """Flask アプリケーションを組み立てる

    Args:
        config: 設定（None なら .env / 環境変数から読み込む）
    """
    config = config or load_config()
    setup_logging(config)

    app = Flask(
        __name__,
        template_folder='templates',
        static_folder=str(config.static_dir),
        static_url_path='/static',
    )
    app.config['SECRET_KEY'] = config.secret_key or secrets.token_hex(32)
    app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB
    app.config['SMART_LEDGER_CONFIG'] = config

    from .routes import bp, build_services

    app.extensions['smart_ledger'] = build_services(config)
    app.register_blueprint(bp)

    logging.getLogger(__name__).info(
        'app started: excel=%s dropbox=%s jev=%s',
        config.excel_path,
        'enabled' if config.dropbox_path else 'disabled',
        'enabled' if config.typesafe_api_key else 'no api key',
    )
    return app
