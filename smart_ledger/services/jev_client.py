"""TypeSafe AI Jev (System One) API クライアント。

仕様(2026-09 時点の公式ドキュメント):
    POST https://api.typesafe.ai/v1/systemone
    Authorization: Bearer <API_KEY>
    body: {"model": "jev-latest", "state": <str|obj>, "questions": {<id>: {"type": "choice",
           "instructions": "...", "criteria": {<option>: <description>, ...}}}}
    response: {"model": "...", "answers": {<id>: {"type": "choice", "choice": "...",
               "probabilities": {...}, "confidence": 0.xx}}, "usage": {...}}

送信する情報は「加盟店名・金額・利用日」のみ。氏名・会員番号・カード番号は送らない。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

logger = logging.getLogger(__name__)

QUESTION_ID = "category"
RETRY_STATUSES = {429, 529, 502, 503}


class JevError(Exception):
    """Jev API 呼び出しに失敗した。"""


class JevNotConfigured(JevError):
    """API キーが未設定。"""


@dataclass
class JevChoice:
    choice: str
    confidence: float | None
    probabilities: dict[str, float]
    model: str | None = None


def build_request_body(
    merchant: str,
    amount: int,
    usage_date: date | str,
    categories: dict[str, str],
    model: str,
) -> dict[str, Any]:
    state = {
        "merchant": merchant,
        "amount_jpy": int(amount),
        "usage_date": usage_date.isoformat() if isinstance(usage_date, date) else str(usage_date),
    }
    return {
        "model": model,
        "state": state,
        "questions": {
            QUESTION_ID: {
                "type": "choice",
                "instructions": (
                    "これは日本のクレジットカード利用明細の1行です。"
                    "加盟店名(カナ表記や略称、店舗名+駅名などを含む)と金額から、"
                    "この支出が属する家計簿カテゴリを1つ選んでください。"
                ),
                "criteria": dict(categories),
            }
        },
    }


def parse_choice_response(payload: dict[str, Any], question_id: str = QUESTION_ID) -> JevChoice:
    """レスポンス JSON から choice / confidence / probabilities を取り出す。"""
    answers = payload.get("answers")
    if not isinstance(answers, dict) or question_id not in answers:
        raise JevError(f"Jev レスポンスに answers.{question_id} がありません")
    answer = answers[question_id]
    if not isinstance(answer, dict):
        raise JevError("Jev レスポンスの answer 形式が不正です")
    choice = answer.get("choice")
    if not isinstance(choice, str) or not choice:
        raise JevError("Jev レスポンスに choice がありません")
    probabilities_raw = answer.get("probabilities") or {}
    probabilities: dict[str, float] = {}
    if isinstance(probabilities_raw, dict):
        for k, v in probabilities_raw.items():
            try:
                probabilities[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
    confidence_raw = answer.get("confidence")
    confidence: float | None
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else None
    except (TypeError, ValueError):
        confidence = None
    if confidence is None and choice in probabilities:
        confidence = probabilities[choice]
    return JevChoice(choice=choice, confidence=confidence, probabilities=probabilities, model=payload.get("model"))


class JevClient:
    def __init__(
        self,
        api_key: str | None,
        model: str = "jev-latest",
        base_url: str = "https://api.typesafe.ai",
        timeout: float = 20.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/systemone"

    def choose_category(
        self, merchant: str, amount: int, usage_date: date | str, categories: dict[str, str]
    ) -> JevChoice:
        if not self.configured:
            raise JevNotConfigured("TYPESAFE_API_KEY が設定されていません")
        body = build_request_body(merchant, amount, usage_date, categories, self.model)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        attempt = 0
        delay = 1.0
        while True:
            attempt += 1
            try:
                with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                    response = client.post(self.endpoint, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    logger.warning("Jev 通信エラー(再試行 %d/%d): %s", attempt, self.max_retries, exc)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise JevError(f"Jev API への接続に失敗しました: {exc.__class__.__name__}") from exc

            if response.status_code in RETRY_STATUSES and attempt <= self.max_retries:
                logger.warning("Jev HTTP %d(再試行 %d/%d)", response.status_code, attempt, self.max_retries)
                time.sleep(delay)
                delay *= 2
                continue
            if response.status_code == 401:
                raise JevError("Jev API キーが無効です(401)。TYPESAFE_API_KEY を確認してください。")
            if response.status_code >= 400:
                detail = response.text[:200].replace("\n", " ")
                raise JevError(f"Jev API エラー HTTP {response.status_code}: {detail}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise JevError("Jev API のレスポンスが JSON ではありません") from exc
            choice = parse_choice_response(payload)
            logger.info(
                "Jev 分類: merchant=%s -> %s (confidence=%s)",
                merchant,
                choice.choice,
                f"{choice.confidence:.2f}" if choice.confidence is not None else "n/a",
            )
            return choice
