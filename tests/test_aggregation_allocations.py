"""月次・年間集計と内訳（allocations）のテスト"""

from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.models import Allocation, Category, LedgerData, Transaction
from smart_ledger.services.aggregation import (
    AnnualTable,
    annual_table,
    available_years,
    monthly_summary,
    monthly_trend,
    shift_month,
    total_income,
    total_spending,
    transactions_in_month,
)
from smart_ledger.services.allocations import (
    AllocationError,
    AllocationInput,
    copy_previous_allocations,
    previous_allocation_source,
    validate_allocations,
)


def make_tx(id_: str, d: date, amount: int, category: str, merchant: str = '店', kind: str = 'expense') -> Transaction:
    """テスト用の明細を作る

    Args:
        id_: 明細 ID
        d: 利用日
        amount: 金額
        category: カテゴリ
        merchant: 加盟店名
        kind: expense / income
    """
    return Transaction(
        id=id_,
        usage_date=d,
        merchant_raw=merchant,
        merchant_normalized=merchant,
        amount=amount,
        category=category,
        kind=kind,
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


def make_kyash_ledger(categories: list[str]) -> LedgerData:
    """前回の内訳の複写用に、請求月だけ違う KYASH の明細 4 件と別の加盟店の明細を持つ LedgerData を作る

    7 月・8 月の KYASH と 8 月の別の加盟店には内訳があり、9 月の KYASH（複写先）と 10 月の KYASH には無い

    Args:
        categories: 初期カテゴリ
    """
    txs = [
        make_tx('jul', date(2026, 7, 27), 9000, '食費', 'KYASH 7ガツブン'),
        make_tx('aug', date(2026, 8, 27), 10000, '食費', 'kyash 8ガツブン'),
        make_tx('other', date(2026, 8, 28), 5000, '食費', 'SUPER'),
        make_tx('sep', date(2026, 9, 27), 10500, '食費', 'KYASH 9ガツブン'),
        make_tx('oct', date(2026, 10, 27), 9500, '食費', 'KYASH 10ガツブン'),
    ]
    allocations = [
        Allocation('jul', '食費', 9000),
        Allocation('aug', '食費', 6000, 'スーパー'),
        Allocation('aug', '外食', 4000, 'ランチ'),
        Allocation('other', '日用品・買い物', 5000),
    ]
    return make_ledger(txs, categories, allocations)


def test_previous_allocation_source_picks_latest_earlier_same_merchant(categories: list[str]) -> None:
    """複写元は同じ加盟店キー（請求月・大文字小文字を無視）で内訳があり、利用日が以前の明細のうち最新のもの

    Args:
        categories: 初期カテゴリ
    """
    data = make_kyash_ledger(categories)

    def source_id(tx_id: str) -> str | None:
        """tx_id の明細の複写元の ID を返す

        Args:
            tx_id: 複写先の明細 ID
        """
        tx = data.find_transaction(tx_id)
        if tx is None:
            raise AssertionError(tx_id)

        source = previous_allocation_source(data, tx)
        return source.id if source else None

    assert source_id('sep') == 'aug'  # 別の加盟店（8/28）や内訳の無い後の明細（10 月）は選ばない
    assert source_id('oct') == 'aug'  # 内訳の無い 9 月は飛ばす
    assert source_id('aug') == 'jul'  # 自分自身は除く
    assert source_id('jul') is None  # より前に内訳のある同じ加盟店が無い

    data.transactions.append(make_tx('aug2', date(2026, 8, 27), 10000, '食費', 'KYASH 8ガツブン'))
    data.allocations.append(Allocation('aug2', '交通', 10000))
    assert source_id('sep') == 'aug2'  # 利用日と取込日時が同じなら後ろの行

    data.transactions[0].imported_at = '2026-09-01 10:00:00'  # jul の位置に 8/27 の明細を後から取り込んだ扱いにする
    data.transactions[0].usage_date = date(2026, 8, 27)
    assert source_id('sep') == 'jul'  # 利用日が同じなら行の位置より取込日時が新しいものを優先する


def test_copy_previous_allocations_moves_difference_to_last_row(categories: list[str]) -> None:
    """前回の内訳を順序・メモごと複写し、金額の差額は最後の行に寄せる（元の内訳は変えない）

    Args:
        categories: 初期カテゴリ
    """
    data = make_kyash_ledger(categories)
    sep = data.find_transaction('sep')
    if sep is None:
        raise AssertionError('sep')

    copied = copy_previous_allocations(data, sep)
    if copied is None:
        raise AssertionError('copy')

    assert copied.source.id == 'aug' and copied.difference == 500 and not copied.last_row_flipped
    assert copied.items == [AllocationInput('食費', 6000, 'スーパー'), AllocationInput('外食', 4500, 'ランチ')]
    assert validate_allocations(sep, copied.items, categories)  # そのまま保存できる
    assert [a.amount for a in data.allocations_for('aug')] == [6000, 4000]

    sep.amount = 10000  # 同じ金額なら差額なし
    same = copy_previous_allocations(data, sep)
    assert same is not None and same.difference == 0 and [i.amount for i in same.items] == [6000, 4000]

    sep.amount = 3000  # 最後の行が明細と逆の符号になる場合もそのまま返し、見直しの印を付ける
    smaller = copy_previous_allocations(data, sep)
    assert smaller is not None and [i.amount for i in smaller.items] == [6000, -3000] and smaller.last_row_flipped

    sep.amount = 6000  # 最後の行が 0 円になる場合も同じ
    zero = copy_previous_allocations(data, sep)
    assert zero is not None and [i.amount for i in zero.items] == [6000, 0] and zero.last_row_flipped

    jul = data.find_transaction('jul')
    if jul is None:
        raise AssertionError('jul')

    assert copy_previous_allocations(data, jul) is None


def test_previous_allocations_match_sign_and_kind(categories: list[str]) -> None:
    """返金（マイナス金額）は返金どうし、収入は収入どうしで複写元を選び、返金の正しい複写には見直しの印を付けない

    Args:
        categories: 初期カテゴリ
    """
    data = make_kyash_ledger(categories)
    data.transactions += [
        make_tx('ref_aug', date(2026, 8, 30), -3000, '食費', 'KYASH 8ガツブン'),
        make_tx('inc_aug', date(2026, 8, 31), 2000, '食費', 'KYASH 8ガツブン', kind='income'),
        make_tx('ref_sep', date(2026, 9, 29), -2500, '食費', 'KYASH 9ガツブン'),
        make_tx('inc_sep', date(2026, 9, 30), 2500, '食費', 'KYASH 9ガツブン', kind='income'),
    ]
    data.allocations += [
        Allocation('ref_aug', '食費', -1000),
        Allocation('ref_aug', '外食', -2000),
        Allocation('inc_aug', 'その他', 2000),
    ]

    def copy_of(tx_id: str) -> tuple[str, list[int], bool]:
        """tx_id の明細に複写した結果の（複写元 ID・金額の並び・見直しの印）を返す

        Args:
            tx_id: 複写先の明細 ID
        """
        tx = data.find_transaction(tx_id)
        if tx is None:
            raise AssertionError(tx_id)

        copied = copy_previous_allocations(data, tx)
        if copied is None:
            raise AssertionError(f'no source: {tx_id}')

        return copied.source.id, [i.amount for i in copied.items], copied.last_row_flipped

    assert copy_of('sep') == ('aug', [6000, 4500], False)  # より新しい返金・収入があっても通常の支出から写す
    assert copy_of('ref_sep') == ('ref_aug', [-1000, -1500], False)  # 返金は返金から写し、正しい結果なら印なし
    assert copy_of('inc_sep') == ('inc_aug', [2500], False)  # 収入は収入から写す


def test_annual_table_matrix_and_totals(categories: list[str]) -> None:
    """年間表は返金も含めてカテゴリ別金額を並べ、月間総支出は明細金額を 1 回だけ数える

    Args:
        categories: 初期カテゴリ
    """
    kyash = make_tx('k', date(2026, 3, 15), 10000, 'その他', 'KYASH')
    data = make_ledger(
        [
            make_tx('a', date(2026, 1, 5), 1000, '外食'),
            make_tx('b', date(2026, 1, 20), 2000, '食費'),
            make_tx('c', date(2026, 12, 31), 3000, '食費'),
            make_tx('refund', date(2026, 12, 15), -500, '食費'),
            make_tx('d', date(2025, 12, 31), 9999, '食費'),  # 前年は含まれない
            make_tx('e', date(2026, 6, 1), 500, ''),  # 未分類は末尾
            kyash,
        ],
        categories,
        [Allocation('k', '食費', 6000), Allocation('k', '交通', 4000)],
    )
    table = annual_table(data, 2026)
    assert table.year == 2026
    assert table.months[0] == '2026-01' and table.months[-1] == '2026-12'
    assert [r.category for r in table.rows] == ['食費', '外食', '交通', '未分類']
    by_cat = {r.category: r for r in table.rows}
    assert by_cat['食費'].amounts == [2000, 0, 6000, 0, 0, 0, 0, 0, 0, 0, 0, 2500]
    assert by_cat['食費'].total == 10500
    assert by_cat['交通'].amounts[2] == 4000
    assert 'その他' not in by_cat  # 内訳のある明細は自身のカテゴリでは数えない
    assert table.monthly_totals == [3000, 0, 10000, 0, 0, 500, 0, 0, 0, 0, 0, 2500]
    assert table.monthly_counts == [2, 0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 2]
    assert table.total == 16000 == sum(r.total for r in table.rows)


def test_annual_table_empty_year_and_available_years(categories: list[str]) -> None:
    """明細が無い年は行が空で合計 0、available_years は新しい順

    Args:
        categories: 初期カテゴリ
    """
    data = make_ledger(
        [make_tx('a', date(2024, 5, 1), 100, '食費'), make_tx('b', date(2026, 1, 1), 100, '食費')], categories
    )
    assert available_years(data.transactions) == [2026, 2024]
    assert available_years([]) == []
    table = annual_table(data, 2025)
    assert table.rows == [] and table.total == 0 and table.monthly_totals == [0] * 12


def test_income_is_excluded_from_spending(categories: list[str]) -> None:
    """収入は総支出・前月比・推移に含めず、収入と収支として別に集計する

    Args:
        categories: 初期カテゴリ
    """
    data = make_ledger(
        [
            make_tx('a', date(2026, 8, 10), 1000, '食費'),
            make_tx('b', date(2026, 9, 10), 3000, '食費'),
            make_tx('c', date(2026, 9, 5), 7000, '売電収入', '振込*トウデンPG コウニユウ', kind='income'),
        ],
        categories,
    )
    current = transactions_in_month(data.transactions, '2026-09')
    assert total_spending(current) == 3000 and total_income(current) == 7000

    summary = monthly_summary(data, '2026-09')
    assert (summary.total, summary.income, summary.balance) == (3000, 7000, 4000)
    assert summary.prev_total == 1000 and summary.diff == 2000  # 前月比は支出どうしで比べる
    assert (summary.transaction_count, summary.income_count) == (1, 1)  # 総支出の件数に収入は混ぜない
    assert [c.category for c in summary.categories] == ['食費']
    assert [(c.category, c.amount) for c in summary.income_categories] == [('売電収入', 7000)]
    assert dict(monthly_trend(data, '2026-09', months=2)) == {'2026-08': 1000, '2026-09': 3000}


def test_annual_table_separates_income_rows(categories: list[str]) -> None:
    """年間表は収入を別の行に分け、月間総支出に混ぜない

    Args:
        categories: 初期カテゴリ
    """
    data = make_ledger(
        [
            make_tx('a', date(2026, 2, 10), 5000, '住宅ローン'),
            make_tx('b', date(2026, 3, 10), 5000, '住宅ローン'),
            make_tx('c', date(2026, 2, 5), 6000, '売電収入', kind='income'),
            make_tx('d', date(2026, 3, 5), 7000, '売電収入', kind='income'),
        ],
        categories,
    )
    table = annual_table(data, 2026)
    assert [r.category for r in table.rows] == ['住宅ローン']
    assert [r.category for r in table.income_rows] == ['売電収入']
    assert table.monthly_totals[1:3] == [5000, 5000] and table.total == 10000
    assert table.monthly_incomes[1:3] == [6000, 7000] and table.income_total == 13000
    assert table.monthly_balances[1:3] == [1000, 2000] and table.balance == 3000
    assert table.monthly_counts[1:3] == [1, 1]  # 件数（月平均の分母）は支出だけを数える


def test_income_only_previous_month_counts_as_no_data(categories: list[str]) -> None:
    """前月に収入しか無い場合は「前月のデータなし」扱いで、0 円との比較にしない

    Args:
        categories: 初期カテゴリ
    """
    data = make_ledger(
        [
            make_tx('a', date(2026, 8, 5), 7000, '売電収入', kind='income'),
            make_tx('b', date(2026, 9, 10), 3000, '食費'),
        ],
        categories,
    )
    summary = monthly_summary(data, '2026-09')
    assert summary.prev_total is None and summary.diff is None
    assert (summary.total, summary.transaction_count, summary.income_count) == (3000, 1, 0)


def test_monthly_balances_length_matches_months_without_income() -> None:
    """収入を持たない AnnualTable でも月ごとの収支は 12 か月分そろい、年間の収支と符合する"""
    table = AnnualTable(
        year=2026,
        months=[f'2026-{m:02d}' for m in range(1, 13)],
        rows=[],
        monthly_totals=[100] * 12,
        monthly_counts=[1] * 12,
        total=1200,
    )
    assert table.monthly_balances == [-100] * 12
    assert sum(table.monthly_balances) == table.balance == -1200
