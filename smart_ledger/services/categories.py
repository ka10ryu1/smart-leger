"""カテゴリ（categories シート）の追加・名称変更・並び替え・削除

- 名称変更は transactions / merchant_rules / allocations の category にも伝播させる
- フォールバックカテゴリ（Jev 失敗時に使う「その他」）は名称変更・削除しない
- 明細・ルール・内訳で使われているカテゴリは削除しない（先に付け替えてもらう）
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import FALLBACK_CATEGORY
from ..models import Category, LedgerData
from .normalize import normalize_merchant


class CategoryError(ValueError):
    """カテゴリ操作の入力が不正"""


@dataclass
class CategoryUsage:
    """カテゴリがどこで使われているかの件数"""

    transactions: int = 0
    rules: int = 0
    allocations: int = 0

    @property
    def total(self) -> int:
        """全用途の合計件数"""
        return self.transactions + self.rules + self.allocations


def normalize_name(name: str) -> str:
    """カテゴリ名を正規化する（NFKC・空白整理。加盟店名と同じ規則）

    Args:
        name: 入力されたカテゴリ名
    """
    return normalize_merchant(name)


def sorted_categories(data: LedgerData) -> list[Category]:
    """sort_order 順のカテゴリを返す

    Args:
        data: 対象の LedgerData
    """
    return sorted(data.categories, key=lambda c: (c.sort_order, c.category))


def find_category(data: LedgerData, name: str) -> Category | None:
    """名前でカテゴリを探す

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
    """
    for category in data.categories:
        if category.category == name:
            return category

    return None


def category_usage(data: LedgerData) -> dict[str, CategoryUsage]:
    """カテゴリごとの使用件数（明細・ルール・内訳）を返す

    Args:
        data: 対象の LedgerData
    """
    usage: dict[str, CategoryUsage] = {c.category: CategoryUsage() for c in data.categories}
    for tx in data.transactions:
        usage.setdefault(tx.category, CategoryUsage()).transactions += 1

    for rule in data.merchant_rules:
        usage.setdefault(rule.category, CategoryUsage()).rules += 1

    for allocation in data.allocations:
        usage.setdefault(allocation.category, CategoryUsage()).allocations += 1

    return usage


def renumber(data: LedgerData) -> None:
    """sort_order を 1 から連番に振り直す

    Args:
        data: 対象の LedgerData
    """
    for index, category in enumerate(sorted_categories(data), start=1):
        category.sort_order = index


def add_category(data: LedgerData, name: str, description: str = '') -> Category:
    """カテゴリを末尾に追加する（空文字・重複は CategoryError）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
        description: Jev に渡す説明（任意）
    """
    name = normalize_name(name)
    if not name:
        raise CategoryError('カテゴリ名を入力してください。')

    if find_category(data, name) is not None:
        raise CategoryError(f'カテゴリ「{name}」は既に存在します。')

    max_order = max((c.sort_order for c in data.categories), default=0)
    category = Category(category=name, sort_order=max_order + 1, description=description.strip())
    data.categories.append(category)
    return category


def edit_category(data: LedgerData, name: str, new_name: str, description: str) -> int:
    """カテゴリの名称と説明を変更する（名称変更は明細・ルール・内訳にも伝播）

    Args:
        data: 対象の LedgerData
        name: 現在のカテゴリ名
        new_name: 新しいカテゴリ名（同じ名前なら説明だけ更新）
        description: Jev に渡す説明

    Returns:
        名称変更を伝播した件数（明細 + ルール + 内訳）
    """
    category = find_category(data, name)
    if category is None:
        raise CategoryError(f'カテゴリ「{name}」が見つかりません。')

    new_name = normalize_name(new_name)
    if not new_name:
        raise CategoryError('カテゴリ名を入力してください。')

    category.description = description.strip()
    if new_name == name:
        return 0

    if name == FALLBACK_CATEGORY:
        raise CategoryError(f'「{FALLBACK_CATEGORY}」は分類エラー時の受け皿として使うため名称変更できません。')

    if find_category(data, new_name) is not None:
        raise CategoryError(f'カテゴリ「{new_name}」は既に存在します。')

    category.category = new_name
    changed = 0
    for tx in data.transactions:
        if tx.category == name:
            tx.category = new_name
            changed += 1

    for rule in data.merchant_rules:
        if rule.category == name:
            rule.category = new_name
            changed += 1

    for allocation in data.allocations:
        if allocation.category == name:
            allocation.category = new_name
            changed += 1

    return changed


def move_category(data: LedgerData, name: str, delta: int) -> bool:
    """カテゴリの表示順を delta だけ動かす（隣と入れ替える）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
        delta: -1 で上へ、+1 で下へ

    Returns:
        並びが変わったか（端にあって動かせない場合は False）
    """
    ordered = sorted_categories(data)
    names = [c.category for c in ordered]
    if name not in names:
        raise CategoryError(f'カテゴリ「{name}」が見つかりません。')

    index = names.index(name)
    target = index + delta
    if target < 0 or target >= len(ordered):
        return False

    ordered[index], ordered[target] = ordered[target], ordered[index]
    for order, category in enumerate(ordered, start=1):
        category.sort_order = order

    return True


def delete_category(data: LedgerData, name: str) -> None:
    """カテゴリを削除する（フォールバック・使用中・存在しない場合は CategoryError）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
    """
    category = find_category(data, name)
    if category is None:
        raise CategoryError(f'カテゴリ「{name}」が見つかりません。')

    if name == FALLBACK_CATEGORY:
        raise CategoryError(f'「{FALLBACK_CATEGORY}」は分類エラー時の受け皿として使うため削除できません。')

    usage = category_usage(data).get(name, CategoryUsage())
    if usage.total:
        raise CategoryError(
            f'カテゴリ「{name}」は使用中のため削除できません'
            f'(明細 {usage.transactions} 件、ルール {usage.rules} 件、内訳 {usage.allocations} 件)。先に別カテゴリへ変更してください。'
        )

    data.categories.remove(category)
    renumber(data)
