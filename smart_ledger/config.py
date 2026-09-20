"""環境変数 / .env からの設定読み込み（API キーは環境変数からのみ取得する）"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    """アプリ全体の設定値"""

    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / 'data'
    excel_path: Path = PROJECT_ROOT / 'data' / 'household.xlsx'
    backup_dir: Path = PROJECT_ROOT / 'data' / 'backup'
    staging_dir: Path = PROJECT_ROOT / 'data' / 'staging'
    log_dir: Path = PROJECT_ROOT / 'logs'
    static_dir: Path = PROJECT_ROOT / 'static'
    dropbox_path: Path | None = None
    typesafe_api_key: str | None = None
    typesafe_model: str = 'jev-latest'
    typesafe_base_url: str = 'https://api.typesafe.ai'
    confidence_threshold: float = 0.85
    backup_generations: int = 20
    secret_key: str | None = None
    port: int = 5000
    debug: bool = False

    def ensure_dirs(self) -> None:
        """data / backup / staging / logs ディレクトリを作成する"""
        for directory in (self.data_dir, self.backup_dir, self.staging_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


def env_path(name: str) -> Path | None:
    """環境変数をパスとして読む（未設定・空なら None）

    Args:
        name: 環境変数名
    """
    value = os.environ.get(name, '').strip().strip('"')
    if not value:
        return None

    return Path(value).expanduser()


def env_float(name: str, default: float) -> float:
    """環境変数を float として読む（変換できなければ既定値を使い警告を出す）

    Args:
        name: 環境変数名
        default: 既定値
    """
    raw = os.environ.get(name, '')
    if not raw.strip():
        return default

    try:
        return float(raw)
    except ValueError:
        logger.warning('invalid float env: name=%s value=%s fallback=%s', name, raw, default)
        return default


def env_int(name: str, default: int) -> int:
    """環境変数を int として読む（変換できなければ既定値を使い警告を出す）

    Args:
        name: 環境変数名
        default: 既定値
    """
    raw = os.environ.get(name, '')
    if not raw.strip():
        return default

    try:
        return int(raw)
    except ValueError:
        logger.warning('invalid int env: name=%s value=%s fallback=%s', name, raw, default)
        return default


def load_config(env_file: Path | None = None) -> Config:
    """.env を読み込んで Config を作る

    Args:
        env_file: 読み込む .env のパス（None ならプロジェクト直下の .env）
    """
    load_dotenv(env_file or PROJECT_ROOT / '.env', override=False)

    excel_path = env_path('SMART_LEDGER_EXCEL_PATH')
    if excel_path is None:
        excel_path = PROJECT_ROOT / 'data' / 'household.xlsx'
    elif not excel_path.is_absolute():
        excel_path = PROJECT_ROOT / excel_path

    data_dir = excel_path.parent
    api_key = os.environ.get('TYPESAFE_API_KEY', '').strip() or None

    return Config(
        data_dir=data_dir,
        excel_path=excel_path,
        backup_dir=data_dir / 'backup',
        staging_dir=data_dir / 'staging',
        dropbox_path=env_path('DROPBOX_SMART_LEDGER_PATH'),
        typesafe_api_key=api_key,
        typesafe_model=os.environ.get('TYPESAFE_MODEL', 'jev-latest').strip() or 'jev-latest',
        typesafe_base_url=os.environ.get('TYPESAFE_BASE_URL', 'https://api.typesafe.ai').rstrip('/'),
        confidence_threshold=env_float('CLASSIFICATION_CONFIDENCE_THRESHOLD', 0.85),
        backup_generations=env_int('BACKUP_GENERATIONS', 20),
        secret_key=os.environ.get('FLASK_SECRET_KEY') or None,
        port=env_int('SMART_LEDGER_PORT', 5000),
        debug=os.environ.get('FLASK_DEBUG', '0').strip() in ('1', 'true', 'True'),
    )
