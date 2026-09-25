"""Flask ルーティング（Blueprint 'ledger' に画面ごとのモジュールのルートを登録する）

画面: ダッシュボード / 年間表 / 明細一覧 / 要確認 / 明細編集 / 手動明細の追加 / CSV 取込 / ルール / カテゴリ /
スマホで開く・PIN 入力（LAN モードのみ）
"""

from __future__ import annotations

# 画面ごとのモジュールは import 時に bp へルートを登録する
from . import categories, dashboard, edit, imports, lan, manual, rules, transactions  # noqa: F401
from .common import Services, bp, build_services

__all__ = ['Services', 'bp', 'build_services']
