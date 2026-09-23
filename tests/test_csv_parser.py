"""CSV パーサーのテスト"""

from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.services.csv_parser import (
    CsvParseError,
    decode_bytes,
    parse_amount,
    parse_statement_bytes,
    select_profile,
)


def test_header_detection_not_fixed_line_count() -> None:
    """ヘッダー行は固定行数ではなく「ご利用年月日」で検出する"""
    lines = [['会員番号', 'x'], ['対象カード', 'y'], [], ['メモ', '追加行'], ['ご利用年月日', 'ご利用箇所', 'ご利用額']]
    profile, idx = select_profile(lines)
    assert (profile.name, idx) == ('card', 4)


def test_empty_csv_raises() -> None:
    """空の CSV は CsvParseError"""
    with pytest.raises(CsvParseError, match='空'):
        parse_statement_bytes(b'')


def test_undecodable_csv_raises() -> None:
    """UTF-8 / CP932 のどちらでも読めないバイト列は CsvParseError"""
    with pytest.raises(CsvParseError, match='文字コード'):
        decode_bytes(b'\x81\x00')


def test_missing_required_column_raises() -> None:
    """加盟店列などの必須列が無い CSV は CsvParseError"""
    content = 'ご利用年月日,ご利用額\r\n2026/08/01,100\r\n'.encode()
    with pytest.raises(CsvParseError, match='必要な列'):
        parse_statement_bytes(content)


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


def test_invalid_amount_is_reported_as_skipped_warning() -> None:
    """日付は有効だが金額を読めない行は、警告を残してスキップする"""
    content = 'ご利用年月日,ご利用箇所,ご利用額\r\n2026/08/01,テスト,abc\r\n'.encode()
    parsed = parse_statement_bytes(content)
    assert parsed.rows == []
    assert parsed.skipped_lines == 1
    assert parsed.warnings == ['2 行目: 金額を読み取れなかったためスキップしました。']


def test_usage_amount_takes_precedence_over_refund_amount() -> None:
    """利用額と払戻額の両方がある場合は利用額を採用する"""
    content = (
        'ご利用年月日,ご利用箇所,ご利用額,払戻額\r\n2026/08/01,テスト,100,200\r\n2026/08/02,返金,,200\r\n'
    ).encode()
    parsed = parse_statement_bytes(content)
    assert [row.amount for row in parsed.rows] == [100, -200]


@pytest.mark.parametrize(
    'raw,expected',
    [
        ('20,871', 20871),
        ('440', 440),
        ('-1,000', -1000),
        ('▲2,500', -2500),
        ('(300)', -300),
        ('¥1,000円', 1000),
        ('', None),
        ('abc', None),
    ],
)
def test_parse_amount(raw: str, expected: int | None) -> None:
    """金額文字列の変換

    Args:
        raw: 入力
        expected: 期待値（読めない場合は None）
    """
    assert parse_amount(raw) == expected


def test_bank_profile_reads_only_allowed_rows(fixture_bank_csv_bytes: bytes) -> None:
    """銀行口座 CSV は住宅ローンと売電の行だけを取り込み、出金は支出・入金は収入として読む（他は対象外として数える）

    Args:
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    parsed = parse_statement_bytes(fixture_bank_csv_bytes)
    assert (parsed.profile, parsed.card, parsed.header_line) == ('bank', '銀行口座', 1)
    assert len(parsed.rows) == 5
    assert parsed.excluded_lines == 4  # 地方税・利息・定額自動入金・個人宛振込
    assert parsed.skipped_lines == 0 and parsed.warnings == []
    assert {r.merchant_normalized for r in parsed.rows} == {'約定返済 円 住宅', '振込*トウデンPG コウニユウ'}
    loans = [r for r in parsed.rows if r.category_hint == '住宅ローン']
    solar = [r for r in parsed.rows if r.category_hint == '売電収入']
    assert [r.amount for r in loans] == [69000, 70000, 70000]
    assert all(r.kind == 'expense' for r in loans)
    assert [r.amount for r in solar] == [6500, 7000]
    assert all(r.kind == 'income' for r in solar)  # 入金は負数ではなく kind で表す


@pytest.mark.parametrize(
    ('withdrawal', 'deposit', 'expected'),
    [
        ('', '7,000', (7000, 'income')),
        ('0', '7,000', (7000, 'income')),  # 空欄の代わりに 0 を書く銀行でも入金を落とさない
        ('7,000', '', (7000, 'expense')),
        ('7,000', '0', (7000, 'expense')),
    ],
)
def test_bank_zero_in_other_column_is_ignored(withdrawal: str, deposit: str, expected: tuple[int, str]) -> None:
    """出金・入金の反対側の欄が空でも「0」でも、金額のある側で収支を決める

    Args:
        withdrawal: 出金金額の欄
        deposit: 入金金額の欄
        expected: (金額, kind)
    """
    content = f'日付,内容,出金金額(円),入金金額(円)\r\n2026/09/05,振込＊トウデンＰＧ　コウニユウ,"{withdrawal}","{deposit}"\r\n'
    (row,) = parse_statement_bytes(content.encode()).rows
    assert (row.amount, row.kind) == expected


def test_bank_rows_are_sorted_by_date_and_deduplicated(fixture_bank_csv_bytes: bytes) -> None:
    """新しい日付が先の CSV でも利用日の昇順に並び、同日同額の 2 行は出現回数で区別される

    Args:
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    parsed = parse_statement_bytes(fixture_bank_csv_bytes)
    assert [r.usage_date for r in parsed.rows] == sorted(r.usage_date for r in parsed.rows)
    same_day = [r for r in parsed.rows if r.usage_date == date(2026, 9, 28)]
    assert [r.occurrence for r in same_day] == [0, 1]
    assert len({r.row_key for r in parsed.rows}) == len(parsed.rows)


def test_card_profile_is_preferred_over_bank_profile() -> None:
    """「日付」列を持つカード明細でも、ヘッダーに「ご利用年月日」があれば card として読む"""
    content = '日付,メモ\r\nご利用年月日,ご利用箇所,ご利用額\r\n2026/08/01,テスト,100\r\n'.encode()
    parsed = parse_statement_bytes(content)
    assert parsed.profile == 'card'
    assert [r.category_hint for r in parsed.rows] == ['']  # card は許可リストを使わない


def test_unknown_header_reports_both_markers() -> None:
    """どちらのプロファイルのヘッダーも無ければ、両方の目印を挙げて CsvParseError"""
    with pytest.raises(CsvParseError, match='ご利用年月日.*日付'):
        parse_statement_bytes('氏名,金額\r\nテスト,100\r\n'.encode())


def test_row_keys_do_not_depend_on_file_order(fixture_bank_csv_bytes: bytes) -> None:
    """同じ明細を新しい日付が先 / 古い日付が先のどちらの並びで出力した CSV でも row_key の集合は一致する

    Args:
        fixture_bank_csv_bytes: CP932 の銀行 fixture（新しい日付が先）
    """
    header, *body = fixture_bank_csv_bytes.decode('cp932').rstrip('\r\n').split('\r\n')
    reversed_csv = '\r\n'.join([header, *reversed(body)]).encode('cp932')
    newest_first = parse_statement_bytes(fixture_bank_csv_bytes)
    oldest_first = parse_statement_bytes(reversed_csv)
    assert {r.row_key for r in newest_first.rows} == {r.row_key for r in oldest_first.rows}
    assert [r.usage_date for r in newest_first.rows] == [r.usage_date for r in oldest_first.rows]
