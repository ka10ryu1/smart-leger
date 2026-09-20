from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.models import Allocation, Category, LedgerData, Transaction, DEFAULT_CATEGORIES
from smart_ledger.services.aggregation import monthly_summary, shift_month, total_spending, transactions_in_month
from smart_ledger.services.allocations import AllocationError, AllocationInput, validate_allocations

CATS = list(DEFAULT_CATEGORIES)


def tx(id_, d, amount, category, merchant="店"):
    return Transaction(
        id=id_, usage_date=d, merchant_raw=merchant, merchant_normalized=merchant, amount=amount, category=category
    )


def ledger(*txs, allocations=()):
    data = LedgerData(transactions=list(txs), allocations=list(allocations))
    data.categories = [Category(c, i) for i, c in enumerate(CATS)]
    return data


def test_monthly_aggregation_uses_usage_date():
    data = ledger(
        tx("a", date(2026, 8, 31), 1000, "食費"),
        tx("b", date(2026, 9, 1), 2000, "食費"),
        tx("c", date(2026, 9, 30), 3000, "外食"),
    )
    assert total_spending(transactions_in_month(data.transactions, "2026-08")) == 1000
    assert total_spending(transactions_in_month(data.transactions, "2026-09")) == 5000
    s = monthly_summary(data, "2026-09")
    assert s.total == 5000
    assert s.prev_total == 1000
    assert s.diff == 4000


def test_shift_month():
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2026-12", 1) == "2027-01"


def test_allocations_are_not_double_counted():
    kyash = tx("k", date(2026, 8, 15), 10000, "その他", "KYASH")
    other = tx("o", date(2026, 8, 16), 500, "食費")
    allocs = [
        Allocation("k", "食費", 3500),
        Allocation("k", "日用品・買い物", 4000),
        Allocation("k", "外食", 2500),
    ]
    s = monthly_summary(ledger(kyash, other, allocations=allocs), "2026-08")
    assert s.total == 10500  # transactions.amount を 1 回だけ
    by_cat = {c.category: c.amount for c in s.categories}
    assert by_cat == {"食費": 4000, "日用品・買い物": 4000, "外食": 2500}
    assert sum(by_cat.values()) == s.total
    assert "その他" not in by_cat  # 元明細のカテゴリは集計に出ない
    assert abs(sum(c.ratio for c in s.categories) - 1.0) < 1e-9


def test_transaction_without_allocation_uses_own_category():
    s = monthly_summary(ledger(tx("a", date(2026, 8, 1), 700, "通信")), "2026-08")
    assert [(c.category, c.amount) for c in s.categories] == [("通信", 700)]


def test_validate_allocations_sum_must_match():
    t = tx("k", date(2026, 8, 15), 10000, "その他")
    ok = validate_allocations(t, [AllocationInput("食費", 6000), AllocationInput("外食", 4000)], CATS)
    assert len(ok) == 2 and all(a.transaction_id == "k" for a in ok)
    with pytest.raises(AllocationError, match="一致しません"):
        validate_allocations(t, [AllocationInput("食費", 6000), AllocationInput("外食", 3000)], CATS)


def test_validate_allocations_rejects_unknown_category_and_empty():
    t = tx("k", date(2026, 8, 15), 100, "その他")
    with pytest.raises(AllocationError):
        validate_allocations(t, [AllocationInput("宇宙", 100)], CATS)
    with pytest.raises(AllocationError):
        validate_allocations(t, [AllocationInput("", 100)], CATS)
    assert validate_allocations(t, [], CATS) == []
