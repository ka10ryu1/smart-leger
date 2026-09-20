"""pytest 共通フィクスチャ"""

from __future__ import annotations

from pathlib import Path

import pytest

from smart_ledger.config import Config
from smart_ledger.services.backup import DropboxBackup
from smart_ledger.services.excel_repository import ExcelRepository


@pytest.fixture
def repo(tmp_path: Path) -> ExcelRepository:
    """一時ディレクトリ上の Excel リポジトリ（Dropbox 無効）

    Args:
        tmp_path: pytest の一時ディレクトリ
    """
    return ExcelRepository(tmp_path / 'household.xlsx', backup_dir=tmp_path / 'backup', dropbox=DropboxBackup(None))


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """一時ディレクトリを使うテスト用 Config（API キー・Dropbox 無し）

    Args:
        tmp_path: pytest の一時ディレクトリ
    """
    data_dir = tmp_path / 'data'
    return Config(
        data_dir=data_dir,
        excel_path=data_dir / 'household.xlsx',
        backup_dir=data_dir / 'backup',
        staging_dir=data_dir / 'staging',
        log_dir=tmp_path / 'logs',
        dropbox_path=None,
        typesafe_api_key=None,
        confidence_threshold=0.85,
        secret_key='test',
    )
