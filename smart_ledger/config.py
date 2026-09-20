"""環境変数 / .env からの設定読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    excel_path: Path = PROJECT_ROOT / "data" / "household.xlsx"
    backup_dir: Path = PROJECT_ROOT / "data" / "backup"
    staging_dir: Path = PROJECT_ROOT / "data" / "staging"
    log_dir: Path = PROJECT_ROOT / "logs"
    static_dir: Path = PROJECT_ROOT / "static"
    dropbox_path: Path | None = None
    typesafe_api_key: str | None = None
    typesafe_model: str = "jev-latest"
    typesafe_base_url: str = "https://api.typesafe.ai"
    confidence_threshold: float = 0.85
    backup_generations: int = 20
    secret_key: str | None = None
    port: int = 5000
    debug: bool = False

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.backup_dir, self.staging_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name, "").strip().strip('"')
    if not value:
        return None
    return Path(value).expanduser()


def load_config(env_file: Path | None = None) -> Config:
    """`.env` を読み込んで Config を作る。API キーは環境変数からのみ取得する。"""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    excel_path = _env_path("SMART_LEDGER_EXCEL_PATH")
    if excel_path is None:
        excel_path = PROJECT_ROOT / "data" / "household.xlsx"
    elif not excel_path.is_absolute():
        excel_path = PROJECT_ROOT / excel_path
    data_dir = excel_path.parent

    threshold_raw = os.environ.get("CLASSIFICATION_CONFIDENCE_THRESHOLD", "0.85")
    try:
        threshold = float(threshold_raw)
    except ValueError:
        threshold = 0.85

    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip() or None

    return Config(
        data_dir=data_dir,
        excel_path=excel_path,
        backup_dir=data_dir / "backup",
        staging_dir=data_dir / "staging",
        dropbox_path=_env_path("DROPBOX_SMART_LEDGER_PATH"),
        typesafe_api_key=api_key,
        typesafe_model=os.environ.get("TYPESAFE_MODEL", "jev-latest").strip() or "jev-latest",
        typesafe_base_url=os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/"),
        confidence_threshold=threshold,
        backup_generations=int(os.environ.get("BACKUP_GENERATIONS", "20") or 20),
        secret_key=os.environ.get("FLASK_SECRET_KEY") or None,
        port=int(os.environ.get("SMART_LEDGER_PORT", "5000") or 5000),
        debug=os.environ.get("FLASK_DEBUG", "0").strip() in ("1", "true", "True"),
    )
