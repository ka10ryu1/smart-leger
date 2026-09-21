"""家計簿の中心となるドメインモデル

Jev はあくまで「未知の加盟店に初期カテゴリを付ける交換可能な分類器」であり、
ここに定義する Transaction / Category / MerchantRule / Allocation がアプリの中核
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar

from .constants import CATEGORY_DESCRIPTIONS, DEFAULT_CATEGORIES, SOURCE_ERROR, SOURCE_JEV


def new_id(prefix: str) -> str:
    """接頭辞付きの短い一意 ID を生成する

    Args:
        prefix: 'tx' や 'imp' などの接頭辞
    """
    return f'{prefix}_{uuid.uuid4().hex[:12]}'


def now_iso() -> str:
    """現在時刻を 'YYYY-MM-DD HH:MM:SS' 形式で返す"""
    return datetime.now().replace(microsecond=0).isoformat(sep=' ')


def to_str(value: Any) -> str:
    """Excel セルの値を文字列に変換する（None は空文字、日付は ISO 形式）

    Args:
        value: セルの値
    """
    if value is None:
        return ''

    if isinstance(value, (datetime, date)):
        return value.isoformat()[:19].replace('T', ' ')

    return str(value)


def to_date(value: Any) -> date:
    """Excel セルの値を date に変換する

    Args:
        value: datetime / date / 'YYYY-MM-DD' または 'YYYY/M/D' で始まる文字列（月日のゼロ埋めは不要）
    """
    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    match = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', str(value).strip())
    if match is None:
        raise ValueError(f'日付として読めません: {value}')

    return date(*(int(part) for part in match.groups()))


def to_int(value: Any) -> int:
    """Excel セルの値を int に変換する（空は 0、カンマや通貨記号は除去）

    Args:
        value: セルの値
    """
    if value is None or value == '':
        return 0

    if isinstance(value, str):
        value = value.replace(',', '').replace('¥', '').replace('￥', '').strip()

    return int(round(float(value)))


def to_float_or_none(value: Any) -> float | None:
    """Excel セルの値を float に変換する（空や変換不能なら None）

    Args:
        value: セルの値
    """
    if value is None or value == '':
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Transaction:
    """カード利用明細 1 行（transactions シートの 1 行に対応）"""

    COLUMNS: ClassVar[tuple[str, ...]] = (
        'id',
        'usage_date',
        'merchant_raw',
        'merchant_normalized',
        'amount',
        'category',
        'confidence',
        'classification_source',
        'card',
        'import_id',
        'imported_at',
        'row_key',
        'memo',
    )

    id: str
    usage_date: date
    merchant_raw: str
    merchant_normalized: str
    amount: int
    category: str = ''
    confidence: float | None = None
    classification_source: str = ''
    card: str = ''
    import_id: str = ''
    imported_at: str = ''
    row_key: str = ''
    memo: str = ''

    @property
    def month(self) -> str:
        """利用日の年月（'YYYY-MM'）"""
        return f'{self.usage_date.year:04d}-{self.usage_date.month:02d}'  # strftime は避ける（月次集計が全明細に対して呼ぶ）

    def needs_review(self, threshold: float) -> bool:
        """要確認かどうかを返す（未分類 / Jev エラー / confidence が閾値未満）

        Args:
            threshold: 自動採用する confidence の閾値
        """
        if not self.category:
            return True

        if self.classification_source == SOURCE_ERROR:
            return True

        if self.classification_source == SOURCE_JEV:
            return self.confidence is None or self.confidence < threshold

        return False

    def to_row(self) -> list[Any]:
        """Excel 行（COLUMNS 順）に変換する"""
        return [
            self.id,
            self.usage_date.isoformat(),
            self.merchant_raw,
            self.merchant_normalized,
            self.amount,
            self.category,
            self.confidence,
            self.classification_source,
            self.card,
            self.import_id,
            self.imported_at,
            self.row_key,
            self.memo,
        ]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Transaction:
        """Excel 行（列名 → 値の dict）から生成する

        Args:
            row: 列名をキーにしたセル値の dict
        """
        return cls(
            id=to_str(row.get('id')),
            usage_date=to_date(row.get('usage_date')),
            merchant_raw=to_str(row.get('merchant_raw')),
            merchant_normalized=to_str(row.get('merchant_normalized')),
            amount=to_int(row.get('amount')),
            category=to_str(row.get('category')),
            confidence=to_float_or_none(row.get('confidence')),
            classification_source=to_str(row.get('classification_source')),
            card=to_str(row.get('card')),
            import_id=to_str(row.get('import_id')),
            imported_at=to_str(row.get('imported_at')),
            row_key=to_str(row.get('row_key')),
            memo=to_str(row.get('memo')),
        )


@dataclass
class MerchantRule:
    """加盟店パターン → カテゴリの固定ルール（merchant_rules シート）"""

    COLUMNS: ClassVar[tuple[str, ...]] = ('merchant_pattern', 'category', 'created_at')

    merchant_pattern: str
    category: str
    created_at: str = field(default_factory=now_iso)

    def to_row(self) -> list[Any]:
        """Excel 行（COLUMNS 順）に変換する"""
        return [self.merchant_pattern, self.category, self.created_at]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MerchantRule:
        """Excel 行から生成する

        Args:
            row: 列名をキーにしたセル値の dict
        """
        return cls(
            merchant_pattern=to_str(row.get('merchant_pattern')),
            category=to_str(row.get('category')),
            created_at=to_str(row.get('created_at')),
        )


@dataclass
class Category:
    """家計簿カテゴリ（categories シート）"""

    COLUMNS: ClassVar[tuple[str, ...]] = ('category', 'sort_order', 'description')

    category: str
    sort_order: int
    description: str = ''  # Jev のカテゴリ選択に渡す説明（空なら constants の既定説明、無ければカテゴリ名）

    def to_row(self) -> list[Any]:
        """Excel 行（COLUMNS 順）に変換する"""
        return [self.category, self.sort_order, self.description]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Category:
        """Excel 行から生成する

        Args:
            row: 列名をキーにしたセル値の dict
        """
        return cls(
            category=to_str(row.get('category')),
            sort_order=to_int(row.get('sort_order')),
            description=to_str(row.get('description')),
        )


@dataclass
class ImportRecord:
    """CSV 取込の履歴（imports シート）"""

    COLUMNS: ClassVar[tuple[str, ...]] = (
        'import_id',
        'filename',
        'file_hash',
        'imported_at',
        'card',
        'row_count',
    )

    import_id: str
    filename: str
    file_hash: str
    imported_at: str
    card: str = ''
    row_count: int = 0

    def to_row(self) -> list[Any]:
        """Excel 行（COLUMNS 順）に変換する"""
        return [self.import_id, self.filename, self.file_hash, self.imported_at, self.card, self.row_count]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ImportRecord:
        """Excel 行から生成する

        Args:
            row: 列名をキーにしたセル値の dict
        """
        return cls(
            import_id=to_str(row.get('import_id')),
            filename=to_str(row.get('filename')),
            file_hash=to_str(row.get('file_hash')),
            imported_at=to_str(row.get('imported_at')),
            card=to_str(row.get('card')),
            row_count=to_int(row.get('row_count')),
        )


@dataclass
class Allocation:
    """1 明細を複数カテゴリに分割した内訳（allocations シート）"""

    COLUMNS: ClassVar[tuple[str, ...]] = ('transaction_id', 'category', 'amount', 'memo')

    transaction_id: str
    category: str
    amount: int
    memo: str = ''

    def to_row(self) -> list[Any]:
        """Excel 行（COLUMNS 順）に変換する"""
        return [self.transaction_id, self.category, self.amount, self.memo]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Allocation:
        """Excel 行から生成する

        Args:
            row: 列名をキーにしたセル値の dict
        """
        return cls(
            transaction_id=to_str(row.get('transaction_id')),
            category=to_str(row.get('category')),
            amount=to_int(row.get('amount')),
            memo=to_str(row.get('memo')),
        )


@dataclass
class ClassificationResult:
    """分類器の出力（Jev 以外の分類器に置き換えても同じ形で返す）"""

    category: str
    confidence: float | None
    source: str
    error: str | None = None


@dataclass
class LedgerData:
    """household.xlsx の全シートをメモリ上に載せたもの"""

    transactions: list[Transaction] = field(default_factory=list)
    merchant_rules: list[MerchantRule] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)

    def sorted_categories(self) -> list[Category]:
        """sort_order 順（同順なら名前順）のカテゴリを返す"""
        return sorted(self.categories, key=lambda c: (c.sort_order, c.category))

    def category_names(self) -> list[str]:
        """sort_order 順のカテゴリ名を返す（categories シートが空なら初期値）"""
        names = [c.category for c in self.sorted_categories() if c.category]
        return names or list(DEFAULT_CATEGORIES)

    def category_criteria(self) -> dict[str, str]:
        """Jev の choice に渡す カテゴリ名 → 説明 を sort_order 順で返す（説明が無ければ既定説明、それも無ければ名前）"""
        described = {c.category: c.description for c in self.categories}
        return {name: described.get(name) or CATEGORY_DESCRIPTIONS.get(name, name) for name in self.category_names()}

    def find_transaction(self, tx_id: str) -> Transaction | None:
        """ID で明細を探す

        Args:
            tx_id: 明細 ID
        """
        for tx in self.transactions:
            if tx.id == tx_id:
                return tx

        return None

    def allocations_for(self, tx_id: str) -> list[Allocation]:
        """明細に紐づく内訳を返す

        Args:
            tx_id: 明細 ID
        """
        return [a for a in self.allocations if a.transaction_id == tx_id]

    def existing_row_keys(self) -> set[tuple[str, str]]:
        """取込済み明細の (row_key, card) 集合を返す（同じカードの明細どうしで重複を判定する）"""
        return {(tx.row_key, tx.card) for tx in self.transactions if tx.row_key}

    def has_file_hash(self, file_hash: str) -> ImportRecord | None:
        """同じファイルハッシュの取込履歴を探す（複数あれば最新）

        Args:
            file_hash: CSV の SHA-256
        """
        for imp in reversed(self.imports):
            if imp.file_hash == file_hash:
                return imp

        return None
