"""TypeSafe Jev の実 API を叩くライブテスト。

- TYPESAFE_API_KEY が(環境変数または .env に)設定されているときだけ実行される
- SMART_LEDGER_SKIP_LIVE=1 で明示的にスキップできる
- 通常の pytest 実行から外す場合:  pytest -m "not live"
- 送信するのは加盟店名・金額・利用日のみ(架空の値)
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from dotenv import load_dotenv

from smart_ledger.config import PROJECT_ROOT
from smart_ledger.models import CATEGORY_DESCRIPTIONS, DEFAULT_CATEGORIES
from smart_ledger.services.classifier import ClassificationPipeline, JevClassifier
from smart_ledger.services.jev_client import JevClient

load_dotenv(PROJECT_ROOT / ".env", override=False)
API_KEY = os.environ.get("TYPESAFE_API_KEY", "").strip()

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not API_KEY, reason="TYPESAFE_API_KEY が未設定のためライブテストをスキップ"),
    pytest.mark.skipif(os.environ.get("SMART_LEDGER_SKIP_LIVE") == "1", reason="SMART_LEDGER_SKIP_LIVE=1"),
]

CATS = list(DEFAULT_CATEGORIES)


@pytest.fixture(scope="module")
def client() -> JevClient:
    return JevClient(
        API_KEY,
        model=os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        base_url=os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai"),
    )


def test_live_choice_returns_valid_category_and_confidence(client: JevClient):
    criteria = {c: CATEGORY_DESCRIPTIONS.get(c, c) for c in CATS}
    choice = client.choose_category("サンプルインターネット サービスリヨウリヨウ", 6000, date(2026, 8, 13), criteria)
    assert choice.choice in CATS
    assert choice.confidence is not None and 0.0 <= choice.confidence <= 1.0
    assert set(choice.probabilities) <= set(CATS)
    assert abs(sum(choice.probabilities.values()) - 1.0) < 0.05
    # 明らかに通信費の加盟店なので、通信が最有力候補であること
    assert choice.choice == "通信"


def test_live_pipeline_marks_high_confidence_as_auto_accepted(client: JevClient):
    pipeline = ClassificationPipeline(JevClassifier(client), threshold=0.85)
    result = pipeline.classify("サンプルセイメイホケン(ホケンリヨウ)", 5000, date(2026, 8, 15), CATS, [])
    assert result.source == "jev"
    assert result.category == "保険・税金"
    assert result.confidence is not None
    # 閾値判定は confidence に従う(値そのものはモデル更新で変わりうる)
    assert pipeline.is_auto_accepted(result) == (result.confidence >= 0.85)
