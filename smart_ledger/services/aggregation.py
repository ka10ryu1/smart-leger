"""月次集計（usage_date 基準）

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
    """カテゴリ別の集計値"""

    category: str
    amount: int
    ratio: float
    count: int


@dataclass
class MonthlySummary:
    """1 か月分の集計結果"""

    month: str
    total: int
    prev_total: int | None
    diff: int | None
    diff_ratio: float | None
    categories: list[CategoryTotal] = field(default_factory=list)
    transaction_count: int = 0


def month_of(d: date) -> str:
    """日付を 'YYYY-MM' にする

    Args:
        d: 日付
    """
    return d.strftime('%Y-%m')


def shift_month(month: str, delta: int) -> str:
    """'YYYY-MM' を delta か月ずらす

    Args:
        month: 基準の年月
        delta: ずらす月数（負なら過去）
    """
    year, mon = (int(x) for x in month.split('-'))
    idx = year * 12 + (mon - 1) + delta
    return f'{idx // 12:04d}-{idx % 12 + 1:02d}'


def month_label(month: str) -> str:
    """'YYYY-MM' を '2026年9月' 形式にする

    Args:
        month: 年月
    """
    year, mon = month.split('-')
    return f'{int(year)}年{int(mon)}月'


def transactions_in_month(transactions: list[Transaction], month: str) -> list[Transaction]:
    """利用日がその月に含まれる明細を返す

    Args:
        transactions: 明細
        month: 'YYYY-MM'
    """
    return [t for t in transactions if month_of(t.usage_date) == month]


def total_spending(transactions: list[Transaction]) -> int:
    """明細金額の合計（内訳は見ない）

    Args:
        transactions: 明細
    """
    return int(sum(t.amount for t in transactions))


def category_rows(
    transactions: list[Transaction],
    allocations: list[Allocation],
    unclassified_label: str = '未分類',
    fallback_category: str = 'その他',
) -> pd.DataFrame:
    """カテゴリ集計用の行を返す（allocation がある明細は allocation 行に置き換える）

    Args:
        transactions: 明細
        allocations: 内訳
        unclassified_label: カテゴリ未設定の明細に付けるラベル
        fallback_category: カテゴリ未設定の内訳に付けるカテゴリ

    Returns:
        transaction_id / category / amount の 3 列の DataFrame
    """
    alloc_by_tx: dict[str, list[Allocation]] = {}
    for a in allocations:
        alloc_by_tx.setdefault(a.transaction_id, []).append(a)

    records = []
    for t in transactions:
        allocs = alloc_by_tx.get(t.id)
        if allocs:
            for a in allocs:
                records.append(
                    {'transaction_id': t.id, 'category': a.category or fallback_category, 'amount': a.amount}
                )
        else:
            records.append({'transaction_id': t.id, 'category': t.category or unclassified_label, 'amount': t.amount})

    return pd.DataFrame(records, columns=['transaction_id', 'category', 'amount'])


def category_totals(
    transactions: list[Transaction], allocations: list[Allocation], category_order: list[str]
) -> list[CategoryTotal]:
    """カテゴリ別の金額・割合・件数を金額の大きい順に返す

    Args:
        transactions: 明細
        allocations: 内訳
        category_order: 同額のときの並び順に使うカテゴリ順
    """
    df = category_rows(transactions, allocations)
    if df.empty:
        return []

    grouped = df.groupby('category').agg(amount=('amount', 'sum'), count=('transaction_id', 'nunique'))
    total = int(df['amount'].sum())
    order = {c: i for i, c in enumerate(category_order)}
    items = []
    for cat, row in grouped.iterrows():
        amount = int(row['amount'])
        items.append(
            CategoryTotal(
                category=str(cat),
                amount=amount,
                ratio=(amount / total) if total else 0.0,
                count=int(row['count']),
            )
        )

    items.sort(key=lambda c: (-c.amount, order.get(c.category, 999)))
    return items


def monthly_summary(data: LedgerData, month: str) -> MonthlySummary:
    """月間総支出・前月比・カテゴリ別集計をまとめる

    Args:
        data: 全データ
        month: 対象の 'YYYY-MM'
    """
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
    """明細が存在する年月を新しい順に返す

    Args:
        transactions: 明細
    """
    return sorted({month_of(t.usage_date) for t in transactions}, reverse=True)


def monthly_trend(data: LedgerData, months: int = 6, end_month: str | None = None) -> list[tuple[str, int]]:
    """end_month までの月別総支出を古い順に返す

    Args:
        data: 全データ
        months: 何か月分返すか
        end_month: 最後の月（None なら最新の明細がある月、それも無ければ今月）
    """
    end_month = end_month or (available_months(data.transactions) or [date.today().strftime('%Y-%m')])[0]
    result = []
    for i in range(months - 1, -1, -1):
        m = shift_month(end_month, -i)
        result.append((m, total_spending(transactions_in_month(data.transactions, m))))

    return result
