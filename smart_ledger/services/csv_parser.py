"""利用明細 CSV を解析する（カード明細 / 銀行口座明細の 2 プロファイル）

プロファイル card（クレジットカードの利用明細。先頭にメタ情報、その後に「ご利用年月日」から始まる明細ヘッダー）:

    会員番号,****-****-****-1234
    対象カード,○○カード
    お支払日,2026年10月05日
    今回お支払金額,"119,108"

    ご利用年月日,ご利用箇所,ご利用額,払戻額,ご請求額（うち手数料・利息）,...
    ****-****-****-1234 氏名            ← カード保有者行（明細ではないので無視）
    2026/08/10,加盟店名,"20,871",,"20,871",１回払,,"20,871",,   ,

プロファイル bank（銀行口座の入出金明細。1 行目が「日付」から始まるヘッダー、新しい日付が先）:

    "日付","内容","出金金額(円)","入金金額(円)","残高(円)","メモ"
    "2026/09/28","約定返済　円　住宅","70,000",,"120,000","-"
    "2026/09/05","振込＊トウデンＰＧ　コウニユウ",,"7,000","190,000","-"

- 固定行数 skip ではなく、ヘッダー行を「ご利用年月日」「日付」で検出してプロファイルを選ぶ
- 文字コードは UTF-8(BOM 有無どちらも) → CP932 の順に試す（constants.CSV_ENCODINGS）
- bank は許可リスト（constants.BANK_CSV_TARGETS）に一致する行だけを取り込み、他は「対象外」として数える
- bank は出金を支出、入金を収入（kind=income）として読む
- 会員番号・氏名・残高はパース結果に含めない（card はカード名のみ、bank は固定の口座名）
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
    BANK_CSV_ACCOUNT,
    BANK_CSV_DATE_COLUMNS,
    BANK_CSV_DEPOSIT_COLUMNS,
    BANK_CSV_DESCRIPTION_COLUMNS,
    BANK_CSV_HEADER_MARKER,
    BANK_CSV_TARGETS,
    BANK_CSV_WITHDRAWAL_COLUMNS,
    CSV_AMOUNT_COLUMNS,
    CSV_CARD_META_KEYS,
    CSV_DATE_COLUMNS,
    CSV_DATE_FORMATS,
    CSV_ENCODINGS,
    CSV_HEADER_MARKER,
    CSV_MERCHANT_COLUMNS,
    CSV_REFUND_COLUMNS,
    KIND_EXPENSE,
    KIND_INCOME,
)
from .normalize import normalize_merchant

logger = logging.getLogger(__name__)


class CsvParseError(Exception):
    """CSV が想定フォーマットでない場合に投げる"""


@dataclass(frozen=True)
class CsvProfile:
    """CSV の種類ごとの読み取り方（ヘッダーの目印・列名候補・口座名・取込対象）"""

    name: str
    header_marker: str
    date_columns: tuple[str, ...]
    description_columns: tuple[str, ...]  # 加盟店名 / 取引内容の列
    amount_columns: tuple[str, ...]  # 支出として読む金額の列
    refund_columns: tuple[str, ...] = ()  # 負の支出として読む列（カードの払戻額）
    income_columns: tuple[str, ...] = ()  # 収入として読む列（銀行の入金金額）
    fixed_account: str = ''  # 口座名を CSV から読まず固定する場合の名前
    targets: tuple[tuple[str, str], ...] = ()  # (内容の正規表現, カテゴリ)。空なら全行を取り込む


@dataclass
class ColumnIndexes:
    """プロファイルの列名候補をヘッダー行に当てはめた結果（見つからない列は None）"""

    date: int
    description: int
    amount: int | None = None
    refund: int | None = None
    income: int | None = None


@dataclass
class ParsedRow:
    """CSV から読み取った明細 1 行"""

    usage_date: date
    merchant_raw: str
    merchant_normalized: str
    amount: int
    card: str
    source_line: int  # CSV 内の行番号（1 始まり）
    kind: str = KIND_EXPENSE
    category_hint: str = ''  # 許可リストで決まったカテゴリ（bank のみ。空なら分類器に任せる）
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
    profile: str = ''
    skipped_lines: int = 0  # 日付や金額を読めなかった行（カード保有者行・合計行など）
    excluded_lines: int = 0  # 明細としては読めたが許可リストに無い行
    warnings: list[str] = field(default_factory=list)


def csv_profiles() -> tuple[CsvProfile, ...]:
    """対応する CSV プロファイルを試す順に返す（先にヘッダー行が見つかったものを使う）"""
    return (
        CsvProfile(
            name='card',
            header_marker=CSV_HEADER_MARKER,
            date_columns=CSV_DATE_COLUMNS,
            description_columns=CSV_MERCHANT_COLUMNS,
            amount_columns=CSV_AMOUNT_COLUMNS,
            refund_columns=CSV_REFUND_COLUMNS,
        ),
        CsvProfile(
            name='bank',
            header_marker=BANK_CSV_HEADER_MARKER,
            date_columns=BANK_CSV_DATE_COLUMNS,
            description_columns=BANK_CSV_DESCRIPTION_COLUMNS,
            amount_columns=BANK_CSV_WITHDRAWAL_COLUMNS,
            income_columns=BANK_CSV_DEPOSIT_COLUMNS,
            fixed_account=BANK_CSV_ACCOUNT,
            targets=BANK_CSV_TARGETS,
        ),
    )


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


def search_header_index(lines: list[list[str]], header_marker: str) -> int | None:
    """明細ヘッダー行（先頭セルが header_marker で始まる行）の index を返す（無ければ None）

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

    return None


