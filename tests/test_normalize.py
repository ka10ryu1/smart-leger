"""加盟店名正規化のテスト"""

from __future__ import annotations

import pytest

from smart_ledger.services.normalize import normalize_merchant


@pytest.mark.parametrize(
    'raw,expected',
    [
        ('  ＫＹＡＳＨ ', 'KYASH'),
        ('Ｊ：ＣＯＭ　サ－ビス', 'J:COM サ-ビス'),
        ('千駄ケ谷駅　　オートチャージ', '千駄ケ谷駅 オートチャージ'),
        ('ｼﾞｮﾅｶﾞﾁｭｰ', 'ジョナガチュー'),
        (None, ''),
    ],
)
def test_normalize_merchant(raw: str | None, expected: str) -> None:
    """NFKC 正規化・全角スペース変換・連続空白の整理が行われる

    Args:
        raw: 入力
        expected: 期待する正規化結果
    """
    assert normalize_merchant(raw) == expected
