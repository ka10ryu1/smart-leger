"""ブロック終了後（インデントが戻る箇所）に空行を挿入する整形ツール

CLAUDE.md の「インデントが戻る場所には空行を入れる」ルールを機械的に適用する
（else / elif / except / finally の前には入れない）

    python tools/insert_block_blank_lines.py smart_ledger tests app.py
    python tools/insert_block_blank_lines.py --check smart_ledger tests app.py
"""

from __future__ import annotations

import argparse
import io
import sys
import tokenize
from pathlib import Path


def _indent_of(line: str) -> int:
    """行頭の空白数を返す

    Args:
        line: 対象の 1 行
    """
    return len(line) - len(line.lstrip(' \t'))


def insert_block_blank_lines(
    source: str,
    continuation_keywords: tuple[str, ...] = ('else', 'elif', 'except', 'finally'),
) -> str:
    """ブロック終了後の行の直前に空行を挿入したソースを返す

    Args:
        source: Python ソースコード
        continuation_keywords: 直前に空行を入れない継続キーワード

    Returns:
        整形後のソース（変更が無ければ入力と同じ文字列）
    """
    lines = source.splitlines(keepends=True)
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    insert_before: set[int] = set()
    skip_types = {tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.ENDMARKER}
    pending_dedent = False
    for tok in tokens:
        if tok.type == tokenize.DEDENT:
            pending_dedent = True
            continue

        if tok.type in skip_types or not pending_dedent:
            continue

        pending_dedent = False
        if tok.string in continuation_keywords:
            continue

        target = tok.start[0] - 1
        indent = _indent_of(lines[target])
        # 直前にぶら下がるコメント行はまとめて扱う
        while target > 0 and lines[target - 1].strip().startswith('#') and _indent_of(lines[target - 1]) == indent:
            target -= 1

        if target > 0 and lines[target - 1].strip() != '':
            insert_before.add(target)

    if not insert_before:
        return source

    out: list[str] = []
    for index, line in enumerate(lines):
        if index in insert_before:
            out.append('\n')

        out.append(line)

    return ''.join(out)


def iter_python_files(paths: list[str]) -> list[Path]:
    """引数のパス（ファイルまたはディレクトリ）から .py ファイルを列挙する

    Args:
        paths: ファイルまたはディレクトリのパス
    """
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob('*.py') if '.venv' not in p.parts))
        elif path.suffix == '.py':
            files.append(path)

    return files


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント

    Args:
        argv: コマンドライン引数（None なら sys.argv）

    Returns:
        終了コード（--check で修正が必要なファイルがあれば 1）
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--check', action='store_true', help='書き換えずに差分の有無だけ確認する')
    args = parser.parse_args(argv)

    changed: list[Path] = []
    for path in iter_python_files(args.paths):
        original = path.read_text(encoding='utf-8')
        formatted = insert_block_blank_lines(original)
        if formatted == original:
            continue

        changed.append(path)
        if not args.check:
            path.write_text(formatted, encoding='utf-8')

    for path in changed:
        print(('would reformat: ' if args.check else 'reformatted: ') + str(path))

    return 1 if (args.check and changed) else 0


if __name__ == '__main__':
    sys.exit(main())
