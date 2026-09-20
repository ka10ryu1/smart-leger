"""加盟店ルール（merchant_rules シート）の検索と追加

ルールはユーザーが「今後この加盟店はこのカテゴリ」と明示的に選んだときだけ追加する
（Jev の分類結果や confidence が高いだけの明細を勝手にルール化しない）
"""

from __future__ import annotations

from ..models import LedgerData, MerchantRule, now_iso
from .normalize import normalize_merchant


def match_rule(rules: list[MerchantRule], merchant_normalized: str) -> MerchantRule | None:
    """加盟店名に合うルールを探す（完全一致を優先し、次に部分一致の最長パターン。大文字小文字は無視）

    Args:
        rules: 登録済みルール
        merchant_normalized: 正規化済みの加盟店名

    Returns:
        一致したルール（無ければ None）
    """
    target = normalize_merchant(merchant_normalized).casefold()
    if not target:
        return None

    best: MerchantRule | None = None
    best_len = -1
    for rule in rules:
        pattern = normalize_merchant(rule.merchant_pattern).casefold()
        if not pattern or not rule.category:
            continue

        if pattern == target:
            return rule

        if pattern in target and len(pattern) > best_len:
            best, best_len = rule, len(pattern)

    return best


def upsert_rule(data: LedgerData, merchant_pattern: str, category: str) -> MerchantRule:
    """同じパターンのルールがあればカテゴリを更新し、なければ追加する

    Args:
        data: 対象の LedgerData
        merchant_pattern: 加盟店パターン（正規化して保存する）
        category: 割り当てるカテゴリ
    """
    pattern = normalize_merchant(merchant_pattern)
    if not pattern:
        raise ValueError('加盟店パターンが空です。')

    for rule in data.merchant_rules:
        if normalize_merchant(rule.merchant_pattern).casefold() == pattern.casefold():
            rule.category = category
            rule.created_at = now_iso()
            return rule

    rule = MerchantRule(merchant_pattern=pattern, category=category, created_at=now_iso())
    data.merchant_rules.append(rule)
    return rule


def delete_rule(data: LedgerData, merchant_pattern: str) -> bool:
    """パターンが一致するルールを削除する

    Args:
        data: 対象の LedgerData
        merchant_pattern: 削除する加盟店パターン

    Returns:
        1 件以上削除したか
    """
    before = len(data.merchant_rules)
    pattern = normalize_merchant(merchant_pattern).casefold()
    data.merchant_rules = [
        r for r in data.merchant_rules if normalize_merchant(r.merchant_pattern).casefold() != pattern
    ]
    return len(data.merchant_rules) != before
