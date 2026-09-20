"""CSV パーサーのテスト"""

from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.services.csv_parser import (
    CsvParseError,
    decode_bytes,
    find_header_index,
    parse_amount,
    parse_statement_bytes,
)


def test_header_detection_not_fixed_line_count() -> None:
    """ヘッダー行は固定行数ではなく「ご利用年月日」で検出する"""
    lines = [['会員番号', 'x'], ['対象カード', 'y'], [], ['メモ', '追加行'], ['ご利用年月日', 'ご利用箇所', 'ご利用額']]
    assert find_header_index(lines) == 4


def test_header_detection_missing_raises() -> None:
    """ヘッダー行が無ければ CsvParseError"""
    with pytest.raises(CsvParseError):
        find_header_index([['a', 'b'], ['c', 'd']])


def test_decode_cp932(fixture_csv_bytes: bytes) -> None:
    """CP932 の CSV を読める

    Args:
        fixture_csv_bytes: CP932 の fixture
    """
    text, enc = decode_bytes(fixture_csv_bytes)
    assert enc == 'cp932'
    assert 'ご利用年月日' in text


def test_decode_utf8_bom() -> None:
    """UTF-8 BOM 付き CSV を読める"""
    data = '﻿ご利用年月日,ご利用箇所,ご利用額\r\n2026/08/01,テスト,100\r\n'.encode('utf-8')
    text, enc = decode_bytes(data)
    assert enc == 'utf-8-sig'
    assert text.startswith('﻿') or text.startswith('ご利用年月日')
    parsed = parse_statement_bytes(data)
    assert parsed.rows[0].amount == 100


def test_parse_cp932_statement(fixture_csv_bytes: bytes) -> None:
    """メタ情報・カード名・明細・払戻額を正しく読み取る（氏名は含まない）

    Args:
        fixture_csv_bytes: CP932 の fixture
    """
    parsed = parse_statement_bytes(fixture_csv_bytes)
    assert parsed.encoding == 'cp932'
    assert parsed.header_line == 6
    assert parsed.card == 'テストカード'
    assert all('太郎' not in r.merchant_raw for r in parsed.rows)
    assert len(parsed.rows) == 10

    first = parsed.rows[0]
    assert first.usage_date == date(2026, 8, 10)
    assert first.merchant_raw == 'テストデンリヨク　８ガツブン'
    assert first.merchant_normalized == 'テストデンリヨク 8ガツブン'
    assert first.amount == 7850

    refund = parsed.rows[-1]
    assert refund.amount == -1000


def test_usage_date_is_kept_not_billing_date(fixture_csv_bytes: bytes) -> None:
    """月は請求月(2026-10)ではなく利用日から決まる

    Args:
        fixture_csv_bytes: CP932 の fixture
    """
    parsed = parse_statement_bytes(fixture_csv_bytes)
    months = {r.usage_date.strftime('%Y-%m') for r in parsed.rows}
    assert months == {'2026-08', '2026-09'}


def test_same_day_same_merchant_same_amount_rows_are_distinct(fixture_csv_bytes: bytes) -> None:
    """同日・同加盟店・同金額の行は出現回数で区別され row_key が重複しない

    Args:
        fixture_csv_bytes: CP932 の fixture
    """
    parsed = parse_statement_bytes(fixture_csv_bytes)
    same = [r for r in parsed.rows if r.merchant_normalized == 'サンプルホケン(ホケンリヨウ)']
    assert len(same) == 2
    assert same[0].occurrence == 0 and same[1].occurrence == 1
    assert same[0].row_key != same[1].row_key
    assert len({r.row_key for r in parsed.rows}) == len(parsed.rows)


def test_row_key_stable_across_parses(fixture_csv_bytes: bytes) -> None:
    """同じ CSV を再解析しても row_key は変わらない

    Args:
        fixture_csv_bytes: CP932 の fixture
    """
    a = parse_statement_bytes(fixture_csv_bytes)
    b = parse_statement_bytes(fixture_csv_bytes)
    assert [r.row_key for r in a.rows] == [r.row_key for r in b.rows]


@pytest.mark.parametrize(
    'raw,expected',
    [('20,871', 20871), ('440', 440), ('-1,000', -1000), ('', None), ('abc', None)],
)
def test_parse_amount(raw: str, expected: int | None) -> None:
    """金額文字列の変換

    Args:
        raw: 入力
        expected: 期待値（読めない場合は None）
    """
    assert parse_amount(raw) == expected
