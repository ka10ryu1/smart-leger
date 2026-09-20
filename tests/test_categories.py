"""カテゴリ管理サービスのテスト"""

from __future__ import annotations

from datetime import date

import pytest

from smart_ledger.models import Allocation, Category, LedgerData, MerchantRule, Transaction
from smart_ledger.services.categories import (
    CategoryError,
    add_category,
    category_usage,
    delete_category,
    edit_category,
    move_category,
    sorted_categories,
)


@pytest.fixture
def data(categories: list[str]) -> LedgerData:
    """初期カテゴリと、食費を使う明細・ルール・内訳を持つ LedgerData

    Args:
        categories: 初期カテゴリ
    """
    ledger = LedgerData(categories=[Category(c, i + 1) for i, c in enumerate(categories)])
    ledger.transactions.append(
        Transaction(
            id='tx_1',
            usage_date=date(2026, 8, 1),
            merchant_raw='A',
            merchant_normalized='A',
            amount=100,
            category='食費',
        )
    )
    ledger.merchant_rules.append(MerchantRule('A', '食費'))
    ledger.allocations.append(Allocation('tx_1', '食費', 100))
    return ledger


def test_add_category_appends_with_next_sort_order(data: LedgerData) -> None:
    """追加したカテゴリは末尾に入り、名前は正規化される

    Args:
        data: テスト用データ
    """
    added = add_category(data, ' ペット　用品 ', '犬猫のフード・病院')
    assert added.category == 'ペット 用品'
    assert added.sort_order == 11
    assert data.category_names()[-1] == 'ペット 用品'
    assert data.category_criteria()['ペット 用品'] == '犬猫のフード・病院'
    assert data.category_criteria()['食費'].startswith('スーパー')  # 既定説明にフォールバック


def test_add_category_rejects_empty_and_duplicate(data: LedgerData) -> None:
    """空文字と重複は CategoryError

    Args:
        data: テスト用データ
    """
    with pytest.raises(CategoryError):
        add_category(data, '   ')

    with pytest.raises(CategoryError):
        add_category(data, '食費')


def test_edit_category_renames_and_propagates(data: LedgerData) -> None:
    """名称変更は明細・ルール・内訳にも伝播し、説明も更新される

    Args:
        data: テスト用データ
    """
    changed = edit_category(data, '食費', '食料品', 'スーパーでの買い物')
    assert changed == 3
    assert data.transactions[0].category == '食料品'
    assert data.merchant_rules[0].category == '食料品'
    assert data.allocations[0].category == '食料品'
    assert '食費' not in data.category_names()
    assert data.category_criteria()['食料品'] == 'スーパーでの買い物'


def test_edit_category_description_only(data: LedgerData) -> None:
    """同じ名前なら説明だけ更新し、伝播件数は 0

    Args:
        data: テスト用データ
    """
    assert edit_category(data, '通信', '通信', '携帯・回線') == 0
    assert data.category_criteria()['通信'] == '携帯・回線'


def test_edit_category_guards(data: LedgerData) -> None:
    """フォールバックの名称変更・既存名への変更・存在しないカテゴリは CategoryError

    Args:
        data: テスト用データ
    """
    with pytest.raises(CategoryError):
        edit_category(data, 'その他', '雑費', '')

    with pytest.raises(CategoryError):
        edit_category(data, '通信', '交通', '')

    with pytest.raises(CategoryError):
        edit_category(data, '存在しない', 'x', '')


def test_move_category_swaps_neighbors_and_stops_at_edges(data: LedgerData) -> None:
    """上下移動は隣と入れ替わり、端では False を返す

    Args:
        data: テスト用データ
    """
    assert move_category(data, '外食', -1) is True
    assert data.category_names()[:2] == ['外食', '食費']
    assert move_category(data, '外食', -1) is False
    assert move_category(data, 'その他', 1) is False
    assert [c.sort_order for c in sorted_categories(data)] == list(range(1, 11))


def test_delete_category_guards_and_renumbers(data: LedgerData) -> None:
    """使用中とフォールバックは削除できず、未使用は削除されて連番になる

    Args:
        data: テスト用データ
    """
    with pytest.raises(CategoryError, match='使用中'):
        delete_category(data, '食費')

    with pytest.raises(CategoryError):
        delete_category(data, 'その他')

    delete_category(data, '外食')
    assert '外食' not in data.category_names()
    assert [c.sort_order for c in sorted_categories(data)] == list(range(1, 10))


def test_category_usage_counts(data: LedgerData) -> None:
    """使用件数は明細・ルール・内訳ごとに数えられる

    Args:
        data: テスト用データ
    """
    usage = category_usage(data)
    assert (usage['食費'].transactions, usage['食費'].rules, usage['食費'].allocations) == (1, 1, 1)
    assert usage['通信'].total == 0
