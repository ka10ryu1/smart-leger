from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from smart_ledger.models import DEFAULT_CATEGORIES, MerchantRule
from smart_ledger.services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from smart_ledger.services.jev_client import JevClient, JevError, build_request_body, parse_choice_response
from smart_ledger.services.merchant_rules import match_rule

CATS = list(DEFAULT_CATEGORIES)


def make_client(handler, retries=0) -> JevClient:
    return JevClient("test-key", transport=httpx.MockTransport(handler), max_retries=retries)


def test_parse_choice_response():
    payload = {
        "model": "jev-1.13.0",
        "answers": {
            "category": {
                "type": "choice",
                "choice": "通信",
                "probabilities": {"通信": 0.91, "その他": 0.09},
                "confidence": 0.88,
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }
    choice = parse_choice_response(payload)
    assert choice.choice == "通信"
    assert choice.confidence == pytest.approx(0.88)
    assert choice.probabilities["通信"] == pytest.approx(0.91)


def test_parse_choice_response_missing_answer():
    with pytest.raises(JevError):
        parse_choice_response({"answers": {}})


def test_request_body_contains_only_minimal_fields():
    body = build_request_body("KYASH", 10000, date(2026, 8, 15), {c: c for c in CATS}, "jev-latest")
    assert body["model"] == "jev-latest"
    assert set(body["state"].keys()) == {"merchant", "amount_jpy", "usage_date"}
    q = body["questions"]["category"]
    assert q["type"] == "choice"
    assert set(q["criteria"].keys()) == set(CATS)


def test_client_posts_to_systemone_with_bearer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"answers": {"category": {"type": "choice", "choice": "交通", "probabilities": {"交通": 0.9}, "confidence": 0.9}}},
        )

    client = make_client(handler)
    result = client.choose_category("テスト駅 オートチャージ", 10000, date(2026, 8, 20), {c: c for c in CATS})
    assert result.choice == "交通"
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["auth"] == "Bearer test-key"
    assert seen["body"]["state"]["merchant"] == "テスト駅 オートチャージ"


def test_client_raises_on_401():
    client = make_client(lambda r: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(JevError):
        client.choose_category("x", 1, date(2026, 1, 1), {c: c for c in CATS})


def test_classifier_high_confidence_auto_accepted():
    client = make_client(
        lambda r: httpx.Response(
            200, json={"answers": {"category": {"type": "choice", "choice": "通信", "probabilities": {}, "confidence": 0.93}}}
        )
    )
    pipeline = ClassificationPipeline(JevClassifier(client), threshold=0.85)
    result = pipeline.classify("サンプルツウシン", 4500, date(2026, 8, 28), CATS, [])
    assert result.source == "jev"
    assert result.category == "通信"
    assert pipeline.is_auto_accepted(result) is True


def test_classifier_low_confidence_needs_review():
    client = make_client(
        lambda r: httpx.Response(
            200, json={"answers": {"category": {"type": "choice", "choice": "食費", "probabilities": {}, "confidence": 0.62}}}
        )
    )
    pipeline = ClassificationPipeline(JevClassifier(client), threshold=0.85)
    result = pipeline.classify("サンプルスーパー", 3240, date(2026, 8, 12), CATS, [])
    assert result.source == "jev"
    assert result.confidence == pytest.approx(0.62)
    assert pipeline.is_auto_accepted(result) is False


def test_threshold_boundary_inclusive():
    client = make_client(
        lambda r: httpx.Response(
            200, json={"answers": {"category": {"type": "choice", "choice": "食費", "probabilities": {}, "confidence": 0.85}}}
        )
    )
    pipeline = ClassificationPipeline(JevClassifier(client), threshold=0.85)
    assert pipeline.is_auto_accepted(pipeline.classify("x", 1, date(2026, 1, 1), CATS, [])) is True


def test_classifier_api_error_falls_back_safely():
    client = make_client(lambda r: httpx.Response(500, text="boom"))
    pipeline = ClassificationPipeline(JevClassifier(client), threshold=0.85)
    result = pipeline.classify("ナゾノミセ", 999, date(2026, 8, 1), CATS, [])
    assert result.source == "error"
    assert result.category == "その他"
    assert result.confidence is None
    assert result.error
    assert pipeline.is_auto_accepted(result) is False


def test_classifier_network_error_falls_back_safely():
    def handler(request):
        raise httpx.ConnectError("no network")

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    result = pipeline.classify("ナゾノミセ", 999, date(2026, 8, 1), CATS, [])
    assert result.source == "error"


def test_unknown_choice_maps_to_fallback():
    client = make_client(
        lambda r: httpx.Response(
            200, json={"answers": {"category": {"type": "choice", "choice": "宇宙", "probabilities": {}, "confidence": 0.99}}}
        )
    )
    result = JevClassifier(client).classify("x", 1, date(2026, 1, 1), CATS)
    assert result.category == "その他"


def test_rule_takes_precedence_over_jev():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"answers": {"category": {"type": "choice", "choice": "食費", "confidence": 0.99}}})

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    rules = [MerchantRule("KYASH", "その他")]
    result = pipeline.classify("KYASH", 10000, date(2026, 8, 15), CATS, rules)
    assert result.source == "rule"
    assert result.category == "その他"
    assert result.confidence is None
    assert calls["n"] == 0


def test_same_merchant_queried_once_per_import():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"answers": {"category": {"type": "choice", "choice": "交通", "confidence": 0.9}}})

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    for _ in range(3):
        pipeline.classify("テスト駅 オートチャージ", 10000, date(2026, 8, 20), CATS, [])
    assert calls["n"] == 1


def test_null_classifier_when_no_api_key():
    pipeline = ClassificationPipeline(NullClassifier("no key"), threshold=0.85)
    result = pipeline.classify("x", 1, date(2026, 1, 1), CATS, [])
    assert result.source == "error" and result.category == "その他"


def test_match_rule_exact_partial_and_case():
    rules = [MerchantRule("スミトモセイメイ", "保険・税金"), MerchantRule("kyash", "その他"), MerchantRule("ＫＹＡＳＨ ＰＲＩＭＥ", "娯楽・サブスク")]
    assert match_rule(rules, "スミトモセイメイホケン(ホケンリヨウ)").category == "保険・税金"
    assert match_rule(rules, "KYASH").category == "その他"
    assert match_rule(rules, "KYASH PRIME").category == "娯楽・サブスク"  # 完全一致優先
    assert match_rule(rules, "セブンイレブン") is None
