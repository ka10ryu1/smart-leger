"""クレジットカード会社の利用明細 CSV を解析する。

想定フォーマット(先頭にメタ情報、その後に「ご利用年月日」から始まる明細ヘッダー):

    会員番号,****-****-****-1234
    対象カード,○○カード
    お支払日,2026年10月05日
    今回お支払金額,"119,108"

    ご利用年月日,ご利用箇所,ご利用額,払戻額,ご請求額（うち手数料・利息）,...
    ****-****-****-1234 氏名            ← カード保有者行(明細ではないので無視)
    2026/08/10,加盟店名,"20,871",,"20,871",１回払,,"20,871",,   ,

- 固定行数 skip ではなく、ヘッダー行を「ご利用年月日」で検出する。
- 文字コードは UTF-8(BOM) → CP932 → Shift_JIS → UTF-8 の順に試す(ENCODINGS)。
- 会員番号・氏名などはパース結果に含めない(card はカード名のみ)。
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

from .normalize import normalize_merchant

logger = logging.getLogger(__name__)

ENCODINGS = ("utf-8-sig", "cp932", "shift_jis", "utf-8")
HEADER_MARKER = "ご利用年月日"

DATE_COLUMN_CANDIDATES = ("ご利用年月日", "利用日", "ご利用日", "利用年月日")
MERCHANT_COLUMN_CANDIDATES = ("ご利用箇所", "ご利用先", "利用先", "加盟店名", "ご利用店名", "利用店名")
AMOUNT_COLUMN_CANDIDATES = ("ご利用額", "利用金額", "ご利用金額", "利用額", "金額")
REFUND_COLUMN_CANDIDATES = ("払戻額", "返品額", "返金額")
CARD_META_KEYS = ("対象カード", "カード名", "ご利用カード")

_DATE_PATTERNS = ("%Y/%m/%d", "%Y-%m-%d", "%Y年%m月%d日", "%Y.%m.%d", "%Y%m%d")


class CsvParseError(Exception):
    """CSV が想定フォーマットでない場合に投げる。"""


@dataclass
class ParsedRow:
    usage_date: date
    merchant_raw: str
    merchant_normalized: str
    amount: int
    card: str
    source_line: int  # CSV 内の行番号(1 始まり)
    occurrence: int = 0  # 同ファイル内で (利用日, 加盟店, 金額) が同じ行の何番目か
    row_key: str = ""

    def compute_row_key(self) -> str:
        base = f"{self.usage_date.isoformat()}|{self.merchant_normalized}|{self.amount}|{self.occurrence}"
        self.row_key = hashlib.sha256(base.encode("utf-8")).hexdigest()
        return self.row_key


@dataclass
class ParsedStatement:
    rows: list[ParsedRow]
    card: str
    encoding: str
    header_line: int
    header: list[str]
    file_hash: str
    skipped_lines: int = 0
    payment_date: str | None = None
    warnings: list[str] = field(default_factory=list)


def compute_file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_bytes(data: bytes) -> tuple[str, str]:
    """複数の文字コードを試して (テキスト, 使用エンコーディング) を返す。"""
    last_error: Exception | None = None
    for enc in ENCODINGS:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        # UTF-8 で読めたが BOM 無しの場合は utf-8-sig でも同じ結果になる
        return text, enc
    raise CsvParseError(f"CSV の文字コードを判定できませんでした: {last_error}")


def find_header_index(lines: list[list[str]]) -> int:
    """「ご利用年月日」から始まる明細ヘッダー行の index を返す。"""
    for idx, fields_ in enumerate(lines):
        if not fields_:
            continue
        first = unicodedata.normalize("NFKC", fields_[0]).strip().lstrip("﻿")
        if first.startswith(HEADER_MARKER):
            return idx
    raise CsvParseError(f"明細ヘッダー行(「{HEADER_MARKER}」で始まる行)が見つかりませんでした。")


def _find_column(header: list[str], candidates: tuple[str, ...]) -> int | None:
    normalized = [unicodedata.normalize("NFKC", h).strip() for h in header]
    for cand in candidates:
        for i, h in enumerate(normalized):
            if h == cand:
                return i
    for cand in candidates:
        for i, h in enumerate(normalized):
            if cand in h:
                return i
    return None


def parse_date(text: str) -> date | None:
    text = unicodedata.normalize("NFKC", (text or "").strip())
    if not text:
        return None
    for pattern in _DATE_PATTERNS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


_AMOUNT_CLEAN = re.compile(r"[,\s¥￥円]")


def parse_amount(text: str) -> int | None:
    text = unicodedata.normalize("NFKC", (text or "").strip())
    if not text:
        return None
    negative = text.startswith("-") or text.startswith("▲") or (text.startswith("(") and text.endswith(")"))
    cleaned = _AMOUNT_CLEAN.sub("", text).strip("-▲()")
    if not cleaned:
        return None
    try:
        value = int(round(float(cleaned)))
    except ValueError:
        return None
    return -value if negative else value


def _extract_meta(lines: list[list[str]], header_idx: int) -> tuple[str, str | None]:
    """ヘッダーより前のメタ情報からカード名・お支払日を拾う。会員番号は読まない。"""
    card = ""
    payment_date: str | None = None
    for fields_ in lines[:header_idx]:
        if len(fields_) < 2:
            continue
        key = unicodedata.normalize("NFKC", fields_[0]).strip()
        value = fields_[1].strip()
        if any(k in key for k in CARD_META_KEYS) and not card:
            card = normalize_merchant(value)
        elif "お支払日" in key or "支払日" in key:
            payment_date = value
    return card, payment_date


def parse_statement_bytes(data: bytes) -> ParsedStatement:
    text, encoding = decode_bytes(data)
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    lines = list(reader)
    if not lines:
        raise CsvParseError("CSV が空です。")

    header_idx = find_header_index(lines)
    header = [h.strip() for h in lines[header_idx]]
    card, payment_date = _extract_meta(lines, header_idx)

    date_col = _find_column(header, DATE_COLUMN_CANDIDATES)
    merchant_col = _find_column(header, MERCHANT_COLUMN_CANDIDATES)
    amount_col = _find_column(header, AMOUNT_COLUMN_CANDIDATES)
    refund_col = _find_column(header, REFUND_COLUMN_CANDIDATES)
    if date_col is None or merchant_col is None or amount_col is None:
        raise CsvParseError(
            "明細ヘッダーに必要な列(利用日・利用先・利用額)が見つかりませんでした: " + ", ".join(header)
        )

    rows: list[ParsedRow] = []
    warnings: list[str] = []
    skipped = 0
    occurrence_counter: dict[tuple[str, str, int], int] = {}

    for offset, fields_ in enumerate(lines[header_idx + 1 :], start=header_idx + 2):
        if not fields_ or all(not f.strip() for f in fields_):
            continue
        usage_date = parse_date(fields_[date_col]) if len(fields_) > date_col else None
        if usage_date is None:
            # カード保有者行(****-****-****-1234 氏名)や合計行などは明細ではない
            skipped += 1
            continue
        merchant_raw = fields_[merchant_col] if len(fields_) > merchant_col else ""
        amount = parse_amount(fields_[amount_col]) if len(fields_) > amount_col else None
        if amount is None and refund_col is not None and len(fields_) > refund_col:
            refund = parse_amount(fields_[refund_col])
            if refund is not None:
                amount = -abs(refund)
        if amount is None:
            warnings.append(f"{offset} 行目: 金額を読み取れなかったためスキップしました。")
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
            source_line=offset,
            occurrence=occurrence,
        )
        row.compute_row_key()
        rows.append(row)

    logger.info(
        "CSV 解析完了: encoding=%s header_line=%d rows=%d skipped=%d", encoding, header_idx + 1, len(rows), skipped
    )
    return ParsedStatement(
        rows=rows,
        card=card,
        encoding=encoding,
        header_line=header_idx + 1,
        header=header,
        file_hash=compute_file_hash(data),
        skipped_lines=skipped,
        payment_date=payment_date,
        warnings=warnings,
    )


def parse_statement_file(path) -> ParsedStatement:
    with open(path, "rb") as f:
        return parse_statement_bytes(f.read())
