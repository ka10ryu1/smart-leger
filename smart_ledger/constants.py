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
    '住宅ローン',
    '通信',
    '交通',
    '保険・税金',
    '娯楽・サブスク',
    '衣服・美容',
    'その他',
    '売電収入',
)
FALLBACK_CATEGORY = 'その他'
UNCLASSIFIED_LABEL = '未分類'  # category が空の明細の表示・集計用ラベル
FORMULA_PREFIXES = '=+-@'  # この文字で始まるセルは Excel が数式として扱う（カテゴリ名の検証と CSV エクスポートで使う）

# カテゴリの既定説明（categories シートの初期値、Jev のカテゴリ選択に渡す criteria）。カテゴリ自体は categories シートで管理する
# 住宅ローン・売電収入は銀行明細専用で、カード明細を分類する Jev の選択肢には出さない（importer.Importer.commit）
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    '食費': 'スーパー・食料品店・食材の購入、飲料(自宅で消費するもの)',
    '外食': 'レストラン・カフェ・居酒屋・ファストフード・フードデリバリーなど、店で食事をした支出',
    '日用品・買い物': 'ドラッグストア・ホームセンター・雑貨・家電・ネット通販の一般的な買い物',
    '住居・光熱': '家賃・電気(デンリヨク)・ガス・水道の料金',
    '住宅ローン': '住宅ローンの返済(約定返済)。銀行口座からの引き落とし',
    '通信': '携帯電話料金(デンワリヨウリヨウ、au・ドコモ・ソフトバンク・楽天モバイル)・インターネット回線・ケーブルテレビ(J:COM など)・プロバイダ',
    '交通': '電車・バス・タクシー・交通系ICのオートチャージ(駅名+オートチャージ)・ガソリン・高速道路・駐車場',
    '保険・税金': '生命保険・医療保険・損害保険の保険料(ホケンリヨウ)、税金・公的支払い',
    '娯楽・サブスク': '動画・音楽配信、ゲーム、書籍、映画、旅行、趣味、各種サブスクリプション',
    '衣服・美容': '衣料品・靴・美容院・化粧品・エステ',
    'その他': '上記のどれにも当てはまらない、または判断できない支出(送金・チャージ系サービスを含む)',
    '売電収入': '太陽光発電の売電による入金',
}

# ------------------------------------------------------------------ kind
# 明細が支出か収入か（transactions.kind）。旧ファイルの空欄は expense として読む
KIND_EXPENSE = 'expense'
KIND_INCOME = 'income'
KIND_LABELS: dict[str, str] = {KIND_EXPENSE: '支出', KIND_INCOME: '収入'}

# ------------------------------------------------------------- import_id
# 画面から手動で追加した明細の transactions.import_id（imports シートには載せず、取込の取り消しの対象にもならない）
MANUAL_IMPORT_ID = 'manual'

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
CSV_DATE_FORMATS: tuple[str, ...] = ('%Y/%m/%d', '%Y-%m-%d', '%Y年%m月%d日', '%Y.%m.%d', '%Y%m%d')

# カード利用明細 CSV（プロファイル card）
CSV_HEADER_MARKER = 'ご利用年月日'
CSV_DATE_COLUMNS: tuple[str, ...] = ('ご利用年月日', '利用日', 'ご利用日', '利用年月日')
CSV_MERCHANT_COLUMNS: tuple[str, ...] = ('ご利用箇所', 'ご利用先', '利用先', '加盟店名', 'ご利用店名', '利用店名')
CSV_AMOUNT_COLUMNS: tuple[str, ...] = ('ご利用額', '利用金額', 'ご利用金額', '利用額', '金額')
CSV_REFUND_COLUMNS: tuple[str, ...] = ('払戻額', '返品額', '返金額')
CSV_CARD_META_KEYS: tuple[str, ...] = ('対象カード', 'カード名', 'ご利用カード')

# 銀行口座の入出金明細 CSV（プロファイル bank）
BANK_CSV_HEADER_MARKER = '日付'
# ヘッダー行は先頭セルの「日付」で検出するので、日付列の候補はそれだけで足りる
BANK_CSV_DATE_COLUMNS: tuple[str, ...] = (BANK_CSV_HEADER_MARKER,)
BANK_CSV_DESCRIPTION_COLUMNS: tuple[str, ...] = ('内容', 'お取引内容', '摘要')
BANK_CSV_WITHDRAWAL_COLUMNS: tuple[str, ...] = ('出金金額', 'お引出し', '支払金額')
BANK_CSV_DEPOSIT_COLUMNS: tuple[str, ...] = ('入金金額', 'お預入れ', '預入金額')
# プロファイル固定の口座名。重複判定は (row_key, card) で行うため、変えると取込済みの明細と照合できなくなる
BANK_CSV_ACCOUNT = '銀行口座'
# 取り込む行の許可リスト。(内容の正規表現, カテゴリ) で、どれにも一致しない行は「対象外」として読み飛ばす。
# 正規表現は normalize_merchant()（NFKC + 空白整形）を通した内容に対して re.search で照合する
BANK_CSV_TARGETS: tuple[tuple[str, str], ...] = (
    (r'約定返済.*住宅', '住宅ローン'),
    (r'トウデンPG\s*コウニユウ', '売電収入'),
)

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
    'kind': 10,
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
    'description': 48,
}

# --------------------------------------------------------------- ログ / UI
# 万一 13〜19 桁のカード番号らしき数字列がログに混ざった場合にマスクする
LOG_CARD_NUMBER_PATTERN = re.compile(r'(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)')  # 13〜19 桁（区切りは空白か -）
LOG_BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.]+', re.IGNORECASE)
MONTH_PATTERN = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
# 明細一覧の確信度の絞り込み: 半角の 0〜1 で小数 2 桁まで（フォームの step=0.01 に合わせ、全角・指数表記・3 桁以上は弾く）
CONFIDENCE_FILTER_PATTERN = re.compile(r'^(?:[01]|0?\.[0-9]{1,2}|1\.0{1,2})$')
YEAR_PATTERN = re.compile(r'^\d{4}$')  # \d は isdecimal と同じ Unicode Nd なので全角数字も通る（int() できる）
