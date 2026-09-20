"""ブロック終了後の空行挿入ツールのテスト"""

from __future__ import annotations

from pathlib import Path

from tools.insert_block_blank_lines import insert_block_blank_lines, main


def test_inserts_blank_line_after_block_but_not_before_else() -> None:
    """インデントが戻る箇所に空行を入れ、else / except の前には入れない（再適用しても変わらない）"""
    source = 'def f(x):\n    if x:\n        return 1\n    else:\n        y = 2\n    return y\n'
    expected = 'def f(x):\n    if x:\n        return 1\n    else:\n        y = 2\n\n    return y\n'
    formatted = insert_block_blank_lines(source)
    assert formatted == expected
    assert insert_block_blank_lines(formatted) == formatted


def test_main_check_reports_and_rewrite_fixes(tmp_path: Path) -> None:
    """--check は要修正なら 1 を返し、通常実行はファイルを書き換えて次の --check を通す

    Args:
        tmp_path: pytest の一時ディレクトリ
    """
    target = tmp_path / 'sample.py'
    target.write_text('for i in range(2):\n    pass\nprint(i)\n', encoding='utf-8')
    assert main(['--check', str(target)]) == 1
    assert main([str(target)]) == 0
    assert main(['--check', str(target)]) == 0
    assert target.read_text(encoding='utf-8') == 'for i in range(2):\n    pass\n\nprint(i)\n'
