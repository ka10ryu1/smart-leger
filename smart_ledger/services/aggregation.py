"""月次集計(usage_date 基準)。

- 月間総支出: transactions.amount を 1 回だけ合計
- カテゴリ別: allocations がある明細は allocations を、無い明細は transactions.category を使う
  → 二重計上しない
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from ..models import Allocation, LedgerData, Transaction


@dataclass
class CategoryTotal:
    category: str
    amount: int
    ratio: float
    count: int


@dataclass
class MonthlySummary:
    month: str
    total: int
    prev_total: int | None
    diff: int | None
    diff_ratio: float | None
    categories: list[CategoryTotal] = field(default_factory=list)
    transaction_count: int = 0


def month_of(d: date) -> str:
    return d.strftime("%Y-%m")


def shift_month(month: str, delta: int) -> str:
    year, mon = (int(x) for x in month.split("-"))
    idx = year * 12 + (mon - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_label(month: str) -> str:
    year, mon = month.split("-")
    return f"{int(year)}年{int(mon)}月"


def transactions_in_month(transactions: list[Transaction], month: str) -> list[Transaction]:
    return [t for t in transactions if month_of(t.usage_date) == month]


def total_spending(transactions: list[Transaction]) -> int:
    return int(sum(t.amount for t in transactions))


def category_rows(transactions: list[Transaction], allocations: list[Allocation]) -> pd.DataFrame:
    """カテゴリ集計用の行を返す。allocation がある明細は allocation 行に置き換える。"""
    alloc_by_tx: dict[str, list[Allocation]] = {}
    for a in allocations:
        alloc_by_tx.setdefault(a.transaction_id, []).append(a)
    records = []
    for t in transactions:
        allocs = alloc_by_tx.get(t.id)
        if allocs:
            for a in allocs:
                records.append({"transaction_id": t.id, "category": a.category or "その他", "amount": a.amount})
        else:
            records.append({"transaction_id": t.id, "category": t.category or "未分類", "amount": t.amount})
    return pd.DataFrame(records, columns=["transaction_id", "category", "amount"])


def category_totals(
    transactions: list[Transaction], allocations: list[Allocation], category_order: list[str]
) -> list[CategoryTotal]:
    df = category_rows(transactions, allocations)
    if df.empty:
        return []
    grouped = df.groupby("category").agg(amount=("amount", "sum"), count=("transaction_id", "nunique"))
    total = int(df["amount"].sum())
    order = {c: i for i, c in enumerate(category_order)}
    items = []
    for cat, row in grouped.iterrows():
        amount = int(row["amount"])
        items.append(
            CategoryTotal(
                category=str(cat),
                amount=amount,
                ratio=(amount / total) if total else 0.0,
                count=int(row["count"]),
            )
        )
    items.sort(key=lambda c: (-c.amount, order.get(c.category, 999)))
    return items


def monthly_summary(data: LedgerData, month: str) -> MonthlySummary:
    current = transactions_in_month(data.transactions, month)
    previous = transactions_in_month(data.transactions, shift_month(month, -1))
    total = total_spending(current)
    prev_total = total_spending(previous) if previous else None
    diff = total - prev_total if prev_total is not None else None
    diff_ratio = (diff / prev_total) if prev_total else None
    return MonthlySummary(
        month=month,
        total=total,
        prev_total=prev_total,
        diff=diff,
        diff_ratio=diff_ratio,
        categories=category_totals(current, data.allocations, data.category_names()),
        transaction_count=len(current),
    )


def available_months(transactions: list[Transaction]) -> list[str]:
    return sorted({month_of(t.usage_date) for t in transactions}, reverse=True)


def monthly_trend(data: LedgerData, months: int = 6, end_month: str | None = None) -> list[tuple[str, int]]:
    end_month = end_month or (available_months(data.transactions) or [date.today().strftime("%Y-%m")])[0]
    result = []
    for i in range(months - 1, -1, -1):
        m = shift_month(end_month, -i)
        result.append((m, total_spending(transactions_in_month(data.transactions, m))))
    return result
