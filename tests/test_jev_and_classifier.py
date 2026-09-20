"""Jev クライアント・分類パイプライン・加盟店ルールのテスト（実 API は呼ばない）"""

from __future__ import annotations

import json
from datetime import date
from typing import Callable

import httpx
import pytest

from smart_ledger.models import MerchantRule
from smart_ledger.services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from smart_ledger.services.jev_client import JevClient, JevError, build_request_body, parse_choice_response
from smart_ledger.services.merchant_rules import match_rule


def make_client(handler: Callable[[httpx.Request], httpx.Response], retries: int = 0) -> JevClient:
    """MockTransport を使う JevClient を作る

    Args:
        handler: リクエストを受けてレスポンスを返す関数
        retries: 再試行回数
    """
    return JevClient('test-key', transport=httpx.MockTransport(handler), max_retries=retries)


def choice_response(choice: str, confidence: float, probabilities: dict[str, float] | None = None) -> httpx.Response:
    """choice 型の正常レスポンスを作る

    Args:
        choice: 選ばれたカテゴリ
        confidence: confidence
        probabilities: 各カテゴリの確率（None なら空）
    """
    answer = {'type': 'choice', 'choice': choice, 'probabilities': probabilities or {}, 'confidence': confidence}
    return httpx.Response(200, json={'answers': {'category': answer}})


def test_parse_choice_response() -> None:
    """choice / confidence / probabilities を取り出せる"""
    payload = {
        'model': 'jev-1.13.0',
        'answers': {
            'category': {
                'type': 'choice',
                'choice': '通信',
                'probabilities': {'通信': 0.91, 'その他': 0.09},
                'confidence': 0.88,
            }
        },
        'usage': {'input_tokens': 100, 'output_tokens': 10},
    }
    choice = parse_choice_response(payload)
    assert choice.choice == '通信'
    assert choice.confidence == pytest.approx(0.88)
    assert choice.probabilities['通信'] == pytest.approx(0.91)


def test_parse_choice_response_missing_answer() -> None:
    """answers が無ければ JevError"""
    with pytest.raises(JevError):
        parse_choice_response({'answers': {}})


def test_request_body_contains_only_minimal_fields(categories: list[str]) -> None:
    """state には加盟店名・金額・利用日だけを入れる

    Args:
        categories: 初期カテゴリ
    """
    body = build_request_body('KYASH', 10000, date(2026, 8, 15), {c: c for c in categories}, 'jev-latest')
    assert body['model'] == 'jev-latest'
    assert set(body['state'].keys()) == {'merchant', 'amount_jpy', 'usage_date'}
    question = body['questions']['category']
    assert question['type'] == 'choice'
    assert set(question['criteria'].keys()) == set(categories)


def test_client_posts_to_systemone_with_bearer(categories: list[str]) -> None:
    """公式エンドポイントに Bearer 付きで POST する

    Args:
        categories: 初期カテゴリ
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['url'] = str(request.url)
        seen['auth'] = request.headers.get('Authorization')
        seen['body'] = json.loads(request.content)
        return choice_response('交通', 0.9, {'交通': 0.9})

    result = make_client(handler).choose_category(
        'テスト駅 オートチャージ', 10000, date(2026, 8, 20), {c: c for c in categories}
    )
    assert result.choice == '交通'
    assert seen['url'] == 'https://api.typesafe.ai/v1/systemone'
    assert seen['auth'] == 'Bearer test-key'
    assert seen['body']['state']['merchant'] == 'テスト駅 オートチャージ'  # type: ignore[index]


def test_client_raises_on_401(categories: list[str]) -> None:
    """401 は JevError

    Args:
        categories: 初期カテゴリ
    """
    client = make_client(lambda r: httpx.Response(401, json={'error': 'unauthorized'}))
    with pytest.raises(JevError):
        client.choose_category('x', 1, date(2026, 1, 1), {c: c for c in categories})


def test_classifier_high_confidence_auto_accepted(categories: list[str]) -> None:
    """confidence が閾値以上なら自動採用

    Args:
        categories: 初期カテゴリ
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: choice_response('通信', 0.93))), 0.85)
    result = pipeline.classify('サンプルツウシン', 4500, date(2026, 8, 28), categories, [])
    assert result.source == 'jev'
    assert result.category == '通信'
    assert pipeline.is_auto_accepted(result) is True


