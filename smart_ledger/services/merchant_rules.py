"""加盟店ルール（merchant_rules シート）の検索と追加、明細編集でのカテゴリ変更とルールの一括反映

ルールはユーザーが「今後この加盟店はこのカテゴリ」と明示的に選んだときだけ追加する
（Jev の分類結果や confidence が高いだけの明細を勝手にルール化しない）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..constants import KIND_LABELS, SOURCE_MANUAL, SOURCE_RULE
from ..models import LedgerData, MerchantRule, Transaction, now_iso
from .normalize import merchant_key, normalize_merchant

logger = logging.getLogger(__name__)


def prepare_rules(rules: list[MerchantRule]) -> list[tuple[MerchantRule, str, str]]:
    """照合用に (ルール, casefold 済みパターン, そのパターンの加盟店キー) を前計算する（空パターンやカテゴリ無しの行は除く）

    Args:
        rules: 登録済みルール
    """
    return [
        (rule, pattern, merchant_key(pattern))
        for rule in rules
        if rule.category and (pattern := normalize_merchant(rule.merchant_pattern).casefold())
    ]


def match_prepared(prepared: list[tuple[MerchantRule, str, str]], merchant_normalized: str) -> MerchantRule | None:
    """前計算済みのルールから加盟店名に合うものを探す（多数の明細を同じルールで照合するときに使う）

    照合は 3 段階（大文字小文字は無視）:
        1. 加盟店名とパターンの完全一致
        2. 請求月などを除いた加盟店キー同士の完全一致（「6ガツブン ○○」のルールが「7ガツブン ○○」にも効く。先に登録されたルール優先）
        3. 部分一致（パターンが加盟店名に含まれる、またはパターンのキーが加盟店キーに含まれる。最長パターン優先）

    Args:
        prepared: prepare_rules() の結果
        merchant_normalized: 正規化済みの加盟店名

    Returns:
        一致したルール（無ければ None）
    """
    target = normalize_merchant(merchant_normalized).casefold()
    if not target:
        return None

    target_key = merchant_key(target)
    for rule, pattern, _ in prepared:  # 1. 完全一致
        if pattern == target:
            return rule

    for rule, _, key in prepared:  # 2. 加盟店キー同士の一致
        if key == target_key:
            return rule

    partial = [(rule, pattern) for rule, pattern, key in prepared if pattern in target or key in target_key]
    return max(partial, key=lambda hit: len(hit[1]), default=(None, ''))[0]  # 3. 部分一致（同長なら先勝ち）


def match_rule(rules: list[MerchantRule], merchant_normalized: str) -> MerchantRule | None:
    """加盟店名に合うルールを探す（判定の詳細は match_prepared を参照）

    Args:
        rules: 登録済みルール
        merchant_normalized: 正規化済みの加盟店名

    Returns:
        一致したルール（無ければ None）
    """
    return match_prepared(prepare_rules(rules), merchant_normalized)


def rule_targets(
    transactions: list[Transaction], rules: list[MerchantRule], rule: MerchantRule, exclude_id: str
) -> list[Transaction]:
    """rules の中で rule が最優先で一致し、手動修正されていない明細を返す（取込時の分類と同じ優先順位で判定する）

    Args:
        transactions: 判定する明細
        rules: 照合に使うルール全体（rule を含む）
        rule: 反映したいルール
        exclude_id: 除外する明細 ID（編集中の明細）
    """
    prepared = prepare_rules(rules)
    return [
        t
        for t in transactions
        if t.id != exclude_id
        and t.classification_source != SOURCE_MANUAL
        and match_prepared(prepared, t.merchant_normalized) is rule
    ]


def preview_rule_targets(data: LedgerData, merchant_pattern: str, exclude_id: str) -> list[Transaction]:
    """merchant_pattern でルールを登録したときに反映対象になる明細を、登録せずに求める

    upsert_rule 後と同じ並びにした仮のルール一覧で判定する（同じ加盟店キーの既存ルールは先頭のものの位置で
    新パターンの仮ルールに置き換え、残りは除く。無ければ末尾に加える）

    Args:
        data: 対象の LedgerData
        merchant_pattern: 登録しようとしている加盟店パターン
        exclude_id: 除外する明細 ID（編集中の明細）
    """
    # 照合はカテゴリ無しのルールを無視するため、カテゴリには仮の値を入れる
    probe = MerchantRule(merchant_pattern=merchant_pattern, category='-')
    found = find_rules(data.merchant_rules, merchant_pattern)
    rules = [r for r in data.merchant_rules if not any(r is f for f in found)]
    rules.insert(data.merchant_rules.index(found[0]) if found else len(rules), probe)
    return rule_targets(data.transactions, rules, probe, exclude_id)


def find_rules(rules: list[MerchantRule], merchant_pattern: str) -> list[MerchantRule]:
    """加盟店キーが同じ（大文字小文字無視）ルールを登録順に返す（「7ガツブン X」と「X」は同じ加盟店のルールとして扱う）

    Args:
        rules: 登録済みルール
        merchant_pattern: 探す加盟店パターン
    """
    key = merchant_key(merchant_pattern.casefold())
    return [r for r in rules if merchant_key(r.merchant_pattern.casefold()) == key]


def upsert_rule(data: LedgerData, merchant_pattern: str, category: str) -> MerchantRule:
    """同じ加盟店キーのルールがあればパターンとカテゴリを更新し、なければ追加する（同じキーのルールが複数あれば 1 つに統合する）

    Args:
        data: 対象の LedgerData
        merchant_pattern: 加盟店パターン（正規化して保存する）
        category: 割り当てるカテゴリ
    """
    pattern = normalize_merchant(merchant_pattern)
    if not pattern:
        raise ValueError('加盟店パターンが空です。')

    if category not in data.category_names():
        raise ValueError(f'不明なカテゴリです: {category}')

    found = find_rules(data.merchant_rules, pattern)
    if found:
        rule, *duplicates = found
        rule.merchant_pattern = pattern
        rule.category = category
        rule.created_at = now_iso()
        if duplicates:  # 旧仕様で月ごとに登録された同じ加盟店のルールを統合する
            data.merchant_rules = [r for r in data.merchant_rules if not any(r is d for d in duplicates)]
            logger.info('merchant rules merged: removed=%d', len(duplicates))

        return rule

    rule = MerchantRule(merchant_pattern=pattern, category=category)
    data.merchant_rules.append(rule)
    return rule


@dataclass
class CategoryChange:
    """明細のカテゴリ変更の結果（ルールを登録しなかった場合 rule_pattern は空）"""

    rule_pattern: str = ''
    applied_others: int = 0  # ルールで一緒にカテゴリを変えた他の明細の件数
    matches_self: bool = True  # 登録したルールが編集した明細自身に一致するか
    changed_kind: str = ''  # 収支を変えたときだけ新しい kind（メッセージに出す）


def apply_manual_category(
    data: LedgerData,
    tx: Transaction,
    category: str,
    *,
    kind: str = '',
    memo: str = '',
    remember: bool = False,
    rule_pattern: str = '',
) -> CategoryChange:
    """明細のカテゴリを手動分類として変更し、remember ならルールを登録して一致する他の明細にも反映する（保存は呼び出し側）

    不明なカテゴリ・収支、空の加盟店パターンは ValueError。反映する他の明細（登録したルールが最優先で一致するものの
    うち、手動修正済みと、すでに同じカテゴリのものは除く）は classification_source を rule にする

    Args:
        data: 対象の LedgerData
        tx: 編集する明細（data に含まれるもの）
        category: 新しいカテゴリ
        kind: 新しい収支（空なら変えない）
        memo: 新しいメモ
        remember: ルールを登録して他の明細にも反映するか
        rule_pattern: ルールの加盟店パターン（空なら請求月などを除いた加盟店キー）
    """
    if category not in data.category_names():
        raise ValueError(f'不明なカテゴリです: {category}')

    if kind and kind not in KIND_LABELS:
        raise ValueError(f'不明な収支です: {kind}')

    changed_kind = kind if kind and kind != tx.kind else ''
    tx.kind = kind or tx.kind
    tx.category = category
    tx.confidence = None
    tx.classification_source = SOURCE_MANUAL
    tx.memo = memo
    if not remember:
        return CategoryChange(changed_kind=changed_kind)

    rule = upsert_rule(data, rule_pattern or merchant_key(tx.merchant_normalized), category)
    targets = [o for o in rule_targets(data.transactions, data.merchant_rules, rule, tx.id) if o.category != category]
    for other in targets:
        other.category = category
        other.confidence = None
        other.classification_source = SOURCE_RULE

    matches_self = match_rule([rule], tx.merchant_normalized) is not None
    return CategoryChange(rule.merchant_pattern, len(targets), matches_self, changed_kind)


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
