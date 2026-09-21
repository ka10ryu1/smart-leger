"""カテゴリ（categories シート）の追加・名称変更・並び替え・削除

- 名称変更は transactions / merchant_rules / allocations の category にも伝播させる
- フォールバックカテゴリ（Jev 失敗時に使う「その他」）は名称変更・削除しない
- 明細・ルール・内訳で使われているカテゴリは削除しない（先に付け替えてもらう）
- 銀行明細の取込で必要になるカテゴリ（住宅ローン・売電収入など）は ensure_categories で補う
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..constants import CATEGORY_DESCRIPTIONS, FALLBACK_CATEGORY, FORMULA_PREFIXES, UNCLASSIFIED_LABEL
from ..models import Category, LedgerData
from .normalize import normalize_merchant


class CategoryError(ValueError):
    """カテゴリ操作の入力が不正"""


def require_category(data: LedgerData, name: str) -> Category:
    """名前でカテゴリを探す（無ければ CategoryError）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名（シート上の表記そのまま）
    """
    for category in data.categories:
        if category.category == name:
            return category

    raise CategoryError(f'カテゴリ「{name}」が見つかりません。')


def category_usage(data: LedgerData) -> dict[str, Counter[str]]:
    """カテゴリごとの使用件数を返す（キーは transactions / rules / allocations、合計は Counter.total()）

    Args:
        data: 対象の LedgerData
    """
    usage: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for kind, items in (
        ('transactions', data.transactions),
        ('rules', data.merchant_rules),
        ('allocations', data.allocations),
    ):
        for item in items:
            usage[item.category][kind] += 1

    return usage


def validate_category_name(name: str, formula_prefixes: str = FORMULA_PREFIXES) -> str:
    """カテゴリ名を正規化して検証する（空文字・数式として解釈される先頭文字・予約語「未分類」は CategoryError）

    Args:
        name: 入力されたカテゴリ名
        formula_prefixes: 先頭に使えない文字

    Returns:
        正規化後（NFKC・空白整理）のカテゴリ名
    """
    name = normalize_merchant(name)  # カテゴリ名も加盟店名と同じ規則（NFKC・空白整理）で正規化する
    if not name:
        raise CategoryError('カテゴリ名を入力してください。')

    if name[0] in formula_prefixes:
        raise CategoryError(f'カテゴリ名の先頭に {formula_prefixes} の文字は使えません（数式として扱われます）。')

    if name == UNCLASSIFIED_LABEL:
        raise CategoryError(f'「{UNCLASSIFIED_LABEL}」はカテゴリが空の明細を指す予約語のため使えません。')

    return name


def add_category(data: LedgerData, name: str, description: str = '') -> Category:
    """カテゴリを末尾に追加する（空文字・数式になる先頭文字・予約語・重複は CategoryError）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
        description: Jev に渡す説明（任意）
    """
    name = validate_category_name(name)
    if any(normalize_merchant(c.category) == name for c in data.categories):  # シート上の未正規化名とも比較
        raise CategoryError(f'カテゴリ「{name}」は既に存在します。')

    max_order = max((c.sort_order for c in data.categories), default=0)
    category = Category(category=name, sort_order=max_order + 1, description=description.strip())
    data.categories.append(category)
    return category


def ensure_categories(data: LedgerData, names: list[str]) -> list[str]:
    """まだ登録されていないカテゴリを末尾に追加する（CSV の許可リストが使うカテゴリを補うために呼ぶ）

    既存ファイルには後から増やしたカテゴリが無いため、取込時に足りない分だけ補って
    編集画面のカテゴリ選択や年間表の並び順から漏れないようにする

    Args:
        data: 対象の LedgerData
        names: 必要なカテゴリ名（重複・空文字は無視する）

    Returns:
        実際に追加したカテゴリ名（追加が無ければ空リスト）
    """
    added: list[str] = []
    for name in names:
        if not name:
            continue

        try:
            add_category(data, name, CATEGORY_DESCRIPTIONS.get(name, ''))
        except CategoryError:  # 既に存在する（または旧規則で登録できない名前）なら何もしない
            continue

        added.append(name)

    return added


def edit_category(data: LedgerData, name: str, new_name: str, description: str) -> int:
    """カテゴリの名称と説明を変更する（検証をすべて通ってから書き換え、名称変更は明細・ルール・内訳にも伝播）

    Args:
        data: 対象の LedgerData
        name: 現在のカテゴリ名
        new_name: 新しいカテゴリ名（正規化後に現在名と同じなら説明だけ更新）
        description: Jev に渡す説明（名称変更時に空なら旧名の既定説明を引き継ぐ）

    Returns:
        名称変更を伝播した件数（明細 + ルール + 内訳。説明だけの更新なら 0）
    """
    category = require_category(data, name)
    new_name = normalize_merchant(new_name)
    renaming = new_name != normalize_merchant(name)  # シート上の名前が未正規化でも説明だけの保存を名称変更にしない
    if renaming:  # 旧規則で登録済みの名前は改名しない限り検証しない（説明だけ更新できる）
        new_name = validate_category_name(new_name)

    if renaming and name == FALLBACK_CATEGORY:
        raise CategoryError(f'「{FALLBACK_CATEGORY}」は分類エラー時の受け皿として使うため名称変更できません。')

    if renaming and any(normalize_merchant(c.category) == new_name for c in data.categories):
        raise CategoryError(f'カテゴリ「{new_name}」は既に存在します。')

    category.description = description.strip()
    if not renaming:
        return 0

    category.description = category.description or CATEGORY_DESCRIPTIONS.get(name, '')  # 既定説明は旧名にしか紐づかない
    category.category = new_name
    changed = 0
    for item in (*data.transactions, *data.merchant_rules, *data.allocations):
        if item.category == name:
            item.category = new_name
            changed += 1

    return changed


def move_category(data: LedgerData, name: str, delta: int) -> None:
    """カテゴリの表示順を delta だけ動かす（隣と入れ替える。端にあって動かせない場合は CategoryError で保存させない）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
        delta: -1 で上へ、+1 で下へ
    """
    ordered = data.sorted_categories()
    index = ordered.index(require_category(data, name))
    target = index + delta
    if target < 0 or target >= len(ordered):
        raise CategoryError('既に端にあるため並び順は変わりません。')

    ordered[index], ordered[target] = ordered[target], ordered[index]
    for order, category in enumerate(ordered, start=1):
        category.sort_order = order


def delete_category(data: LedgerData, name: str) -> None:
    """カテゴリを削除する（フォールバック・使用中・存在しない場合は CategoryError）

    Args:
        data: 対象の LedgerData
        name: カテゴリ名
    """
    category = require_category(data, name)
    if name == FALLBACK_CATEGORY:
        raise CategoryError(f'「{FALLBACK_CATEGORY}」は分類エラー時の受け皿として使うため削除できません。')

    usage = category_usage(data)[name]
    if usage.total():
        raise CategoryError(
            f'カテゴリ「{name}」は使用中のため削除できません'
            f'(明細 {usage["transactions"]} 件、ルール {usage["rules"]} 件、内訳 {usage["allocations"]} 件)。'
            '先に別カテゴリへ変更してください。'
        )

    data.categories.remove(category)
