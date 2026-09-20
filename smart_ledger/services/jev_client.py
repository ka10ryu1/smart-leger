"""TypeSafe AI Jev (System One) API クライアント

仕様（2026-09 時点の公式ドキュメント）:
    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <API_KEY>
    body: {"model": "jev-latest", "state": <str|obj>, "questions": {<id>: {"type": "choice",
           "instructions": "...", "criteria": {<option>: <description>, ...}}}}
    response: {"model": "...", "answers": {<id>: {"type": "choice", "choice": "...",
               "probabilities": {...}, "confidence": 0.xx}}, "usage": {...}}

送信する情報は「加盟店名・金額・利用日」のみ（氏名・会員番号・カード番号は送らない）
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class JevError(Exception):
    """Jev API 呼び出しに失敗した"""


@dataclass
class JevChoice:
    """choice 型の回答"""

    choice: str
    confidence: float | None
    probabilities: dict[str, float]


def build_request_body(
    merchant: str,
    amount: int,
    usage_date: date | str,
    categories: dict[str, str],
    model: str,
    question_id: str = 'category',
) -> dict[str, Any]:
    """Jev へ送るリクエスト JSON を組み立てる

    Args:
        merchant: 正規化済みの加盟店名
        amount: 金額（円）
        usage_date: 利用日
        categories: カテゴリ名 → 説明文（choice の criteria）
        model: モデル名（例: jev-latest）
        question_id: questions のキー
    """
    state = {
        'merchant': merchant,
        'amount_jpy': int(amount),
        'usage_date': usage_date.isoformat() if isinstance(usage_date, date) else str(usage_date),
    }
    return {
        'model': model,
        'state': state,
        'questions': {
            question_id: {
                'type': 'choice',
                'instructions': (
                    'これは日本のクレジットカード利用明細の1行です。'
                    '加盟店名(カナ表記や略称、店舗名+駅名などを含む)と金額から、'
                    'この支出が属する家計簿カテゴリを1つ選んでください。'
                ),
                'criteria': dict(categories),
            }
        },
    }


def validate_confidence(value: Any) -> float | None:
    """confidence を 0〜1 の有限な float に変換する（変換不能・NaN・inf・範囲外は None）

    Args:
        value: レスポンス中の confidence（None なら None を返す）
    """
    if value is None:
        return None

    try:
        confidence = float(value)
    except (TypeError, ValueError):
        logger.warning('jev confidence ignored: reason=not a number value=%r', value)
        return None

    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        logger.warning('jev confidence ignored: reason=out of range value=%r', value)
        return None

    return confidence


def parse_choice_response(payload: dict[str, Any], question_id: str = 'category') -> JevChoice:
    """レスポンス JSON から choice / confidence / probabilities を取り出す

    confidence が無い・不正なときは probabilities[choice] で補い、それも不正なら None（要確認扱い）にする

    Args:
        payload: レスポンス JSON
        question_id: 取り出す questions のキー
    """
    answers = payload.get('answers')
    if not isinstance(answers, dict) or question_id not in answers:
        raise JevError(f'Jev レスポンスに answers.{question_id} がありません')

    answer = answers[question_id]
    if not isinstance(answer, dict):
        raise JevError('Jev レスポンスの answer 形式が不正です')

    choice = answer.get('choice')
    if not isinstance(choice, str) or not choice:
        raise JevError('Jev レスポンスに choice がありません')

    probabilities: dict[str, float] = {}
    probabilities_raw = answer.get('probabilities') or {}
    if isinstance(probabilities_raw, dict):
        for key, value in probabilities_raw.items():
            try:
                probabilities[str(key)] = float(value)
            except (TypeError, ValueError):
                logger.warning('jev probability ignored: option=%s value=%r', key, value)

    confidence = validate_confidence(answer.get('confidence'))
    if confidence is None and choice in probabilities:
        confidence = validate_confidence(probabilities[choice])

    return JevChoice(choice=choice, confidence=confidence, probabilities=probabilities)


class JevClient:
    """httpx で Jev API を呼び出す"""

    def __init__(
        self,
        api_key: str | None,
        model: str = 'jev-latest',
        base_url: str = 'https://api.typesafe.ai',
        timeout: float = 20.0,
        max_retries: int = 2,
        retry_statuses: tuple[int, ...] = (429, 529, 502, 503),
        transport: httpx.BaseTransport | None = None,
    ):
        """
        Args:
            api_key: TYPESAFE_API_KEY（None なら未設定扱い）
            model: モデル名
            base_url: API のベース URL
            timeout: HTTP タイムアウト秒
            max_retries: 再試行回数（通信エラー・retry_statuses の場合）
            retry_statuses: 指数バックオフで再試行する HTTP ステータス
            transport: テスト用の httpx トランスポート
        """
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip('/')
        self.max_retries = max_retries
        self.retry_statuses = retry_statuses
        self.client = httpx.Client(timeout=timeout, transport=transport)  # 接続を使い回す

    @property
    def configured(self) -> bool:
        """API キーが設定されているか"""
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        """System One エンドポイントの URL"""
        return f'{self.base_url}/v1/systemone'

    def choose_category(
        self, merchant: str, amount: int, usage_date: date | str, categories: dict[str, str]
    ) -> JevChoice:
        """加盟店にカテゴリを 1 つ選ばせる（失敗時は JevError）

        Args:
            merchant: 正規化済みの加盟店名
            amount: 金額（円）
            usage_date: 利用日
            categories: カテゴリ名 → 説明文
        """
        if not self.configured:
            raise JevError('TYPESAFE_API_KEY が設定されていません')

        body = build_request_body(merchant, amount, usage_date, categories, self.model)
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'}
        response = self.post_with_retry(body, headers)
        try:
            payload = response.json()
        except ValueError as exc:
            raise JevError('Jev API のレスポンスが JSON ではありません') from exc

        choice = parse_choice_response(payload)
        if choice.choice not in categories:
            raise JevError(f'Jev が不明なカテゴリを返しました: {choice.choice}')

        logger.info(
            'jev classified: merchant=%s choice=%s confidence=%s',
            merchant,
            choice.choice,
            f'{choice.confidence:.2f}' if choice.confidence is not None else 'n/a',
        )
        return choice

    def post_with_retry(self, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        """通信エラーと retry_statuses を指数バックオフで再試行しながら POST する

        Args:
            body: リクエスト JSON
            headers: HTTP ヘッダー

        Returns:
            2xx のレスポンス（4xx / 5xx は JevError）
        """
        attempt = 0
        delay = 1.0
        while True:
            attempt += 1
            try:
                response = self.client.post(self.endpoint, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt > self.max_retries:
                    raise JevError(f'Jev API への接続に失敗しました: {exc.__class__.__name__}') from exc

                logger.warning('jev request failed: attempt=%d max=%d error=%s', attempt, self.max_retries, exc)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code in self.retry_statuses and attempt <= self.max_retries:
                logger.warning(
                    'jev http retry: status=%d attempt=%d max=%d', response.status_code, attempt, self.max_retries
                )
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 401:
                raise JevError('Jev API キーが無効です(401)。TYPESAFE_API_KEY を確認してください。')

            if response.status_code >= 400:
                detail = response.text[:200].replace('\n', ' ')
                raise JevError(f'Jev API エラー HTTP {response.status_code}: {detail}')

            return response
