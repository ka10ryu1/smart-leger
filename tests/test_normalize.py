from __future__ import annotations

import pytest

from smart_ledger.services.normalize import normalize_merchant


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  ＫＹＡＳＨ ", "KYASH"),
        ("Ｊ：ＣＯＭ　サ－ビス", "J:COM サ-ビス"),
        ("千駄ケ谷駅　　オートチャージ", "千駄ケ谷駅 オートチャージ"),
        ("ｼﾞｮﾅｶﾞﾁｭｰ", "ジョナガチュー"),
        (None, ""),
    ],
)
def test_normalize_merchant(raw, expected):
    assert normalize_merchant(raw) == expected
