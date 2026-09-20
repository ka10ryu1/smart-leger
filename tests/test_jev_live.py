"""TypeSafe Jev の実 API を叩くライブテスト

- TYPESAFE_API_KEY が（環境変数または .env に）設定されているときだけ実行される
- 通常の pytest では除外され、明示実行する場合: pytest -m live
- SMART_LEDGER_SKIP_LIVE=1 で明示的にスキップできる
- 送信するのは加盟店名・金額・利用日のみ（架空の値）
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from dotenv import load_dotenv

from smart_ledger.config import PROJECT_ROOT
from smart_ledger.constants import CATEGORY_DESCRIPTIONS
from smart_ledger.services.classifier import ClassificationPipeline, JevClassifier
from smart_ledger.services.jev_client import JevClient

pytestmark = pytest.mark.live


@pytest.fixture(scope='module')
def live_client() -> JevClient:
    """実 API を呼ぶ JevClient（API キーが無い、またはスキップ指定ならテストをスキップ）"""
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    if os.environ.get('SMART_LEDGER_SKIP_LIVE') == '1':
        pytest.skip('SMART_LEDGER_SKIP_LIVE=1')

    api_key = os.environ.get('TYPESAFE_API_KEY', '').strip()
    if not api_key:
        pytest.skip('TYPESAFE_API_KEY が未設定のためライブテストをスキップ')

    return JevClient(
        api_key,
        model=os.environ.get('TYPESAFE_MODEL', 'jev-latest'),
        base_url=os.environ.get('TYPESAFE_BASE_URL', 'https://api.typesafe.ai'),
    )


def test_live_choice_returns_valid_category_and_confidence(live_client: JevClient, categories: list[str]) -> None:
    """通信費らしい加盟店は「通信」になり、confidence と probabilities が妥当な範囲

    Args:
        live_client: 実 API クライアント
        categories: 初期カテゴリ
    """
    criteria = {c: CATEGORY_DESCRIPTIONS.get(c, c) for c in categories}
    choice = live_client.choose_category(
        'サンプルインターネット サービスリヨウリヨウ', 6000, date(2026, 8, 13), criteria
    )
    assert choice.choice in categories
    assert choice.confidence is not None and 0.0 <= choice.confidence <= 1.0
    assert set(choice.probabilities) <= set(categories)
    assert abs(sum(choice.probabilities.values()) - 1.0) < 0.05
    assert choice.choice == '通信'


def test_live_pipeline_marks_high_confidence_as_auto_accepted(live_client: JevClient, criteria: dict[str, str]) -> None:
    """保険料の加盟店は「保険・税金」になり、閾値判定は confidence に従う

    Args:
        live_client: 実 API クライアント
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(JevClassifier(live_client), threshold=0.85)
    result = pipeline.classify('サンプルセイメイホケン(ホケンリヨウ)', 5000, date(2026, 8, 15), criteria, [])
    assert result.source == 'jev'
    assert result.category == '保険・税金'
    assert result.confidence is not None
    assert pipeline.is_auto_accepted(result) == (result.confidence >= 0.85)


def test_live_kana_telecom_charge_is_classified_as_telecom(live_client: JevClient, criteria: dict[str, str]) -> None:
    """半角カナ略称の携帯電話料金（月分付き）が「通信」になる（指示文のカナ読み替えヒントの回帰確認）

    Args:
        live_client: 実 API クライアント
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(JevClassifier(live_client), threshold=0.85)
    result = pipeline.classify('7ガツブン エ-ユ-デンワリヨウリヨウ', 20871, date(2026, 8, 10), criteria, [])
    assert result.category == '通信'
    assert result.confidence is not None and result.confidence >= 0.85
