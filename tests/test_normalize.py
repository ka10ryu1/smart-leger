"""加盟店名正規化のテスト"""

from __future__ import annotations

import pytest

from smart_ledger.services.normalize import merchant_key, normalize_merchant


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


@pytest.mark.parametrize(
    'normalized,expected',
    [
        ('7ガツブン エ-ユ-デンワリヨウリヨウ', 'エ-ユ-デンワリヨウリヨウ'),
        ('トウキヨウデンリヨク26ネン07ガツ', 'トウキヨウデンリヨク'),
        ('2026年8月分 ガス料金', 'ガス料金'),
        ('ガス料金 2026/08/10', 'ガス料金'),
        ('北松戸駅 オートチャージ(リンク)', '北松戸駅 オートチャージ(リンク)'),
        ('セブン-イレブン 12345', 'セブン-イレブン 12345'),
        ('7-ELEVEN', '7-ELEVEN'),
        ('8ガツブン', '8ガツブン'),
        ('スシロ-', 'スシロ-'),
        ('- 2026/08 ガス -', 'ガス'),
        ('r7年 ジドウシャゼイ', 'ジドウシャゼイ'),
    ],
)
def test_merchant_key_strips_only_billing_month_tokens(normalized: str, expected: str) -> None:
    """請求月らしいトークンだけを除き、店舗コードなどの数字は残す（空になるなら元の名前）

    Args:
        normalized: 正規化済みの加盟店名
        expected: 期待するキー
    """
    assert merchant_key(normalized) == expected
