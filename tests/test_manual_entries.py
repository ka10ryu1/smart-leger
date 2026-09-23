"""手動明細の追加・削除（services/manual_entries.py）のテスト"""

from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.constants import KIND_EXPENSE, KIND_INCOME, MANUAL_IMPORT_ID, SOURCE_MANUAL
from smart_ledger.models import Allocation, ImportRecord, LedgerData, Transaction
from smart_ledger.services.aggregation import monthly_summary
from smart_ledger.services.excel_repository import ExcelRepository, default_ledger
from smart_ledger.services.importer import undo_import
from smart_ledger.services.manual_entries import (
    ManualEntryError,
    ManualEntryInput,
    add_manual_transaction,
    delete_manual_transaction,
    parse_manual_amount,
)


def entry(**overrides: str) -> ManualEntryInput:
    """既定値（2026-08-10 の現金の食費 1,200 円）に上書きを適用した入力を作る

    Args:
        overrides: 上書きするフィールド
    """
    values = {
        'usage_date': '2026-08-10',
        'merchant': ' ﾔｵﾔ　ｻﾝ ',
        'amount': '1,200',
        'category': '食費',
        'kind': KIND_EXPENSE,
        'card': '現金',
        'memo': ' 野菜 ',
    }
    values.update(overrides)
    return ManualEntryInput(**values)


def test_add_manual_transaction_sets_manual_fields() -> None:
    """手動明細は import_id=manual・row_key 空・source=manual で追加し、加盟店名を正規化して imports には載せない"""
    data = default_ledger()
    tx = add_manual_transaction(data, entry())
    assert data.transactions == [tx]
    assert tx.import_id == MANUAL_IMPORT_ID and tx.is_manual_entry
    assert tx.row_key == '' and tx.classification_source == SOURCE_MANUAL and tx.confidence is None
    assert tx.usage_date == date(2026, 8, 10) and tx.amount == 1200
    assert tx.merchant_raw == 'ﾔｵﾔ　ｻﾝ' and tx.merchant_normalized == 'ヤオヤ サン'
    assert (tx.card, tx.memo, tx.kind) == ('現金', '野菜', KIND_EXPENSE)
    assert tx.imported_at and tx.id.startswith('tx_')
    assert data.imports == []
    assert data.existing_row_keys() == set()  # 取込の重複判定に混ざらない
    assert not tx.needs_review(0.85)


def test_manual_transactions_are_sorted_by_usage_date() -> None:
    """追加後の明細は取込確定と同じく利用日順に並ぶ"""
    data = LedgerData(transactions=[Transaction('tx_late', date(2026, 8, 20), 'A', 'A', 100, '食費')])
    add_manual_transaction(data, entry(usage_date='2026-08-01'))
    assert [t.usage_date.day for t in data.transactions] == [1, 20]


def test_manual_transaction_is_aggregated_like_imported_ones() -> None:
    """手動明細も月次集計の総支出・カテゴリ別・収入に入る"""
    data = default_ledger()
    add_manual_transaction(data, entry(amount='1200'))
    add_manual_transaction(data, entry(merchant='お祝い', amount='5000', category='その他', kind=KIND_INCOME))
    summary = monthly_summary(data, '2026-08')
    assert summary.total == 1200 and summary.income == 5000
    assert [(c.category, c.amount) for c in summary.categories] == [('食費', 1200)]


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'usage_date': ''}, '利用日'),
        ({'usage_date': '2026-13-01'}, '利用日'),
        ({'merchant': '   '}, '加盟店・内容'),
        ({'amount': ''}, '整数'),
        ({'amount': '12.5'}, '整数'),
        ({'amount': '0'}, '0 は登録できません'),
        ({'category': '存在しない'}, '不明なカテゴリ'),
        ({'category': ''}, '不明なカテゴリ'),
        ({'kind': 'transfer'}, '不明な収支'),
        ({'amount': '-100', 'kind': KIND_INCOME}, '収入は正の金額'),
    ],
)
def test_add_manual_transaction_rejects_invalid_input(overrides: dict[str, str], message: str) -> None:
    """不正な入力は ManualEntryError にして明細を追加しない

    Args:
        overrides: 既定の入力に上書きする不正な値
        message: エラーメッセージに含まれる文言
    """
    data = default_ledger()
    with pytest.raises(ManualEntryError, match=message):
        add_manual_transaction(data, entry(**overrides))

    assert data.transactions == []


def test_parse_manual_amount_accepts_refund() -> None:
    """金額はカンマ区切りと負の値（返金）を受け付ける"""
    assert parse_manual_amount(' 12,345 ') == 12345
    assert parse_manual_amount('-500') == -500


def test_delete_manual_transaction_removes_allocations() -> None:
    """手動明細の削除は内訳も一緒に消し、他の明細の内訳は残す"""
    data = default_ledger()
    tx = add_manual_transaction(data, entry())
    data.transactions.append(Transaction('tx_other', date(2026, 8, 1), 'B', 'B', 300, '外食'))
    data.allocations += [
        Allocation(tx.id, '食費', 700),
        Allocation(tx.id, '外食', 500),
        Allocation('tx_other', '外食', 300),
    ]
    assert delete_manual_transaction(data, tx.id) == 2
    assert [t.id for t in data.transactions] == ['tx_other']
    assert [a.transaction_id for a in data.allocations] == ['tx_other']


def test_delete_manual_transaction_rejects_imported_and_missing() -> None:
    """CSV から取り込んだ明細と存在しない明細は削除しない"""
    imported = Transaction('tx_imp', date(2026, 8, 1), 'A', 'A', 100, '食費', import_id='imp_1', row_key='k')
    data = LedgerData(transactions=[imported], allocations=[Allocation('tx_imp', '食費', 100)])
    with pytest.raises(ManualEntryError, match='取込ごと取り消して'):
        delete_manual_transaction(data, 'tx_imp')

    with pytest.raises(ManualEntryError, match='見つかりません'):
        delete_manual_transaction(data, 'tx_missing')

    assert data.transactions == [imported] and len(data.allocations) == 1


def test_undo_import_keeps_manual_transactions() -> None:
    """取込の取り消しは同じ月の手動明細を消さない"""
    imported = Transaction('tx_imp', date(2026, 8, 1), 'A', 'A', 100, '食費', import_id='imp_1', row_key='k')
    data = LedgerData(
        transactions=[imported], imports=[ImportRecord('imp_1', 'a.csv', 'hash', '2026-08-02 00:00:00', row_count=1)]
    )
    data.categories = default_ledger().categories
    manual = add_manual_transaction(data, entry())
    undo_import(data, 'imp_1')
    assert data.transactions == [manual]


def test_manual_transaction_round_trips_through_excel(repo: ExcelRepository) -> None:
    """手動明細は Excel 保存後も import_id=manual・row_key 空のまま読み戻せる

    Args:
        repo: 一時ディレクトリ上の Excel リポジトリ
    """
    tx = repo.update(lambda data: add_manual_transaction(data, entry()))
    loaded = repo.load()
    assert loaded.transactions == [tx]
    assert loaded.imports == []
