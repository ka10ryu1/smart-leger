"""Jev クライアント・分類パイプライン・加盟店ルールのテスト（実 API は呼ばない）"""

from __future__ import annotations

import json
from datetime import date
from typing import Callable

import httpx
import pytest

from smart_ledger.models import LedgerData, MerchantRule, Transaction
from smart_ledger.services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from smart_ledger.services.jev_client import JevClient, JevError, build_request_body, parse_choice_response
from smart_ledger.services.merchant_rules import (
    match_rule,
    prepare_rules,
    preview_rule_targets,
    rule_targets,
    upsert_rule,
)


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
    assert question['criteria'] == {c: c for c in categories}  # 説明はそのまま criteria になる


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


def test_classifier_high_confidence_auto_accepted(criteria: dict[str, str]) -> None:
    """confidence が閾値以上なら自動採用

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: choice_response('通信', 0.93))), 0.85)
    result = pipeline.classify('サンプルツウシン', 4500, date(2026, 8, 28), criteria, [])
    assert result.source == 'jev'
    assert result.category == '通信'
    assert pipeline.is_auto_accepted(result) is True


def test_classifier_low_confidence_needs_review(criteria: dict[str, str]) -> None:
    """confidence が閾値未満なら要確認

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: choice_response('食費', 0.62))), 0.85)
    result = pipeline.classify('サンプルスーパー', 3240, date(2026, 8, 12), criteria, [])
    assert result.source == 'jev'
    assert result.confidence == pytest.approx(0.62)
    assert pipeline.is_auto_accepted(result) is False


def test_threshold_boundary_inclusive(criteria: dict[str, str]) -> None:
    """閾値ちょうどは自動採用

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: choice_response('食費', 0.85))), 0.85)
    assert pipeline.is_auto_accepted(pipeline.classify('x', 1, date(2026, 1, 1), criteria, [])) is True


def test_classifier_api_error_falls_back_safely(criteria: dict[str, str]) -> None:
    """HTTP エラーは その他 / error / 要確認 で継続する

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(JevClassifier(make_client(lambda r: httpx.Response(500, text='boom'))), 0.85)
    result = pipeline.classify('ナゾノミセ', 999, date(2026, 8, 1), criteria, [])
    assert result.source == 'error'
    assert result.category == 'その他'
    assert result.confidence is None
    assert result.error
    assert pipeline.is_auto_accepted(result) is False


def test_classifier_network_error_falls_back_safely(criteria: dict[str, str]) -> None:
    """通信エラーも source=error で継続する

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('no network')

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    result = pipeline.classify('ナゾノミセ', 999, date(2026, 8, 1), criteria, [])
    assert result.source == 'error'


def test_unknown_choice_maps_to_fallback(criteria: dict[str, str]) -> None:
    """カテゴリ一覧に無い choice は その他 / source=error にして要確認に回す

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    result = JevClassifier(make_client(lambda r: choice_response('宇宙', 0.99))).classify(
        'x', 1, date(2026, 1, 1), criteria
    )
    assert result.category == 'その他' and result.source == 'error' and result.confidence is None


def test_rule_takes_precedence_over_jev(criteria: dict[str, str]) -> None:
    """ルールに一致すれば Jev を呼ばない

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return choice_response('食費', 0.99)

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    result = pipeline.classify(
        'KYASH', 10000, date(2026, 8, 15), criteria, prepare_rules([MerchantRule('KYASH', 'その他')])
    )
    assert result.source == 'rule'
    assert result.category == 'その他'
    assert result.confidence is None
    assert calls['n'] == 0


def test_same_merchant_queried_once_per_import(criteria: dict[str, str]) -> None:
    """同一取込内の同じ加盟店は 1 回だけ問い合わせる

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return choice_response('交通', 0.9)

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    for _ in range(3):
        pipeline.classify('テスト駅 オートチャージ', 10000, date(2026, 8, 20), criteria, [])

    assert calls['n'] == 1


