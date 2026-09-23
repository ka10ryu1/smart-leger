"""Windows 用ランチャーの配置と参照先を検証するテスト"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('launcher_name', 'script_name'), [('setup.cmd', 'setup.ps1'), ('start.cmd', 'start.ps1')])
def test_windows_launcher_targets_internal_powershell(launcher_name: str, script_name: str) -> None:
    """ルートの cmd だけを入口にし、参照先の PowerShell 実装が存在する

    Args:
        launcher_name: 利用者が実行する cmd の名前
        script_name: cmd から呼び出す内部 PowerShell スクリプトの名前
    """
    launcher = (ROOT / launcher_name).read_text(encoding='ascii')
    script = ROOT / 'scripts' / 'windows' / script_name

    assert f'%~dp0scripts\\windows\\{script_name}' in launcher
    assert script.is_file()
    assert not (ROOT / script_name).exists()

    implementation = script.read_text(encoding='utf-8-sig')
    assert 'Join-Path $PSScriptRoot "..\\.."' in implementation
    assert 'Set-Location -LiteralPath $projectRoot' in implementation


def test_setup_uses_locked_uv_environment() -> None:
    """セットアップが uv.lock に従って Python と依存関係を同期する"""
    setup = (ROOT / 'scripts' / 'windows' / 'setup.ps1').read_text(encoding='utf-8-sig')

    assert 'Get-Command "uv"' in setup
    assert 'sync --locked --python 3.12' in setup
    assert 'Read-Host "uv が見つかりません。winget でインストールしますか？ [Y/n]"' in setup
    assert 'install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements' in setup
    assert 'winget upgrade --id=astral-sh.uv -e' in setup
    assert 'uv self update' in setup
    assert 'pip install' not in setup


def test_start_uses_synced_venv_without_uv_overhead() -> None:
    """通常起動は同期済み仮想環境を直接使い uv の確認処理を挟まない"""
    start = (ROOT / 'scripts' / 'windows' / 'start.ps1').read_text(encoding='utf-8-sig')

    assert 'Join-Path $projectRoot ".venv\\Scripts\\python.exe"' in start
    assert 'uv run' not in start
