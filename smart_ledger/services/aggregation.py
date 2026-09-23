"""月次・年間集計（usage_date 基準）

- 支出と収入（kind）は必ず分けて集計する。総支出に収入を混ぜない
- 月間総支出: kind=expense の transactions.amount を 1 回だけ合計
- 収支: 収入合計 − 総支出
- カテゴリ別: allocations がある明細は allocations を、無い明細は transactions.category を使う
  → 二重計上しない（年間表でも同じ規則）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants import FALLBACK_CATEGORY, UNCLASSIFIED_LABEL
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
    """1 か月分の集計結果（total / categories は支出、income / income_categories は収入）"""

    month: str
    total: int
    prev_total: int | None
    diff: int | None
    diff_ratio: float | None
    categories: list[CategoryTotal] = field(default_factory=list)
    transaction_count: int = 0  # 支出の件数（総支出の内訳として表示する）
    income: int = 0
    income_count: int = 0
    income_categories: list[CategoryTotal] = field(default_factory=list)

    @property
    def balance(self) -> int:
        """収支（収入 − 総支出）"""
        return self.income - self.total


@dataclass
class AnnualRow:
    """年間表の 1 行（カテゴリ別の月別金額）"""

    category: str
    amounts: list[int]  # 1〜12 月の順
    total: int


@dataclass
class AnnualTable:
    """1 年分のカテゴリ × 月のマトリクス（月間総支出は明細金額、カテゴリ別は内訳を使う）

    支出（rows / monthly_totals / total）と収入（income_rows / monthly_incomes / income_total）を分けて持つ
    """

    year: int
    months: list[str]  # 'YYYY-MM' を 1〜12 月の順
    rows: list[AnnualRow]
    monthly_totals: list[int]
    monthly_counts: list[int]  # 支出の件数（明細のある月の数え上げと月平均の分母に使う）
    total: int
    income_rows: list[AnnualRow] = field(default_factory=list)
    monthly_incomes: list[int] = field(default_factory=list)
    income_total: int = 0

    @property
    def monthly_balances(self) -> list[int]:
        """月ごとの収支（収入 − 総支出）を monthly_totals と同じ長さで返す（収入が無い年は総支出の符号を反転した値）"""
        incomes = self.monthly_incomes or [0] * len(self.monthly_totals)  # 既定の空リストで zip が全部落ちないように
        return [income - total for income, total in zip(incomes, self.monthly_totals)]

    @property
    def balance(self) -> int:
        """年間の収支（収入 − 総支出）"""
        return self.income_total - self.total


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
    return [t for t in transactions if t.month == month]


def split_by_kind(transactions: list[Transaction]) -> tuple[list[Transaction], list[Transaction]]:
    """明細を支出と収入に分ける

    Args:
        transactions: 明細

    Returns:
        (支出の明細, 収入の明細)
    """
    expenses = [t for t in transactions if not t.is_income]
    incomes = [t for t in transactions if t.is_income]
    return expenses, incomes


def total_spending(transactions: list[Transaction]) -> int:
    """支出の明細金額の合計（収入と内訳は見ない）

    Args:
        transactions: 明細（収入を含んでいてもよい）
    """
    return int(sum(t.amount for t in transactions if not t.is_income))


def total_income(transactions: list[Transaction]) -> int:
    """収入の明細金額の合計（内訳は見ない）

    Args:
        transactions: 明細（支出を含んでいてもよい）
    """
    return int(sum(t.amount for t in transactions if t.is_income))


def category_rows(transactions: list[Transaction], allocations: list[Allocation]) -> list[tuple[str, str, int]]:
    """カテゴリ集計用の行を返す（allocation がある明細は allocation 行に置き換える）

    Args:
        transactions: 明細
        allocations: 内訳

    Returns:
        (transaction_id, category, amount) のタプルのリスト
    """
    alloc_by_tx: dict[str, list[Allocation]] = {}
    for a in allocations:
        alloc_by_tx.setdefault(a.transaction_id, []).append(a)

    rows: list[tuple[str, str, int]] = []
    for t in transactions:
        allocs = alloc_by_tx.get(t.id)
        if allocs:
            rows.extend((t.id, a.category or FALLBACK_CATEGORY, a.amount) for a in allocs)
        else:
            rows.append((t.id, t.category or UNCLASSIFIED_LABEL, t.amount))

    return rows


def category_totals(
    transactions: list[Transaction], allocations: list[Allocation], category_order: list[str]
) -> list[CategoryTotal]:
    """カテゴリ別の金額・割合・件数を金額の大きい順に返す

    Args:
        transactions: 明細
        allocations: 内訳
        category_order: 同額のときの並び順に使うカテゴリ順
    """
    rows = category_rows(transactions, allocations)
    total = sum(amount for _, _, amount in rows)
    amounts: dict[str, int] = {}
    tx_ids: dict[str, set[str]] = {}
    for tx_id, category, amount in rows:
        amounts[category] = amounts.get(category, 0) + amount
        tx_ids.setdefault(category, set()).add(tx_id)

    order = {c: i for i, c in enumerate(category_order)}
    items = [
        CategoryTotal(category=cat, amount=amt, ratio=(amt / total) if total else 0.0, count=len(tx_ids[cat]))
        for cat, amt in amounts.items()
    ]
    items.sort(key=lambda c: (-c.amount, order.get(c.category, 999)))
    return items


def monthly_summary(data: LedgerData, month: str) -> MonthlySummary:
    """月間総支出・前月比・カテゴリ別集計・収入をまとめる（前月比は支出どうしで比べる）

    Args:
        data: 全データ
        month: 対象の 'YYYY-MM'
    """
    current = transactions_in_month(data.transactions, month)
    expenses, incomes = split_by_kind(current)
    prev_expenses, _ = split_by_kind(transactions_in_month(data.transactions, shift_month(month, -1)))
    total = total_spending(expenses)
    prev_total = total_spending(prev_expenses) if prev_expenses else None  # 前月が収入だけなら「前月のデータなし」
    diff: int | None = None
    diff_ratio: float | None = None
    if prev_total is not None:
        diff = total - prev_total
        if prev_total:
            diff_ratio = diff / prev_total

    names = data.category_names()
    return MonthlySummary(
        month=month,
        total=total,
        prev_total=prev_total,
        diff=diff,
        diff_ratio=diff_ratio,
        categories=category_totals(expenses, data.allocations, names),
        transaction_count=len(expenses),
        income=total_income(incomes),
        income_count=len(incomes),
        income_categories=category_totals(incomes, data.allocations, names),
    )


def available_months(transactions: list[Transaction]) -> list[str]:
    """明細が存在する年月を新しい順に返す

    Args:
        transactions: 明細
    """
    return sorted({t.month for t in transactions}, reverse=True)


def monthly_trend(data: LedgerData, end_month: str, months: int = 6) -> list[tuple[str, int]]:
    """end_month までの月別総支出を古い順に返す

    Args:
        data: 全データ
        end_month: 最後の月（'YYYY-MM'）
        months: 何か月分返すか
    """
    result = []
    for i in range(months - 1, -1, -1):
        m = shift_month(end_month, -i)
        result.append((m, total_spending(transactions_in_month(data.transactions, m))))

    return result


def available_years(transactions: list[Transaction]) -> list[int]:
    """明細が存在する年を新しい順に返す

    Args:
        transactions: 明細
    """
    return sorted({t.usage_date.year for t in transactions}, reverse=True)


def month_matrix(
    transactions: list[Transaction], allocations: list[Allocation], months: list[str]
) -> tuple[dict[str, list[int]], list[int], list[int]]:
    """カテゴリ × 月の金額表と、月ごとの明細金額合計・明細件数を作る

    Args:
        transactions: 集計する明細（支出だけ・収入だけのどちらかを渡す）
        allocations: 内訳
        months: 'YYYY-MM' を月順に並べたもの

    Returns:
        (カテゴリ名 → 月別金額のリスト, 月別の明細金額合計, 月別の明細件数)
    """
    amounts: dict[str, list[int]] = {}
    totals: list[int] = []
    counts: list[int] = []
    for idx, month in enumerate(months):
        txs = transactions_in_month(transactions, month)
        totals.append(int(sum(t.amount for t in txs)))
        counts.append(len(txs))
        for _, category, amount in category_rows(txs, allocations):
            amounts.setdefault(category, [0] * len(months))[idx] += amount

    return amounts, totals, counts


def annual_rows(amounts: dict[str, list[int]], category_order: list[str]) -> list[AnnualRow]:
    """カテゴリ × 月の金額表を AnnualRow のリストにする（sort_order 順、未登録のカテゴリは末尾）

    Args:
        amounts: カテゴリ名 → 月別金額のリスト
        category_order: 並び順に使うカテゴリ順
    """
    order = {c: i for i, c in enumerate(category_order)}
    rows = [AnnualRow(category=cat, amounts=vals, total=sum(vals)) for cat, vals in amounts.items()]
    rows.sort(key=lambda r: (order.get(r.category, len(order)), r.category))
    return rows


def annual_table(data: LedgerData, year: int) -> AnnualTable:
    """カテゴリ × 月の年間表を作る（その年に明細か内訳があるカテゴリだけを sort_order 順に並べ、未登録のカテゴリは末尾）

    支出と収入は別の表にする（加盟店ルールで同じカテゴリ名が両方に出ることもあるが、混ぜて合計はしない）

    Args:
        data: 全データ
        year: 対象の年
    """
    months = [f'{year:04d}-{m:02d}' for m in range(1, 13)]
    in_year = [t for t in data.transactions if t.usage_date.year == year]  # 月ごとの走査を対象年だけに絞る
    expenses, incomes = split_by_kind(in_year)
    expense_amounts, monthly_totals, monthly_counts = month_matrix(expenses, data.allocations, months)
    income_amounts, monthly_incomes, _ = month_matrix(incomes, data.allocations, months)
    names = data.category_names()
    return AnnualTable(
        year=year,
        months=months,
        rows=annual_rows(expense_amounts, names),
        monthly_totals=monthly_totals,
        monthly_counts=monthly_counts,
        total=sum(monthly_totals),
        income_rows=annual_rows(income_amounts, names),
        monthly_incomes=monthly_incomes,
        income_total=sum(monthly_incomes),
    )
