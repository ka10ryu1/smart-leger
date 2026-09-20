"""月次集計と内訳（allocations）のテスト"""

from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.models import Allocation, Category, LedgerData, Transaction
from smart_ledger.services.aggregation import monthly_summary, shift_month, total_spending, transactions_in_month
from smart_ledger.services.allocations import AllocationError, AllocationInput, validate_allocations


def make_tx(id_: str, d: date, amount: int, category: str, merchant: str = '店') -> Transaction:
    """テスト用の明細を作る

    Args:
        id_: 明細 ID
        d: 利用日
        amount: 金額
        category: カテゴリ
        merchant: 加盟店名
    """
    return Transaction(
        id=id_, usage_date=d, merchant_raw=merchant, merchant_normalized=merchant, amount=amount, category=category
    )


def make_ledger(
    txs: list[Transaction], categories: list[str], allocations: list[Allocation] | None = None
) -> LedgerData:
    """テスト用の LedgerData を作る

    Args:
        txs: 明細
        categories: カテゴリ名
        allocations: 内訳（None なら無し）
    """
    data = LedgerData(transactions=list(txs), allocations=list(allocations or []))
    data.categories = [Category(c, i) for i, c in enumerate(categories)]
    return data


def test_monthly_aggregation_uses_usage_date(categories: list[str]) -> None:
    """月次集計は利用日基準で、前月比も計算される

    Args:
        categories: 初期カテゴリ
    """
    data = make_ledger(
        [
            make_tx('a', date(2026, 8, 31), 1000, '食費'),
            make_tx('b', date(2026, 9, 1), 2000, '食費'),
            make_tx('c', date(2026, 9, 30), 3000, '外食'),
        ],
        categories,
    )
    assert total_spending(transactions_in_month(data.transactions, '2026-08')) == 1000
    assert total_spending(transactions_in_month(data.transactions, '2026-09')) == 5000

    summary = monthly_summary(data, '2026-09')
    assert summary.total == 5000
    assert summary.prev_total == 1000
    assert summary.diff == 4000


def test_shift_month() -> None:
    """年をまたぐ月のシフト"""
    assert shift_month('2026-01', -1) == '2025-12'
    assert shift_month('2026-12', 1) == '2027-01'


def test_allocations_are_not_double_counted(categories: list[str]) -> None:
    """内訳がある明細は総支出で 1 回、カテゴリ別では内訳で集計される

    Args:
        categories: 初期カテゴリ
    """
    kyash = make_tx('k', date(2026, 8, 15), 10000, 'その他', 'KYASH')
    other = make_tx('o', date(2026, 8, 16), 500, '食費')
    allocs = [
        Allocation('k', '食費', 3500),
        Allocation('k', '日用品・買い物', 4000),
        Allocation('k', '外食', 2500),
    ]
    summary = monthly_summary(make_ledger([kyash, other], categories, allocs), '2026-08')
    assert summary.total == 10500

    by_cat = {c.category: c.amount for c in summary.categories}
    assert by_cat == {'食費': 4000, '日用品・買い物': 4000, '外食': 2500}
    assert sum(by_cat.values()) == summary.total
    assert 'その他' not in by_cat
    assert abs(sum(c.ratio for c in summary.categories) - 1.0) < 1e-9


def test_transaction_without_allocation_uses_own_category(categories: list[str]) -> None:
    """内訳が無い明細は自身のカテゴリで集計される

    Args:
        categories: 初期カテゴリ
    """
    summary = monthly_summary(make_ledger([make_tx('a', date(2026, 8, 1), 700, '通信')], categories), '2026-08')
    assert [(c.category, c.amount) for c in summary.categories] == [('通信', 700)]


def test_validate_allocations_sum_must_match(categories: list[str]) -> None:
    """内訳の合計が明細金額と一致しなければ AllocationError

    Args:
        categories: 初期カテゴリ
    """
    tx = make_tx('k', date(2026, 8, 15), 10000, 'その他')
    ok = validate_allocations(tx, [AllocationInput('食費', 6000), AllocationInput('外食', 4000)], categories)
    assert len(ok) == 2 and all(a.transaction_id == 'k' for a in ok)
    with pytest.raises(AllocationError, match='一致しません'):
        validate_allocations(tx, [AllocationInput('食費', 6000), AllocationInput('外食', 3000)], categories)


def test_validate_allocations_rejects_unknown_category_and_empty(categories: list[str]) -> None:
    """不明なカテゴリ・未選択は AllocationError、全て空なら空リスト

    Args:
        categories: 初期カテゴリ
    """
    tx = make_tx('k', date(2026, 8, 15), 100, 'その他')
    with pytest.raises(AllocationError):
        validate_allocations(tx, [AllocationInput('宇宙', 100)], categories)

    with pytest.raises(AllocationError):
        validate_allocations(tx, [AllocationInput('', 100)], categories)

    assert validate_allocations(tx, [], categories) == []


def test_validate_allocations_rejects_zero_amount_row(categories: list[str]) -> None:
    """0 円の内訳行は合計が一致しても AllocationError

    Args:
        categories: 初期カテゴリ
    """
    tx = make_tx('k', date(2026, 8, 15), 100, 'その他')
    with pytest.raises(AllocationError, match='0'):
        validate_allocations(tx, [AllocationInput('食費', 100), AllocationInput('外食', 0, 'メモ')], categories)
