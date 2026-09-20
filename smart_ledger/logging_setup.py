"""ログ設定。API キー・カード番号・会員番号はログに出さない前提で運用する。"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler

from .config import Config

_CONFIGURED = False

# 万一 16 桁前後のカード番号らしき数字列がログに混ざった場合のマスク
_CARD_LIKE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_KEY_LIKE = re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]+", re.IGNORECASE)


class SensitiveDataFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        masked = _CARD_LIKE.sub("****", msg)
        masked = _KEY_LIKE.sub(r"\1****", masked)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


def setup_logging(config: Config) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    config.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if config.debug else logging.INFO)

    file_handler = RotatingFileHandler(
        config.log_dir / "smart_ledger.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(SensitiveDataFilter())
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)  # PowerShell で赤字表示されないよう stdout に出す
    console.setFormatter(fmt)
    console.addFilter(SensitiveDataFilter())
    root.addHandler(console)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _CONFIGURED = True