def test_cached_result_is_dropped_when_category_removed(criteria: dict[str, str]) -> None:
    """キャッシュしたカテゴリが選択肢から消えていれば（名称変更・削除後）再問い合わせして新しい結果を使う

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    answers = iter(['交通', '日用品・買い物'])

    def handler(request: httpx.Request) -> httpx.Response:
        return choice_response(next(answers), 0.9)

    pipeline = ClassificationPipeline(JevClassifier(make_client(handler)), threshold=0.85)
    assert pipeline.classify('テスト駅', 100, date(2026, 8, 20), criteria, []).category == '交通'
    renamed = {('移動' if k == '交通' else k): v for k, v in criteria.items()}
    assert pipeline.classify('テスト駅', 100, date(2026, 8, 20), renamed, []).category == '日用品・買い物'
    assert (
        pipeline.classify('テスト駅', 100, date(2026, 8, 20), renamed, []).category == '日用品・買い物'
    )  # 新結果をキャッシュ


def test_null_classifier_when_no_api_key(criteria: dict[str, str]) -> None:
    """API キー無しの代替分類器は その他 / error を返す

    Args:
        criteria: 初期カテゴリの 名前 → 説明
    """
    pipeline = ClassificationPipeline(NullClassifier('no key'), threshold=0.85)
    result = pipeline.classify('x', 1, date(2026, 1, 1), criteria, [])
    assert result.source == 'error' and result.category == 'その他'


def matched_category(rules: list[MerchantRule], merchant: str) -> str:
    """加盟店名に一致したルールのカテゴリを返す（一致しなければテスト失敗）

    Args:
        rules: 登録済みルール
        merchant: 照合する加盟店名
    """
    rule = match_rule(rules, merchant)
    if rule is None:
        pytest.fail(f'ルールに一致しませんでした: {merchant}')

    return rule.category


def test_match_rule_exact_partial_and_case() -> None:
    """完全一致優先、部分一致、大文字小文字無視で一致する"""
    rules = [
        MerchantRule('スミトモセイメイ', '保険・税金'),
        MerchantRule('kyash', 'その他'),
        MerchantRule('ＫＹＡＳＨ ＰＲＩＭＥ', '娯楽・サブスク'),
    ]
    assert matched_category(rules, 'スミトモセイメイホケン(ホケンリヨウ)') == '保険・税金'
    assert matched_category(rules, 'KYASH') == 'その他'
    assert matched_category(rules, 'KYASH PRIME') == '娯楽・サブスク'
    assert match_rule(rules, 'セブンイレブン') is None


def test_retry_on_429_then_success(criteria: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """retry_statuses のレスポンスは待ってから再試行し、成功すれば結果を返す（sleep は差し替える）

    Args:
        criteria: 初期カテゴリの 名前 → 説明
        monkeypatch: time.sleep を無効化する
    """
    from smart_ledger.services import jev_client

    sleeps: list[float] = []
    monkeypatch.setattr(jev_client.time, 'sleep', sleeps.append)
    statuses = iter([429, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return choice_response('通信', 0.9) if status == 200 else httpx.Response(status, text='rate limited')

    result = JevClassifier(make_client(handler, retries=1)).classify('x', 1, date(2026, 1, 1), criteria)
    assert result.category == '通信' and result.source == 'jev'
    assert sleeps == [1.0]


@pytest.mark.parametrize('raw', ['NaN', 'inf', '-inf', 1.5, -0.1, 'abc'])
def test_parse_choice_response_rejects_invalid_confidence(raw: object) -> None:
    """NaN・inf・範囲外・数値でない confidence は None（要確認扱い）になる

    Args:
        raw: レスポンスに入る不正な confidence
    """
    payload = {'answers': {'category': {'type': 'choice', 'choice': '通信', 'confidence': raw}}}
    choice = parse_choice_response(payload)
    assert choice.confidence is None


def test_parse_choice_response_probability_fallback_is_validated() -> None:
    """confidence が不正なとき probabilities[choice] で補うが、それも範囲外なら None にする"""
    valid = {'answers': {'category': {'choice': '通信', 'confidence': 1.5, 'probabilities': {'通信': 0.7}}}}
    assert parse_choice_response(valid).confidence == pytest.approx(0.7)
    invalid = {'answers': {'category': {'choice': '通信', 'confidence': 'NaN', 'probabilities': {'通信': 2.0}}}}
    assert parse_choice_response(invalid).confidence is None


@pytest.mark.parametrize('raw', [0, 1, 0.85, '0.5'])
def test_parse_choice_response_accepts_boundary_confidence(raw: int | float | str) -> None:
    """0 と 1 を含む範囲内の confidence はそのまま受け入れる

    Args:
        raw: レスポンスに入る有効な confidence
    """
    payload = {'answers': {'category': {'type': 'choice', 'choice': '通信', 'confidence': raw}}}
    assert parse_choice_response(payload).confidence == pytest.approx(float(raw))


def test_match_rule_ignores_billing_month_tokens() -> None:
    """「6ガツブン ○○」のルールが「7ガツブン ○○」にも一致し、年月付きの電力会社も同一視される"""
    rules = [
        MerchantRule('6ガツブン エ-ユ-デンワリヨウリヨウ', '通信'),
        MerchantRule('トウキヨウデンリヨク26ネン07ガツ', '住居・光熱'),
    ]
    assert matched_category(rules, '7ガツブン エ-ユ-デンワリヨウリヨウ') == '通信'
    assert matched_category(rules, 'トウキヨウデンリヨク26ネン08ガツ') == '住居・光熱'
    assert matched_category(rules, 'エ-ユ-デンワリヨウリヨウ') == '通信'
    assert match_rule(rules, 'カンサイデンリヨク26ネン08ガツ') is None


def test_match_rule_partial_pattern_covers_varying_station_names() -> None:
    """「オートチャージ」のような共通部分のパターンは駅名が違っても部分一致し、請求月付きの部分パターンもキーで部分一致する"""
    rules = [MerchantRule('オートチャージ', '交通'), MerchantRule('7ガツブン エ-ユ-', '通信')]
    assert matched_category(rules, '北松戸駅 オートチャージ(リンク)') == '交通'
    assert matched_category(rules, '京成電鉄 新津田沼駅 オートチャージ(リンク)') == '交通'
    assert matched_category(rules, '8ガツブン エ-ユ-デンワリヨウリヨウ') == '通信'


def test_upsert_rule_merges_rules_with_same_merchant_key() -> None:
    """同じ加盟店キーのルールが複数あれば、先頭をパターン・カテゴリごと更新し、残りは削除される"""
    data = LedgerData(
        merchant_rules=[
            MerchantRule('7ガツブン X', '通信'),
            MerchantRule('Y', '外食'),
            MerchantRule('8ガツブン X', '外食'),
        ]
    )
    rule = upsert_rule(data, 'X', '住居・光熱')
    assert [(r.merchant_pattern, r.category) for r in data.merchant_rules] == [('X', '住居・光熱'), ('Y', '外食')]
    assert data.merchant_rules[0] is rule


def test_preview_rule_targets_matches_actual_upsert() -> None:
    """登録前の件数プレビューは、旧パターン・重複ルールが残る状態ではなく upsert_rule 後と同じ判定になる"""
    data = LedgerData(
        merchant_rules=[
            MerchantRule('テストデンリヨク 8ガツブン', '食費'),
            MerchantRule('デンリヨク ホンシヤ', '外食'),
        ],
        transactions=[
            Transaction('t1', date(2026, 8, 1), 'a', 'テストデンリヨク ホンシヤ', 100, category='外食'),
            Transaction('t2', date(2026, 9, 1), 'b', 'テストデンリヨク 9ガツブン', 100, category=''),
        ],
    )
    previewed = [t.id for t in preview_rule_targets(data, 'テストデンリヨク', 'none')]
    rule = upsert_rule(data, 'テストデンリヨク', '住居・光熱')
    actual = [t.id for t in rule_targets(data.transactions, data.merchant_rules, rule, 'none')]
    assert previewed == actual == ['t2']  # t1 は長い部分一致「デンリヨク ホンシヤ」が勝つ


def test_match_rule_prefers_exact_over_key_match_regardless_of_order() -> None:
    """完全一致のルールは、先に登録されたキー一致のルールより優先される"""
    rules = [
        MerchantRule('エ-ユ-デンワリヨウリヨウ', '通信'),
        MerchantRule('7ガツブン エ-ユ-デンワリヨウリヨウ', '外食'),
    ]
    assert matched_category(rules, '7ガツブン エ-ユ-デンワリヨウリヨウ') == '外食'
    assert matched_category(rules, '8ガツブン エ-ユ-デンワリヨウリヨウ') == '通信'
