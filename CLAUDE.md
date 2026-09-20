# CLAUDE.md

Smart Ledger（クレジットカード利用明細 CSV を取り込む個人用家計簿 Web アプリ）の開発規約。
Python 3.12 / Flask / openpyxl / httpx。Windows 直接実行を前提とし、SQL・Docker・WSL 前提の構成は使わない。

## コマンド

```powershell
# Windows
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format .
.\.venv\Scripts\python.exe tools\insert_block_blank_lines.py smart_ledger tests tools app.py
.\.venv\Scripts\python.exe -m pytest -q            # 通常テスト（実 Jev API は既定で除外）
.\.venv\Scripts\python.exe -m pytest -q -m live    # ライブテストを明示実行
```

Linux / WSL では `.venv/bin/python` に読み替える。設定は `ruff.toml`（シングルクォート、行長 120）と `pytest.ini`（`live` マーカー）にある。

## コードスタイル

- Python の文字列はシングルクォーテーションを優先する。f-string 内のネストなど構文上必要な場合のみダブルクォーテーションを使う。docstring は慣例どおり三重ダブルクォート（`"""`）で書く（`ruff format` がこの規則で整形する）
- インデントが戻る場所（if / for / while / with / try などのブロック終了後）には空行を入れて可読性を確保する。ネストの深さは問わない（ループ内・ガード節の中でも適用する）。連続するガード節（早期 return / continue / raise）の間にも空行を入れる。ただし else / elif / except / finally など同一構文の継続キーワードの前には入れない。`tools/insert_block_blank_lines.py` で機械的に挿入・検証できる（`--check` で確認のみ）
- モジュールレベルの外部変数（定数）はなるべく使わない。1 モジュール内で完結する値は関数のオプション引数（デフォルト値付き）で受け取る。複数モジュールから参照する値は `smart_ledger/constants.py` に置いてインポートする（カテゴリ初期値、classification_source、CSV / Excel のスキーマ、正規表現など）
- 次はフレームワーク上必要なモジュール変数として許容する: `logger = logging.getLogger(__name__)`、Flask の `bp = Blueprint(...)`、`config.py` の `PROJECT_ROOT`、テストの `pytestmark`

## ログメッセージ

- 対象は `logger.*` に渡すメッセージ。画面表示（flash / テンプレート）と例外メッセージはユーザー向けなので日本語で書く
- 英語の小文字始まりで統一する。環境変数名（`TYPESAFE_API_KEY` 等）のみ大文字を許容する
- `動作ラベル: key=value` の形式にする（例: `'csv parsed: encoding=%s rows=%d'`）
- 区切りは `:` に統一する（`—` やカンマ区切りは使わない）
- snake_case は使わずスペース区切りにする（例: `import_committed` → `import committed`）
- API キー、カード番号、会員番号、氏名はログに出さない（`logging_setup.SensitiveDataFilter` がカード番号らしき数字列と Bearer トークンをマスクするが、フィルタに頼らず最初から出さない）

## 型アノテーション

- 関数の引数と戻り値には型アノテーションを付ける
- `from __future__ import annotations` を使い、`dict[str, Any]` や `bool | None` など Python 3.10+ 記法で統一する

## docstring

- モジュールには概要を示す docstring を付ける
- クラス・関数には日本語で docstring を付ける（テスト関数も対象）
- 末尾「。」は省略する。複数文になる場合は括弧で補足に変換する（例: `"""〜する。〜の想定。"""` → `"""〜する（〜の想定）"""`）
- 引数がある関数は `Args:` セクションで各引数を説明する。pytest のフィクスチャ引数も同様に書く
- 戻り値が自明でない場合（例: タプルや None を返しうる等）は `Returns:` セクションを付ける
- `logging.Filter.filter` のようなフレームワークのオーバーライドで引数が自明な場合は `Args:` を省略してよい
- `__init__` の docstring は `Args:` だけでよい（クラスの説明はクラス docstring に書く）

## エラーハンドリング

- `except Exception: pass` で例外を握りつぶさない。最低でも `logger.warning` でログを残す
- 外部ライブラリや自作関数が返す `Optional` 型（`str | None` 等）は早期に `if x is None: raise` または早期 return でガードし、以降は narrowing された型の変数を使う。`assert x is not None` による narrowing は使わない
- Jev API の失敗は取込全体を止めず、`category='その他'` / `classification_source='error'` / 要確認として継続する（`services/classifier.py`）
- Excel の保存は一時ファイル → 検証 → 世代バックアップ → `os.replace` の順で行い、正本を壊さない（`services/excel_repository.py`）

## async

- 現状このリポジトリに async コードはない。導入する場合、async 関数内では `time.sleep` ではなく `asyncio.sleep` を使う（`time.sleep` はイベントループをブロックする）

## セキュリティ

- `.env`、`data/`（household.xlsx・バックアップ）、`logs/`、`*.csv`、`*.xlsx` はコミットしない。テスト用 CSV は `tests/fixtures/` に架空データのみ置く
- 実際のカード明細 CSV をリポジトリにコピーしない。構造確認に使う場合も内容をログや出力に残さない
- Jev へ送るのは加盟店名・金額・利用日のみ

## リント・テスト

- コード変更後は `ruff check .` と `ruff format .`、`tools/insert_block_blank_lines.py` を実行し、`pytest` を通す
- Jev API はテストで実際に呼ばない（`httpx.MockTransport` を使う）。実 API を呼ぶテストは `@pytest.mark.live` を付け、API キーが無ければスキップされるようにする
