"""モジュール横断で使う定数（CLAUDE.md の方針により専用ファイルに集約）

カテゴリの初期値、classification_source の値、CSV / Excel のスキーマ定義など、
複数モジュールから参照される値だけをここに置く（1 モジュール内で完結する値は関数の既定引数にする）
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- カテゴリ
DEFAULT_CATEGORIES: tuple[str, ...] = (
    '食費',
    '外食',
    '日用品・買い物',
    '住居・光熱',
    '通信',
    '交通',
    '保険・税金',
    '娯楽・サブスク',
    '衣服・美容',
    'その他',
)
FALLBACK_CATEGORY = 'その他'
UNCLASSIFIED_LABEL = '未分類'  # category が空の明細の表示・集計用ラベル

# Jev のカテゴリ選択に渡す説明文（criteria）。カテゴリ自体は categories シートで管理する
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    '食費': 'スーパー・食料品店・食材の購入、飲料(自宅で消費するもの)',
    '外食': 'レストラン・カフェ・居酒屋・ファストフード・フードデリバリー',
    '日用品・買い物': 'ドラッグストア・ホームセンター・雑貨・家電・ネット通販の一般的な買い物',
    '住居・光熱': '家賃・住宅ローン・電気・ガス・水道',
    '通信': '携帯電話料金・インターネット回線・ケーブルテレビ・プロバイダ',
    '交通': '電車・バス・タクシー・交通系ICチャージ・ガソリン・高速道路・駐車場',
    '保険・税金': '生命保険・医療保険・損害保険の保険料、税金・公的支払い',
    '娯楽・サブスク': '動画・音楽配信、ゲーム、書籍、映画、旅行、趣味、各種サブスクリプション',
    '衣服・美容': '衣料品・靴・美容院・化粧品・エステ',
    'その他': '上記のどれにも当てはまらない、または判断できない支出(送金・チャージ系サービスを含む)',
}

# ---------------------------------------------------- classification_source
SOURCE_RULE = 'rule'
SOURCE_JEV = 'jev'
SOURCE_MANUAL = 'manual'
SOURCE_ERROR = 'error'  # Jev 呼び出し失敗・API キー未設定など
SOURCE_LABELS: dict[str, str] = {
    SOURCE_RULE: 'ルール',
    SOURCE_JEV: 'Jev',
    SOURCE_MANUAL: '手動',
    SOURCE_ERROR: 'エラー',
}

# ------------------------------------------------------------------- CSV
CSV_ENCODINGS: tuple[str, ...] = ('utf-8-sig', 'cp932')  # cp932 ⊇ shift_jis、utf-8-sig は BOM 無しも復号できる
CSV_HEADER_MARKER = 'ご利用年月日'
CSV_DATE_COLUMNS: tuple[str, ...] = ('ご利用年月日', '利用日', 'ご利用日', '利用年月日')
CSV_MERCHANT_COLUMNS: tuple[str, ...] = ('ご利用箇所', 'ご利用先', '利用先', '加盟店名', 'ご利用店名', '利用店名')
CSV_AMOUNT_COLUMNS: tuple[str, ...] = ('ご利用額', '利用金額', 'ご利用金額', '利用額', '金額')
CSV_REFUND_COLUMNS: tuple[str, ...] = ('払戻額', '返品額', '返金額')
CSV_CARD_META_KEYS: tuple[str, ...] = ('対象カード', 'カード名', 'ご利用カード')
CSV_DATE_FORMATS: tuple[str, ...] = ('%Y/%m/%d', '%Y-%m-%d', '%Y年%m月%d日', '%Y.%m.%d', '%Y%m%d')

# ----------------------------------------------------------------- Excel
EXCEL_COLUMN_WIDTHS: dict[str, int] = {
    'id': 18,
    'usage_date': 12,
    'merchant_raw': 36,
    'merchant_normalized': 36,
    'merchant_pattern': 36,
    'amount': 12,
    'category': 16,
    'confidence': 11,
    'classification_source': 14,
    'card': 22,
    'import_id': 18,
    'imported_at': 20,
    'created_at': 20,
    'row_key': 20,
    'memo': 30,
    'filename': 32,
    'file_hash': 20,
    'row_count': 10,
    'transaction_id': 18,
    'sort_order': 10,
}

# --------------------------------------------------------------- ログ / UI
# 万一 13〜19 桁のカード番号らしき数字列がログに混ざった場合にマスクする
LOG_CARD_NUMBER_PATTERN = re.compile(r'(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)')  # 13〜19 桁（区切りは空白か -）
LOG_BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]+', re.IGNORECASE)
MONTH_PATTERN = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
