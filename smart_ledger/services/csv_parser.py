"""クレジットカード会社の利用明細 CSV を解析する

想定フォーマット（先頭にメタ情報、その後に「ご利用年月日」から始まる明細ヘッダー）:

    会員番号,****-****-****-1234
    対象カード,○○カード
    お支払日,2026年10月05日
    今回お支払金額,"119,108"

    ご利用年月日,ご利用箇所,ご利用額,払戻額,ご請求額（うち手数料・利息）,...
    ****-****-****-1234 氏名            ← カード保有者行（明細ではないので無視）
    2026/08/10,加盟店名,"20,871",,"20,871",１回払,,"20,871",,   ,

- 固定行数 skip ではなく、ヘッダー行を「ご利用年月日」で検出する
- 文字コードは UTF-8(BOM 有無どちらも) → CP932 の順に試す（constants.CSV_ENCODINGS）
- 会員番号・氏名などはパース結果に含めない（card はカード名のみ）
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from ..constants import (
    CSV_AMOUNT_COLUMNS,
    CSV_CARD_META_KEYS,
    CSV_DATE_COLUMNS,
    CSV_DATE_FORMATS,
    CSV_ENCODINGS,
    CSV_HEADER_MARKER,
    CSV_MERCHANT_COLUMNS,
    CSV_REFUND_COLUMNS,
)
from .normalize import normalize_merchant

logger = logging.getLogger(__name__)


class CsvParseError(Exception):
    """CSV が想定フォーマットでない場合に投げる"""


@dataclass
class ParsedRow:
    """CSV から読み取った明細 1 行"""

    usage_date: date
    merchant_raw: str
    merchant_normalized: str
    amount: int
    card: str
    source_line: int  # CSV 内の行番号（1 始まり）
    occurrence: int = 0  # 同ファイル内で (利用日, 加盟店, 金額) が同じ行の何番目か
    row_key: str = ''

    def compute_row_key(self) -> str:
        """利用日・正規化加盟店・金額・出現回数から明細識別用の SHA-256 を計算して保持する"""
        base = f'{self.usage_date.isoformat()}|{self.merchant_normalized}|{self.amount}|{self.occurrence}'
        self.row_key = hashlib.sha256(base.encode('utf-8')).hexdigest()
        return self.row_key


@dataclass
class ParsedStatement:
    """CSV 1 ファイルの解析結果"""

    rows: list[ParsedRow]
    card: str
    encoding: str
    header_line: int
    file_hash: str
    skipped_lines: int = 0
    warnings: list[str] = field(default_factory=list)


def decode_bytes(data: bytes, encodings: tuple[str, ...] = CSV_ENCODINGS) -> tuple[str, str]:
    """複数の文字コードを順に試してデコードする

    Args:
        data: CSV のバイト列
        encodings: 試す順序のエンコーディング名

    Returns:
        (テキスト, 使用したエンコーディング名)
    """
    last_error: Exception | None = None
    for enc in encodings:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError as exc:
            last_error = exc

    raise CsvParseError(f'CSV の文字コードを判定できませんでした: {last_error}')


def find_header_index(lines: list[list[str]], header_marker: str = CSV_HEADER_MARKER) -> int:
    """明細ヘッダー行（先頭セルが header_marker で始まる行）の index を返す

    Args:
        lines: csv.reader で読んだ全行
        header_marker: ヘッダー行の先頭セルの文字列
    """
    for idx, fields_ in enumerate(lines):
        if not fields_:
            continue

        first = unicodedata.normalize('NFKC', fields_[0]).strip()
        if first.startswith(header_marker):
            return idx

    raise CsvParseError(f'明細ヘッダー行(「{header_marker}」で始まる行)が見つかりませんでした。')


def find_column(header: list[str], candidates: tuple[str, ...]) -> int | None:
    """候補名に一致する列の index を返す（完全一致を優先し、次に部分一致）

    Args:
        header: ヘッダー行のセル
        candidates: 優先順の列名候補
    """
    normalized = [unicodedata.normalize('NFKC', h).strip() for h in header]
    for cand in candidates:
        for i, h in enumerate(normalized):
            if h == cand:
                return i

    for cand in candidates:
        for i, h in enumerate(normalized):
            if cand in h:
                return i

    return None


def parse_date(text: str, formats: tuple[str, ...] = CSV_DATE_FORMATS) -> date | None:
    """日付文字列を date にする（どの形式にも合わなければ None）

    Args:
        text: '2026/08/10' などの文字列
        formats: 試す strptime 形式
    """
    text = unicodedata.normalize('NFKC', (text or '').strip())
    if not text:
        return None

    for pattern in formats:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue

    return None


def parse_amount(text: str) -> int | None:
    """金額文字列を int にする（カンマ・円記号を除去、▲や括弧は負値。読めなければ None）

    Args:
        text: '"20,871"' や '440' などの文字列
    """
    text = unicodedata.normalize('NFKC', (text or '').strip())
    if not text:
        return None

    negative = text.startswith('-') or text.startswith('▲') or (text.startswith('(') and text.endswith(')'))
    cleaned = re.sub(r'[,\s¥￥円]', '', text).strip('-▲()')
    if not cleaned:
        return None

    try:
        value = int(round(float(cleaned)))
    except ValueError:
        return None

    return -value if negative else value


def extract_meta(lines: list[list[str]], header_idx: int, card_keys: tuple[str, ...] = CSV_CARD_META_KEYS) -> str:
    """ヘッダーより前のメタ情報からカード名を拾う（会員番号は読まない。無ければ空文字）

    Args:
        lines: csv.reader で読んだ全行
        header_idx: 明細ヘッダー行の index
        card_keys: カード名を示すメタ行のキー候補
    """
    for fields_ in lines[:header_idx]:
        if len(fields_) < 2:
            continue

        key = unicodedata.normalize('NFKC', fields_[0]).strip()
        if any(k in key for k in card_keys):
            return normalize_merchant(fields_[1].strip())

    return ''


def parse_statement_bytes(data: bytes) -> ParsedStatement:
    """CSV のバイト列を解析して明細行を返す

    Args:
        data: CSV のバイト列
    """
    text, encoding = decode_bytes(data)
    lines = list(csv.reader(io.StringIO(text)))
    if not lines:
        raise CsvParseError('CSV が空です。')

    header_idx = find_header_index(lines)
    header = [h.strip() for h in lines[header_idx]]
    card = extract_meta(lines, header_idx)

    date_col = find_column(header, CSV_DATE_COLUMNS)
    merchant_col = find_column(header, CSV_MERCHANT_COLUMNS)
    amount_col = find_column(header, CSV_AMOUNT_COLUMNS)
    refund_col = find_column(header, CSV_REFUND_COLUMNS)
    if date_col is None or merchant_col is None or amount_col is None:
        raise CsvParseError(
            '明細ヘッダーに必要な列(利用日・利用先・利用額)が見つかりませんでした: ' + ', '.join(header)
        )

    rows: list[ParsedRow] = []
    warnings: list[str] = []
    skipped = 0
    occurrence_counter: dict[tuple[str, str, int], int] = {}
    for line_no, fields_ in enumerate(lines[header_idx + 1 :], start=header_idx + 2):
        if not fields_ or all(not f.strip() for f in fields_):
            continue

        usage_date = parse_date(fields_[date_col]) if len(fields_) > date_col else None
        if usage_date is None:
            # カード保有者行（****-****-****-1234 氏名）や合計行などは明細ではない
            skipped += 1
            continue

        merchant_raw = fields_[merchant_col] if len(fields_) > merchant_col else ''
        amount = parse_amount(fields_[amount_col]) if len(fields_) > amount_col else None
        if amount is None and refund_col is not None and len(fields_) > refund_col:
            refund = parse_amount(fields_[refund_col])
            if refund is not None:
                amount = -abs(refund)

        if amount is None:
            warnings.append(f'{line_no} 行目: 金額を読み取れなかったためスキップしました。')
            skipped += 1
            continue

        merchant_normalized = normalize_merchant(merchant_raw)
        key = (usage_date.isoformat(), merchant_normalized, amount)
        occurrence = occurrence_counter.get(key, 0)
        occurrence_counter[key] = occurrence + 1

        row = ParsedRow(
            usage_date=usage_date,
            merchant_raw=merchant_raw,
            merchant_normalized=merchant_normalized,
            amount=amount,
            card=card,
            source_line=line_no,
            occurrence=occurrence,
        )
        row.compute_row_key()
        rows.append(row)

    logger.info(
        'csv parsed: encoding=%s header line=%d rows=%d skipped=%d', encoding, header_idx + 1, len(rows), skipped
    )
    return ParsedStatement(
        rows=rows,
        card=card,
        encoding=encoding,
        header_line=header_idx + 1,
        file_hash=hashlib.sha256(data).hexdigest(),
        skipped_lines=skipped,
        warnings=warnings,
    )
