"""分類パイプライン。

    加盟店名正規化 → merchant_rules 検索 → 一致すれば rule
                                       → 一致しなければ Jev(交換可能な Classifier)
                                       → confidence >= 閾値 なら自動採用、未満なら要確認
                                       → Jev エラー時は その他 / source=error / 要確認

Jev を別モデルやルールエンジンに置き換える場合は Classifier Protocol を実装して差し替える。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

from ..models import (
    CATEGORY_DESCRIPTIONS,
    FALLBACK_CATEGORY,
    SOURCE_ERROR,
    SOURCE_JEV,
    SOURCE_RULE,
    ClassificationResult,
    MerchantRule,
)
from .jev_client import JevClient, JevError
from .merchant_rules import match_rule

logger = logging.getLogger(__name__)


class Classifier(Protocol):
    """未知の加盟店にカテゴリを付ける分類器のインターフェース。"""

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: list[str]
    ) -> ClassificationResult: ...


class JevClassifier:
    def __init__(self, client: JevClient):
        self.client = client

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: list[str]
    ) -> ClassificationResult:
        criteria = {c: CATEGORY_DESCRIPTIONS.get(c, c) for c in categories}
        try:
            choice = self.client.choose_category(merchant_normalized, amount, usage_date, criteria)
        except JevError as exc:
            logger.error("Jev 分類失敗 merchant=%s: %s", merchant_normalized, exc)
            return ClassificationResult(
                category=FALLBACK_CATEGORY, confidence=None, source=SOURCE_ERROR, error=str(exc)
            )
        except Exception as exc:  # 想定外でも取込全体を止めない
            logger.exception("Jev 分類で想定外のエラー merchant=%s", merchant_normalized)
            return ClassificationResult(
                category=FALLBACK_CATEGORY, confidence=None, source=SOURCE_ERROR, error=repr(exc)
            )
        category = choice.choice if choice.choice in categories else FALLBACK_CATEGORY
        if choice.choice not in categories:
            logger.warning("Jev が未知のカテゴリを返しました: %s", choice.choice)
        return ClassificationResult(
            category=category,
            confidence=choice.confidence,
            source=SOURCE_JEV,
            probabilities=choice.probabilities,
        )


class NullClassifier:
    """API キー未設定などで分類器を使えないときの代替。常に要確認にする。"""

    def __init__(self, reason: str = "分類器が設定されていません"):
        self.reason = reason

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: list[str]
    ) -> ClassificationResult:
        return ClassificationResult(
            category=FALLBACK_CATEGORY, confidence=None, source=SOURCE_ERROR, error=self.reason
        )


class ClassificationPipeline:
    def __init__(self, fallback: Classifier, threshold: float = 0.85):
        self.fallback = fallback
        self.threshold = threshold
        self._cache: dict[str, ClassificationResult] = {}

    def is_auto_accepted(self, result: ClassificationResult) -> bool:
        if result.source == SOURCE_RULE:
            return True
        if result.source != SOURCE_JEV or result.confidence is None:
            return False
        return result.confidence >= self.threshold

    def classify(
        self,
        merchant_normalized: str,
        amount: int,
        usage_date: date,
        categories: list[str],
        rules: list[MerchantRule],
    ) -> ClassificationResult:
        rule = match_rule(rules, merchant_normalized)
        if rule is not None and rule.category:
            return ClassificationResult(category=rule.category, confidence=None, source=SOURCE_RULE)

        # 同一取込内で同じ加盟店は 1 回だけ問い合わせる(エラー結果はキャッシュしない)
        cached = self._cache.get(merchant_normalized)
        if cached is not None:
            return cached
        result = self.fallback.classify(merchant_normalized, amount, usage_date, categories)
        if result.source != SOURCE_ERROR:
            self._cache[merchant_normalized] = result
        return result

    def clear_cache(self) -> None:
        self._cache.clear()
