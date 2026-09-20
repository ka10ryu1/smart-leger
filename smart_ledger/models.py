"""家計簿の中心となるドメインモデル。

Jev はあくまで「未知の加盟店に初期カテゴリを付ける交換可能な分類器」であり、
ここに定義する Transaction / Category / MerchantRule / Allocation がアプリの中核。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar

DEFAULT_CATEGORIES: tuple[str, ...] = (
    "食費",
    "外食",
    "日用品・買い物",
    "住居・光熱",
    "通信",
    "交通",
    "保険・税金",
    "娯楽・サブスク",
    "衣服・美容",
    "その他",
)
FALLBACK_CATEGORY = "その他"

# Jev のカテゴリ選択に使う説明文(criteria)。カテゴリは categories シートで管理し、
# 説明はここで補う。未知のカテゴリは名前のみで渡す。
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "食費": "スーパー・食料品店・食材の購入、飲料(自宅で消費するもの)",
    "外食": "レストラン・カフェ・居酒屋・ファストフード・フードデリバリー",
    "日用品・買い物": "ドラッグストア・ホームセンター・雑貨・家電・ネット通販の一般的な買い物",
    "住居・光熱": "家賃・住宅ローン・電気・ガス・水道",
    "通信": "携帯電話料金・インターネット回線・ケーブルテレビ・プロバイダ",
    "交通": "電車・バス・タクシー・交通系ICチャージ・ガソリン・高速道路・駐車場",
    "保険・税金": "生命保険・医療保険・損害保険の保険料、税金・公的支払い",
    "娯楽・サブスク": "動画・音楽配信、ゲーム、書籍、映画、旅行、趣味、各種サブスクリプション",
    "衣服・美容": "衣料品・靴・美容院・化粧品・エステ",
    "その他": "上記のどれにも当てはまらない、または判断できない支出(送金・チャージ系サービスを含む)",
}

# classification_source の値
SOURCE_RULE = "rule"
SOURCE_JEV = "jev"
SOURCE_MANUAL = "manual"
SOURCE_ERROR = "error"  # Jev 呼び出し失敗・APIキー未設定など


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _to_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:19].replace("T", " ")
    return str(value)


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10].replace("/", "-")
    return date.fromisoformat(text)


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, str):
        value = value.replace(",", "").replace("¥", "").replace("￥", "").strip()
    return int(round(float(value)))


def _to_float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Transaction:
    COLUMNS: ClassVar[tuple[str, ...]] = (
        "id",
        "usage_date",
        "merchant_raw",
        "merchant_normalized",
        "amount",
        "category",
        "confidence",
        "classification_source",
        "card",
        "import_id",
        "imported_at",
        "row_key",
        "memo",
    )

    id: str
    usage_date: date
    merchant_raw: str
    merchant_normalized: str
    amount: int
    category: str = ""
    confidence: float | None = None
    classification_source: str = ""
    card: str = ""
    import_id: str = ""
    imported_at: str = ""
    row_key: str = ""
    memo: str = ""

    @property
    def month(self) -> str:
        return self.usage_date.strftime("%Y-%m")

    def needs_review(self, threshold: float) -> bool:
        if not self.category:
            return True
        if self.classification_source == SOURCE_ERROR:
            return True
        if self.classification_source == SOURCE_JEV:
            return self.confidence is None or self.confidence < threshold
        return False

    def to_row(self) -> list[Any]:
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
    def from_row(cls, row: dict[str, Any]) -> "Transaction":
        return cls(
            id=_to_str(row.get("id")),
            usage_date=_to_date(row.get("usage_date")),
            merchant_raw=_to_str(row.get("merchant_raw")),
            merchant_normalized=_to_str(row.get("merchant_normalized")),
            amount=_to_int(row.get("amount")),
            category=_to_str(row.get("category")),
            confidence=_to_float_or_none(row.get("confidence")),
            classification_source=_to_str(row.get("classification_source")),
            card=_to_str(row.get("card")),
            import_id=_to_str(row.get("import_id")),
            imported_at=_to_str(row.get("imported_at")),
            row_key=_to_str(row.get("row_key")),
            memo=_to_str(row.get("memo")),
        )


@dataclass
class MerchantRule:
    COLUMNS: ClassVar[tuple[str, ...]] = ("merchant_pattern", "category", "created_at")

    merchant_pattern: str
    category: str
    created_at: str = field(default_factory=now_iso)

    def to_row(self) -> list[Any]:
        return [self.merchant_pattern, self.category, self.created_at]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MerchantRule":
        return cls(
            merchant_pattern=_to_str(row.get("merchant_pattern")),
            category=_to_str(row.get("category")),
            created_at=_to_str(row.get("created_at")),
        )


@dataclass
class Category:
    COLUMNS: ClassVar[tuple[str, ...]] = ("category", "sort_order")

    category: str
    sort_order: int

    def to_row(self) -> list[Any]:
        return [self.category, self.sort_order]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Category":
        return cls(category=_to_str(row.get("category")), sort_order=_to_int(row.get("sort_order")))


@dataclass
class ImportRecord:
    COLUMNS: ClassVar[tuple[str, ...]] = (
        "import_id",
        "filename",
        "file_hash",
        "imported_at",
        "card",
        "row_count",
    )

    import_id: str
    filename: str
    file_hash: str
    imported_at: str
    card: str = ""
    row_count: int = 0

    def to_row(self) -> list[Any]:
        return [self.import_id, self.filename, self.file_hash, self.imported_at, self.card, self.row_count]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ImportRecord":
        return cls(
            import_id=_to_str(row.get("import_id")),
            filename=_to_str(row.get("filename")),
            file_hash=_to_str(row.get("file_hash")),
            imported_at=_to_str(row.get("imported_at")),
            card=_to_str(row.get("card")),
            row_count=_to_int(row.get("row_count")),
        )


@dataclass
class Allocation:
    COLUMNS: ClassVar[tuple[str, ...]] = ("transaction_id", "category", "amount", "memo")

    transaction_id: str
    category: str
    amount: int
    memo: str = ""

    def to_row(self) -> list[Any]:
        return [self.transaction_id, self.category, self.amount, self.memo]

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Allocation":
        return cls(
            transaction_id=_to_str(row.get("transaction_id")),
            category=_to_str(row.get("category")),
            amount=_to_int(row.get("amount")),
            memo=_to_str(row.get("memo")),
        )


@dataclass
class ClassificationResult:
    """分類器の出力。Jev 以外の分類器に置き換えても同じ形で返す。"""

    category: str
    confidence: float | None
    source: str
    error: str | None = None
    probabilities: dict[str, float] | None = None


@dataclass
class LedgerData:
    """household.xlsx の全シートをメモリ上に載せたもの。"""

    transactions: list[Transaction] = field(default_factory=list)
    merchant_rules: list[MerchantRule] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    allocations: list[Allocation] = field(default_factory=list)

    def category_names(self) -> list[str]:
        cats = sorted(self.categories, key=lambda c: (c.sort_order, c.category))
        names = [c.category for c in cats if c.category]
        return names or list(DEFAULT_CATEGORIES)

    def find_transaction(self, tx_id: str) -> Transaction | None:
        for tx in self.transactions:
            if tx.id == tx_id:
                return tx
        return None

    def allocations_for(self, tx_id: str) -> list[Allocation]:
        return [a for a in self.allocations if a.transaction_id == tx_id]

    def existing_row_keys(self) -> set[str]:
        return {tx.row_key for tx in self.transactions if tx.row_key}

    def has_file_hash(self, file_hash: str) -> ImportRecord | None:
        for imp in self.imports:
            if imp.file_hash == file_hash:
                return imp
        return None

