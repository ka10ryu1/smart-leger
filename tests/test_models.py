"""ドメインモデルの変換関数のテスト"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from smart_ledger.models import to_date


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
