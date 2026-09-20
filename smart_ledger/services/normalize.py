"""加盟店名の正規化。"""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


def normalize_merchant(raw: str | None) -> str:
    """Unicode NFKC 正規化 → 前後空白除去 → 連続空白を 1 つに。

    全角英数・全角スペース・半角カナは NFKC で半角/全角の標準形に寄せる。
    元データ(merchant_raw)は呼び出し側で必ず別に保持すること。
    """
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw))
    text = text.replace("　", " ")
    text = _WS.sub(" ", text).strip()
    return text
