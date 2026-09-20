"""ログ設定（API キー・カード番号・会員番号はログに出さない前提で運用する）"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Config
from .constants import LOG_BEARER_PATTERN, LOG_CARD_NUMBER_PATTERN


class SensitiveDataFilter(logging.Filter):
    """カード番号らしき数字列と Bearer トークンをマスクするフィルタ"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        masked = LOG_CARD_NUMBER_PATTERN.sub('****', msg)
        masked = LOG_BEARER_PATTERN.sub(r'\1****', masked)
        if masked != msg:
            record.msg = masked
            record.args = ()

        return True


def setup_logging(config: Config, handler_name: str = 'smart_ledger_file') -> None:
    """ファイル（ローテーション）と stdout にログを出す設定を行う（2 回目以降の呼び出しは何もしない）

    Args:
        config: ログディレクトリなどの設定
        handler_name: 設定済み判定に使うファイルハンドラ名
    """
    root = logging.getLogger()
    if any(handler.get_name() == handler_name for handler in root.handlers):
        return

    config.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter('%(asctime)s %(levelname)s [%(name)s] %(message)s')
    root.setLevel(logging.DEBUG if config.debug else logging.INFO)

    file_handler = RotatingFileHandler(
        config.log_dir / 'smart_ledger.log', maxBytes=2_000_000, backupCount=5, encoding='utf-8'
    )
    file_handler.set_name(handler_name)
    file_handler.setFormatter(fmt)
    file_handler.addFilter(SensitiveDataFilter())
    root.addHandler(file_handler)

    # PowerShell で赤字表示されないよう stderr ではなく stdout に出す
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(SensitiveDataFilter())
    root.addHandler(console)

    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
