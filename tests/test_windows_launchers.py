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