def test_classifier_low_confidence_needs_review(categories: list[str]) -> None:
    """confidence が閾値未満なら要確認

    Args:
        categories: 初期カテゴリ
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: choice_response('食費', 0.62))), 0.85)
    result = pipeline.classify('サンプルスーパー', 3240, date(2026, 8, 12), categories, [])
    assert result.source == 'jev'
    assert result.confidence == pytest.approx(0.62)
    assert pipeline.is_auto_accepted(result) is False


def test_threshold_boundary_inclusive(categories: list[str]) -> None:
    """閾値ちょうどは自動採用

    Args:
        categories: 初期カテゴリ
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: choice_response('食費', 0.85))), 0.85)
    assert pipeline.is_auto_accepted(pipeline.classify('x', 1, date(2026, 1, 1), categories, [])) is True


def test_classifier_api_error_falls_back_safely(categories: list[str]) -> None:
    """HTTP エラーは その他 / error / 要確認 で継続する

    Args:
        categories: 初期カテゴリ
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: httpx.Response(500, text='boom'))), 0.85)
    result = pipeline.classify('ナゾノミセ', 999, date(2026, 8, 1), categories, [])
    assert result.source == 'error'
    assert result.category == 'その他'
    assert result.confidence is None
    assert result.error
    assert pipeline.is_auto_accepted(result) is False


def test_classifier_network_error_falls_back_safely(categories: list[str]) -> None:
    """通信エラーも source=error で継続する

    Args:
        categories: 初期カテゴリ
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('no network')

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    result = pipeline.classify('ナゾノミセ', 999, date(2026, 8, 1), categories, [])
    assert result.source == 'error'


def test_unknown_choice_maps_to_fallback(categories: list[str]) -> None:
    """カテゴリ一覧に無い choice は その他 / source=error にして要確認に回す

    Args:
        categories: 初期カテゴリ
    """
    result = JevClassifier(make_client(lambda r: choice_response('宇宙', 0.99))).classify(
        'x', 1, date(2026, 1, 1), categories
    )
    assert result.category == 'その他' and result.source == 'error' and result.confidence is None


def test_rule_takes_precedence_over_jev(categories: list[str]) -> None:
    """ルールに一致すれば Jev を呼ばない

    Args:
        categories: 初期カテゴリ
    """
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return choice_response('食費', 0.99)

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    result = pipeline.classify('KYASH', 10000, date(2026, 8, 15), categories, [MerchantRule('KYASH', 'その他')])
    assert result.source == 'rule'
    assert result.category == 'その他'
    assert result.confidence is None
    assert calls['n'] == 0


def test_same_merchant_queried_once_per_import(categories: list[str]) -> None:
    """同一取込内の同じ加盟店は 1 回だけ問い合わせる

    Args:
        categories: 初期カテゴリ
    """
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return choice_response('交通', 0.9)

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    for _ in range(3):
        pipeline.classify('テスト駅 オートチャージ', 10000, date(2026, 8, 20), categories, [])

    assert calls['n'] == 1


def test_null_classifier_when_no_api_key(categories: list[str]) -> None:
    """API キー無しの代替分類器は その他 / error を返す

    Args:
        categories: 初期カテゴリ
    """
    pipeline = ClassificationPipeline(NullClassifier('no key'), threshold=0.85)
    result = pipeline.classify('x', 1, date(2026, 1, 1), categories, [])
    assert result.source == 'error' and result.category == 'その他'


def test_match_rule_exact_partial_and_case() -> None:
    """完全一致優先、部分一致、大文字小文字無視で一致する"""
    rules = [
        MerchantRule('スミトモセイメイ', '保険・税金'),
        MerchantRule('kyash', 'その他'),
        MerchantRule('ＫＹＡＳＨ ＰＲＩＭＥ', '娯楽・サブスク'),
    ]
    assert match_rule(rules, 'スミトモセイメイホケン(ホケンリヨウ)').category == '保険・税金'
    assert match_rule(rules, 'KYASH').category == 'その他'
    assert match_rule(rules, 'KYASH PRIME').category == '娯楽・サブスク'
    assert match_rule(rules, 'セブンイレブン') is None


def test_retry_on_429_then_success(categories: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """retry_statuses のレスポンスは待ってから再試行し、成功すれば結果を返す（sleep は差し替える）

    Args:
        categories: 初期カテゴリ
        monkeypatch: time.sleep を無効化する
    """
    from smart_ledger.services import jev_client

    sleeps: list[float] = []
    monkeypatch.setattr(jev_client.time, 'sleep', sleeps.append)
    statuses = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return choice_response('通信', 0.9) if status == 200 else httpx.Response(status, text='rate limited')

    result = JevClassifier(make_client(handler, retries=1)).classify('x', 1, date(2026, 1, 1), categories)
    assert result.category == '通信' and result.source == 'jev'
    assert sleeps == [1.0]
