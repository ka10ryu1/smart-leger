"""加盟店ルール(merchant_rules シート)の検索と追加。

ルールはユーザーが「今後この加盟店はこのカテゴリ」と明示的に選んだときだけ追加する。
Jev の分類結果や confidence が高いだけの明細を勝手にルール化しない。
"""

from __future__ import annotations

from ..models import LedgerData, MerchantRule, now_iso
from .normalize import normalize_merchant


def match_rule(rules: list[MerchantRule], merchant_normalized: str) -> MerchantRule | None:
    """完全一致を優先し、次に部分一致(最長パターン優先)で探す。大文字小文字は無視。"""
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
    """同じパターンのルールがあればカテゴリを更新し、なければ追加する。"""
    pattern = normalize_merchant(merchant_pattern)
    if not pattern:
        raise ValueError("加盟店パターンが空です。")
    for rule in data.merchant_rules:
        if normalize_merchant(rule.merchant_pattern).casefold() == pattern.casefold():
            rule.category = category
            rule.created_at = now_iso()
            return rule
    rule = MerchantRule(merchant_pattern=pattern, category=category, created_at=now_iso())
    data.merchant_rules.append(rule)
    return rule


def delete_rule(data: LedgerData, merchant_pattern: str) -> bool:
    before = len(data.merchant_rules)
    pattern = normalize_merchant(merchant_pattern).casefold()
    data.merchant_rules = [
        r for r in data.merchant_rules if normalize_merchant(r.merchant_pattern).casefold() != pattern
    ]
    return len(data.merchant_rules) != before
