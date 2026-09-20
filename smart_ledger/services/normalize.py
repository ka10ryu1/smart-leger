"""加盟店名の正規化"""

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
