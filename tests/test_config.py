"""環境変数 / .env からの設定読み込みテスト"""

from __future__ import annotations

from pathlib import Path

import pytest

from smart_ledger.config import PROJECT_ROOT, Config, load_config


def clear_config_env(
    monkeypatch: pytest.MonkeyPatch,
    names: tuple[str, ...] = (
        'SMART_LEDGER_EXCEL_PATH',
        'DROPBOX_SMART_LEDGER_PATH',
        'TYPESAFE_API_KEY',
        'TYPESAFE_MODEL',
        'TYPESAFE_BASE_URL',
        'CLASSIFICATION_CONFIDENCE_THRESHOLD',
        'BACKUP_GENERATIONS',
        'FLASK_SECRET_KEY',
        'SMART_LEDGER_PORT',
        'FLASK_DEBUG',
        'SMART_LEDGER_LAN',
        'SMART_LEDGER_LAN_ADDRESS',
    ),
) -> None:
    """設定に使う環境変数をすべて削除する

    Args:
        monkeypatch: 環境変数を復元可能な形で変更する
        names: 削除する環境変数名
    """
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_load_config_uses_defaults_without_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """環境変数と .env が無ければ Config の既定値を使う

    Args:
        tmp_path: 存在しない .env パスの作成先
        monkeypatch: プロセスの環境変数を隔離する
    """
    clear_config_env(monkeypatch)
    config = load_config(tmp_path / 'missing.env')
    assert config.excel_path == Config.excel_path
    assert config.confidence_threshold == Config.confidence_threshold
    assert config.backup_generations == Config.backup_generations
    assert config.port == Config.port
    assert config.debug is False
    assert config.lan is False
    assert config.lan_address is None


def test_load_config_reads_paths_and_numbers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """パス・数値・API 設定を環境変数から読み、正本の親を data_dir にする

    Args:
        tmp_path: 正本と Dropbox のテストパス
        monkeypatch: 環境変数をテスト値に差し替える
    """
    clear_config_env(monkeypatch)
    excel = tmp_path / 'ledger' / 'book.xlsx'
    dropbox = tmp_path / 'Dropbox'
    values = {
        'SMART_LEDGER_EXCEL_PATH': str(excel),
        'DROPBOX_SMART_LEDGER_PATH': str(dropbox),
        'TYPESAFE_API_KEY': 'test-key',
        'TYPESAFE_MODEL': 'test-model',
        'TYPESAFE_BASE_URL': 'https://example.test/',
        'CLASSIFICATION_CONFIDENCE_THRESHOLD': '0.75',
        'BACKUP_GENERATIONS': '7',
        'FLASK_SECRET_KEY': 'secret',
        'SMART_LEDGER_PORT': '6123',
        'FLASK_DEBUG': 'true',
        'SMART_LEDGER_LAN': '1',
        'SMART_LEDGER_LAN_ADDRESS': ' 192.168.1.50 ',
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    config = load_config(tmp_path / 'missing.env')
    assert config.data_dir == excel.parent
    assert config.excel_path == excel
    assert config.backup_dir == excel.parent / 'backup'
    assert config.staging_dir == excel.parent / 'staging'
    assert config.dropbox_path == dropbox
    assert config.typesafe_api_key == 'test-key'
    assert config.typesafe_model == 'test-model'
    assert config.typesafe_base_url == 'https://example.test'
    assert config.confidence_threshold == pytest.approx(0.75)
    assert config.backup_generations == 7
    assert config.secret_key == 'secret'
    assert config.port == 6123
    assert config.debug is True
    assert config.lan is True
    assert config.lan_address == '192.168.1.50'


def test_load_config_resolves_relative_excel_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """相対パスの正本は実行ディレクトリではなくプロジェクト直下に解決する

    Args:
        monkeypatch: 正本パスの環境変数を設定する
        tmp_path: 存在しない .env パスの作成先
    """
    clear_config_env(monkeypatch)
    monkeypatch.setenv('SMART_LEDGER_EXCEL_PATH', 'var/custom.xlsx')
    assert load_config(tmp_path / 'missing.env').excel_path == PROJECT_ROOT / 'var/custom.xlsx'


def test_load_config_invalid_numbers_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """数値に変換できない環境変数は警告を出して既定値に戻す

    Args:
        tmp_path: 存在しない .env パスの作成先
        monkeypatch: 不正な環境変数を設定する
        caplog: 警告ログを確認する
    """
    clear_config_env(monkeypatch)
    monkeypatch.setenv('CLASSIFICATION_CONFIDENCE_THRESHOLD', 'high')
    monkeypatch.setenv('BACKUP_GENERATIONS', 'many')
    monkeypatch.setenv('SMART_LEDGER_PORT', 'http')

    config = load_config(tmp_path / 'missing.env')
    assert config.confidence_threshold == Config.confidence_threshold
    assert config.backup_generations == Config.backup_generations
    assert config.port == Config.port
    assert caplog.text.count('invalid number env') == 3
