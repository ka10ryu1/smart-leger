"""ドメインモデルの変換関数のテスト"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from smart_ledger.constants import SOURCE_ERROR, SOURCE_JEV, SOURCE_MANUAL, SOURCE_RULE
from smart_ledger.models import Transaction, to_date


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('2026-08-15', date(2026, 8, 15)),
        ('2026/8/1', date(2026, 8, 1)),
        ('2026/08/01 00:00:00', date(2026, 8, 1)),
        (datetime(2026, 8, 15, 10, 30), date(2026, 8, 15)),
        (date(2026, 8, 15), date(2026, 8, 15)),
    ],
)
def test_to_date_accepts_excel_variants(value: object, expected: date) -> None:
    """Excel セルの日付表現（datetime / ISO / スラッシュ区切り・ゼロ埋め無し）を date にできる

    Args:
        value: セルの値
        expected: 期待する日付
    """
    assert to_date(value) == expected


def test_to_date_rejects_garbage() -> None:
    """日付として読めない値は ValueError"""
    with pytest.raises(ValueError):
        to_date('8月1日')


@pytest.mark.parametrize(
    ('category', 'source', 'confidence', 'expected'),
    [
        ('', '', None, True),
        ('その他', SOURCE_ERROR, None, True),
        ('食費', SOURCE_JEV, None, True),
        ('食費', SOURCE_JEV, 0.84, True),
        ('食費', SOURCE_JEV, 0.85, False),
        ('食費', SOURCE_RULE, None, False),
        ('食費', SOURCE_MANUAL, None, False),
    ],
)
def test_transaction_needs_review_states(category: str, source: str, confidence: float | None, expected: bool) -> None:
    """未分類・エラー・Jev の低 confidence だけを要確認にする

    Args:
        category: 明細のカテゴリ
        source: 分類元
        confidence: Jev の confidence
        expected: 要確認の期待値
    """
    tx = Transaction('tx', date(2026, 8, 1), 'A', 'A', 100, category, confidence, source)
    assert tx.needs_review(0.85) is expected
