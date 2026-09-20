"""加盟店名の正規化と、月ごとに揺れる部分を除いた「加盟店キー」の算出"""

from __future__ import annotations

import re
import unicodedata


def normalize_merchant(raw: str | None) -> str:
    """加盟店名を正規化する（NFKC → 前後空白除去 → 連続空白を 1 つに）

    全角英数・全角スペース・半角カナは NFKC で標準形に寄せる。
    元データ（merchant_raw）は呼び出し側で必ず別に保持すること

    Args:
        raw: CSV 上の加盟店名（None 可）
    """
    if raw is None:
        return ''

    text = unicodedata.normalize('NFKC', str(raw))
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def merchant_key(
    merchant: str,
    variable_token_patterns: tuple[str, ...] = (
        r'(?<!\d)\d{2,4}ネン\s*\d{1,2}ガツ(ブン)?',  # 26ネン07ガツ
        r'(?<!\d)\d{1,2}ガツ(ブン)?',  # 7ガツブン / 8ガツ
        r'(?<!\d)\d{2,4}年\s*\d{1,2}月(分)?',  # 2026年8月分
        r'(?<!\d)\d{1,2}月分',  # 8月分
        r'(?<![\d/])\d{4}/(0?[1-9]|1[0-2])(/(0?[1-9]|[12]\d|3[01]))?(?![\d/])',  # 2026/08 や 2026/08/10
        r'(?<![\d/])(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])(?![\d/])',  # 08/10（月日として妥当な範囲のみ。24/7 などは残す）
        r'(?<![A-Za-z0-9])(令和|[Rr])\d{1,2}年(?!度)(\s*\d{1,2}月(分)?)?',  # R7年 / r7年 / 令和7年8月（「年度」は残す）
    ),
) -> str:
    """加盟店名から、請求月など月ごとに変わるトークンを取り除いたキーを返す

    「7ガツブン エ-ユ-デンワリヨウリヨウ」→「エ-ユ-デンワリヨウリヨウ」、
    「トウキヨウデンリヨク26ネン07ガツ」→「トウキヨウデンリヨク」のように、同じ加盟店の月次請求を同一視するために使う
    （店舗コードなど日付以外の数字は残す。何も除かれなければ正規化した名前をそのまま返し、除いた結果が空になる場合も正規化した名前を返す）

    Args:
        merchant: 加盟店名（normalize_merchant() を通してから処理する）
        variable_token_patterns: 取り除くトークンの正規表現（NFKC 正規化後の文字列に対して適用）
    """
    normalized = normalize_merchant(merchant)
    key = normalized
    for pattern in variable_token_patterns:
        key = re.sub(pattern, ' ', key)

    if key == normalized:
        return normalized

    # トークンを除いた跡に残る空白や区切り記号（「デンリヨク 」「・ガス」など）を整える。
    # 「-」はカナの長音（「エ-ユ-」）として名前の一部になりうるので削らない
    key = re.sub(r'\s+', ' ', key).strip(' /・')
    return key or normalized