def find_header_index(lines: list[list[str]], header_marker: str = CSV_HEADER_MARKER) -> int:
    """明細ヘッダー行の index を返す（見つからなければ CsvParseError）

    Args:
        lines: csv.reader で読んだ全行
        header_marker: ヘッダー行の先頭セルの文字列
    """
    idx = search_header_index(lines, header_marker)
    if idx is None:
        raise CsvParseError(f'明細ヘッダー行(「{header_marker}」で始まる行)が見つかりませんでした。')

    return idx


def select_profile(lines: list[list[str]], profiles: tuple[CsvProfile, ...] | None = None) -> tuple[CsvProfile, int]:
    """ヘッダー行の目印から CSV のプロファイルを選ぶ

    Args:
        lines: csv.reader で読んだ全行
        profiles: 試すプロファイル（既定は csv_profiles()）

    Returns:
        (選ばれたプロファイル, 明細ヘッダー行の index)
    """
    candidates = profiles if profiles is not None else csv_profiles()
    for profile in candidates:
        idx = search_header_index(lines, profile.header_marker)
        if idx is not None:
            return profile, idx

    markers = '」「'.join(p.header_marker for p in candidates)
    raise CsvParseError(f'明細ヘッダー行(「{markers}」のいずれかで始まる行)が見つかりませんでした。')


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


def resolve_columns(header: list[str], profile: CsvProfile) -> ColumnIndexes:
    """ヘッダー行からプロファイルの各列の index を求める

    Args:
        header: ヘッダー行のセル
        profile: 選ばれたプロファイル

    Raises:
        CsvParseError: 日付・内容・金額のいずれかの列が見つからない
    """
    date_col = find_column(header, profile.date_columns)
    description_col = find_column(header, profile.description_columns)
    amount_col = find_column(header, profile.amount_columns)
    income_col = find_column(header, profile.income_columns) if profile.income_columns else None
    if date_col is None or description_col is None or (amount_col is None and income_col is None):
        raise CsvParseError(
            '明細ヘッダーに必要な列(利用日・利用先・利用額)が見つかりませんでした: ' + ', '.join(header)
        )

    return ColumnIndexes(
        date=date_col,
        description=description_col,
        amount=amount_col,
        refund=find_column(header, profile.refund_columns) if profile.refund_columns else None,
        income=income_col,
    )


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


def cell(fields_: list[str], index: int | None) -> str:
    """行から列の値を取り出す（列が無い・行が短い場合は空文字）

    Args:
        fields_: csv.reader が読んだ 1 行
        index: 取り出す列の index
    """
    if index is None or index >= len(fields_):
        return ''

    return fields_[index]


