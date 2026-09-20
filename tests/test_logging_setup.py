"""ログのマスクフィルタのテスト"""

from __future__ import annotations

import logging

import pytest

from smart_ledger.logging_setup import SensitiveDataFilter


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        ('card=4111 1111 1111 1111 ok', 'card=**** ok'),
        ('card=4111-1111-1111-1111', 'card=****'),
        ('Authorization: Bearer abc.def-123', 'Authorization: Bearer ****'),
        ('amount=12345 rows=16', 'amount=12345 rows=16'),
    ],
)
def test_sensitive_data_filter_masks(message: str, expected: str) -> None:
    """カード番号らしき数字列と Bearer トークンだけがマスクされる

    Args:
        message: 元のログメッセージ
        expected: フィルタ後のメッセージ
    """
    record = logging.LogRecord('t', logging.INFO, __file__, 1, message, None, None)
    assert SensitiveDataFilter().filter(record) is True
    assert record.getMessage() == expected
