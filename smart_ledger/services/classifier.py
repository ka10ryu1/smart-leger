"""分類パイプライン

    加盟店名正規化 → merchant_rules 検索 → 一致すれば rule
                                       → 一致しなければ Jev（交換可能な Classifier）
                                       → confidence >= 閾値 なら自動採用、未満なら要確認
                                       → Jev エラー時（不明なカテゴリを返した場合を含む）は その他 / source=error / 要確認

Jev を別モデルやルールエンジンに置き換える場合は Classifier Protocol を実装して差し替える
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Protocol

from ..constants import FALLBACK_CATEGORY, SOURCE_ERROR, SOURCE_JEV, SOURCE_RULE
from ..models import ClassificationResult, MerchantRule
from .jev_client import JevClient, JevError
from .merchant_rules import match_prepared
from .normalize import merchant_key

logger = logging.getLogger(__name__)


class Classifier(Protocol):
    """未知の加盟店にカテゴリを付ける分類器のインターフェース"""

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: dict[str, str]
    ) -> ClassificationResult:
        """加盟店を分類する

        Args:
            merchant_normalized: 正規化済みの加盟店名
            amount: 金額（円）
            usage_date: 利用日
            categories: 選択肢のカテゴリ名 → 説明
        """
        ...


class JevClassifier:
    """Jev の choice でカテゴリを選ばせる分類器"""

    def __init__(self, client: JevClient):
        """
        Args:
            client: Jev API クライアント
        """
        self.client = client

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: dict[str, str]
    ) -> ClassificationResult:
        """Jev に問い合わせて分類する（失敗しても例外を投げず source=error で返す）

        Args:
            merchant_normalized: 正規化済みの加盟店名
            amount: 金額（円）
            usage_date: 利用日
            categories: 選択肢のカテゴリ名 → 説明（そのまま criteria に使う）
        """
        try:
            choice = self.client.choose_category(merchant_normalized, amount, usage_date, categories)
        except JevError as exc:
            logger.error('jev classification failed: merchant=%s error=%s', merchant_normalized, exc)
            return ClassificationResult(
                category=FALLBACK_CATEGORY, confidence=None, source=SOURCE_ERROR, error=str(exc)
            )
        except Exception as exc:  # 想定外でも取込全体を止めない
            logger.exception('jev classification unexpected error: merchant=%s', merchant_normalized)
            return ClassificationResult(
                category=FALLBACK_CATEGORY, confidence=None, source=SOURCE_ERROR, error=repr(exc)
            )

        return ClassificationResult(category=choice.choice, confidence=choice.confidence, source=SOURCE_JEV)


class NullClassifier:
    """API キー未設定などで分類器を使えないときの代替（常に要確認にする）"""

    def __init__(self, reason: str = '分類器が設定されていません'):
        """
        Args:
            reason: エラー理由として ClassificationResult.error に入れる文言
        """
        self.reason = reason

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: dict[str, str]
    ) -> ClassificationResult:
        """常に その他 / source=error を返す

        Args:
            merchant_normalized: 正規化済みの加盟店名（未使用）
            amount: 金額（未使用）
            usage_date: 利用日（未使用）
            categories: カテゴリ名 → 説明（未使用）
        """
        return ClassificationResult(category=FALLBACK_CATEGORY, confidence=None, source=SOURCE_ERROR, error=self.reason)


class ClassificationPipeline:
    """rule → fallback 分類器 → 閾値判定 を行うパイプライン"""

    def __init__(self, fallback: Classifier, threshold: float = 0.85):
        """
        Args:
            fallback: ルールに一致しなかった加盟店を分類する分類器（通常は JevClassifier）
            threshold: 自動採用する confidence の閾値
        """
        self.fallback = fallback
        self.threshold = threshold
        self.cache: dict[str, ClassificationResult] = {}

    def is_auto_accepted(self, result: ClassificationResult) -> bool:
        """自動採用できる結果か（rule は常に採用、jev は confidence が閾値以上のとき）

        Args:
            result: 分類結果
        """
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
        categories: dict[str, str],
        prepared_rules: list[tuple[MerchantRule, str, str]],
    ) -> ClassificationResult:
        """ルールを優先して分類し、無ければ fallback に問い合わせる（同一加盟店は請求月が違っても 1 回だけ問い合わせる）

        キャッシュした結果のカテゴリが categories に無い（確定待ちの間に名称変更・削除された）場合は捨てて再問い合わせする

        Args:
            merchant_normalized: 正規化済みの加盟店名
            amount: 金額（円）
            usage_date: 利用日
            categories: 選択肢のカテゴリ名 → 説明
            prepared_rules: merchant_rules.prepare_rules() 済みの加盟店ルール（取込 1 回につき 1 度だけ前計算する）
        """
        rule = match_prepared(prepared_rules, merchant_normalized)
        if rule is not None:
            return ClassificationResult(category=rule.category, confidence=None, source=SOURCE_RULE)

        # 「7ガツブン X」と「8ガツブン X」は同じ加盟店として 1 回だけ問い合わせる
        cache_key = merchant_key(merchant_normalized)
        cached = self.cache.get(cache_key)
        if cached is not None and cached.category in categories:
            return cached

        result = self.fallback.classify(merchant_normalized, amount, usage_date, categories)
        if result.source != SOURCE_ERROR:  # エラー結果はキャッシュしない
            self.cache[cache_key] = result

        return result

    def clear_cache(self) -> None:
        """加盟店キャッシュを空にする（新しい CSV のプレビュー時に呼び、確定の再試行では使い回す）"""
        self.cache.clear()