def read_amount(fields_: list[str], columns: ColumnIndexes) -> tuple[int, str] | None:
    """行の金額と収支の向きを読む（読めなければ None）

    Args:
        fields_: csv.reader が読んだ 1 行
        columns: 解決済みの列 index

    Returns:
        (金額, kind)。出金・利用額は支出、入金は収入、払戻額は負の支出として返す
    """
    amount = parse_amount(cell(fields_, columns.amount))
    if amount is not None:
        return amount, KIND_EXPENSE

    income = parse_amount(cell(fields_, columns.income))
    if income is not None:
        return income, KIND_INCOME

    refund = parse_amount(cell(fields_, columns.refund))
    if refund is not None:
        return -abs(refund), KIND_EXPENSE

    return None


def match_target(description: str, targets: tuple[tuple[str, str], ...]) -> str | None:
    """許可リストに一致する行のカテゴリを返す（どれにも一致しなければ None）

    Args:
        description: 正規化済みの取引内容
        targets: (内容の正規表現, カテゴリ) の並び
    """
    for pattern, category in targets:
        if re.search(pattern, description):
            return category

    return None


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


def assign_row_keys(rows: list[ParsedRow]) -> None:
    """利用日順に並べ替えてから出現回数と row_key を付ける

    出現回数は同じ (利用日, 加盟店, 金額) の行どうしでしか数えず、それらの行は互いに区別できないため、
    row_key の集合は CSV の並び順（新しい日付が先 / 古い日付が先）に依存しない。
    並べ替えはプレビューと取込結果の表示順を利用日順に揃えるためのもの

    Args:
        rows: 出現回数と row_key が未設定の明細行（この場で並べ替えて書き換える）
    """
    rows.sort(key=lambda r: r.usage_date)
    occurrence_counter: dict[tuple[str, str, int], int] = {}
    for row in rows:
        key = (row.usage_date.isoformat(), row.merchant_normalized, row.amount)
        row.occurrence = occurrence_counter.get(key, 0)
        occurrence_counter[key] = row.occurrence + 1
        row.compute_row_key()


def parse_statement_bytes(data: bytes) -> ParsedStatement:
    """CSV のバイト列を解析して明細行を返す

    Args:
        data: CSV のバイト列
    """
    text, encoding = decode_bytes(data)
    lines = list(csv.reader(io.StringIO(text)))
    if not lines:
        raise CsvParseError('CSV が空です。')

    profile, header_idx = select_profile(lines)
    header = [h.strip() for h in lines[header_idx]]
    columns = resolve_columns(header, profile)
    card = profile.fixed_account or extract_meta(lines, header_idx)

    rows: list[ParsedRow] = []
    warnings: list[str] = []
    skipped = 0
    excluded = 0
    for line_no, fields_ in enumerate(lines[header_idx + 1 :], start=header_idx + 2):
        if not fields_ or all(not f.strip() for f in fields_):
            continue

        usage_date = parse_date(cell(fields_, columns.date))
        if usage_date is None:
            # カード保有者行（****-****-****-1234 氏名）や合計行などは明細ではない
            skipped += 1
            continue

        merchant_raw = cell(fields_, columns.description)
        merchant_normalized = normalize_merchant(merchant_raw)
        category_hint = ''
        if profile.targets:
            matched = match_target(merchant_normalized, profile.targets)
            if matched is None:  # 許可リストに無い行（税金・利息・口座間の資金移動など）は取り込まない
                excluded += 1
                continue

            category_hint = matched

        read = read_amount(fields_, columns)
        if read is None:
            warnings.append(f'{line_no} 行目: 金額を読み取れなかったためスキップしました。')
            skipped += 1
            continue

        amount, kind = read
        rows.append(
            ParsedRow(
                usage_date=usage_date,
                merchant_raw=merchant_raw,
                merchant_normalized=merchant_normalized,
                amount=amount,
                card=card,
                source_line=line_no,
                kind=kind,
                category_hint=category_hint,
            )
        )

    assign_row_keys(rows)
    logger.info(
        'csv parsed: profile=%s encoding=%s header line=%d rows=%d skipped=%d excluded=%d',
        profile.name,
        encoding,
        header_idx + 1,
        len(rows),
        skipped,
        excluded,
    )
    return ParsedStatement(
        rows=rows,
        card=card,
        encoding=encoding,
        header_line=header_idx + 1,
        file_hash=hashlib.sha256(data).hexdigest(),
        profile=profile.name,
        skipped_lines=skipped,
        excluded_lines=excluded,
        warnings=warnings,
    )
