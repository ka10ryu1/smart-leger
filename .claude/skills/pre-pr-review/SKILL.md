---
name: pre-pr-review
description: PR 作成直前のセルフレビュー。差分をリスク分類し、検証(pytest/ruff/空行ツール)・独立コードレビュー・条件付きセキュリティレビュー・簡素化を行い、MUST FIX を修正して人間レビューに出せる状態にする。実装完了後、PR 作成前に /pre-pr-review で実行する。
argument-hint: "[<base-branch>] (省略時: main。積み上げブランチなら直前の feature ブランチを渡す)"
disable-model-invocation: true
allowed-tools:
  - Bash(git status *)
  - Bash(git diff *)
  - Bash(git log *)
  - Bash(git merge-base *)
  - Bash(git rev-parse *)
  - Bash(git branch *)
  - Bash(git show *)
  - Bash(.venv/bin/python -m pytest *)
  - Bash(.venv/bin/python -m ruff *)
  - Bash(.venv/bin/python tools/insert_block_blank_lines.py *)
  - Read
  - Edit
  - Grep
  - Glob
  - Skill
  - Agent
---

# Pre-PR Review

人間へレビュー依頼する前に、第三者視点で不具合・設計問題・セキュリティ問題・テスト不足・**不要なコード増加**を潰す。

## 大原則

1. **実装した自分の判断を信用しない。** コードレビューは必ず独立 context(`code-review` skill か Agent ツール)で行い、渡すのは diff・要求仕様・観点のみ。実装時の経緯や言い訳は渡さない。
2. **人間が対応する価値のある指摘だけ残す。** 好み・nit・仮説・formatter で直る問題は出さない。確信が持てない指摘は subagent に反証させ、反証されたら捨てる。
3. **コードは増やさない方向に倒す。** 行数が増える修正より、既存関数の再利用・削除で解決する案を優先する。
4. **途中経過は出さない。** 出力は最後の規定フォーマットのみ。停止して確認が要るときだけ 1〜2 行で報告する。
5. **出力は日本語。** 識別子・コマンド・エラーメッセージ・`LOW/MEDIUM/HIGH` はそのまま。

## 禁止事項

`git push` / merge / PR 作成 / commit(ユーザー指示がある場合のみ commit 可)/ 外部サービスへの書き込み。責務は「レビュー依頼できる状態にする」まで。

## Phase 1: 差分の把握

- base は `$ARGUMENTS`(省略時 `main`)。`git rev-parse --verify --quiet <base>`(無ければ `origin/<base>`)で存在確認し、無ければ候補を 1〜3 件示して**停止する**(黙って main に倒さない)。
- 対象は常に HEAD + 未 commit 変更。別ブランチを見たいときは `git worktree add ../<name> <branch>` でそのブランチを checkout してから実行する(検証・修正は作業ツリーが無いと成立しないため)。
- `git diff --stat <base>...HEAD` と `git diff <base>` を取る。**行数を記録する**: `git diff --shortstat <base>` の追加/削除行と、新規関数・クラス・モジュールの数。
- 確認する: 対応テストの有無、`requirements.txt` / `ruff.toml` / `pytest.ini` / `*.ps1` / `.env.example` の変更、Excel スキーマ(`constants.py`)・CSV パーサ・Jev 通信・ログ出力・ファイルパス(data/ backup)の変更。
- リスク分類:
  - **LOW**: typo、コメント、挙動を変えない小規模 refactor、テスト追加のみ
  - **MEDIUM**: 機能追加、バグ修正、ロジック変更、画面変更
  - **HIGH**: API キー / 個人情報(カード番号・会員番号・氏名)/ ログに出す値の追加 / Jev へ送る内容 / Excel 正本の書き込み経路 / バックアップ・`os.replace` / ファイルパス / 依存追加 のいずれかを含む
- リスクに応じて以降の Phase の実行・スキップを決める。全部を機械的に実行しない。

## Phase 2: 検証

CLAUDE.md の 4 コマンドをそのまま実行する(スイートは 1 秒未満なので絞らない)。

```bash
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python tools/insert_block_blank_lines.py --check smart_ledger tests tools app.py
.venv/bin/python -m pytest -q -m "not live"
```

- 失敗したら base 側で同じテストが通るかを `git worktree` か `git show <base>:<file>` で確かめ、今回起因か判定する。既存障害は直さず残課題に「既存」と書く。
- **受理範囲を狭める変更**(正規表現の厳格化、条件追加、`match`→`fullmatch`、`merchant_rules` / `normalize_merchant` / `MONTH_PATTERN` 等)を含む場合: 旧版(`git show <base>:<file>`)と新版を同一プロセスで読み、`tests/fixtures/` の CSV・`constants.py` の語彙・既存ルールから作ったコーパスで判定差を列挙し、意図しない縮小を MUST FIX にする。テストは書いたケースしか見ないため、緑でも実施する。
- **Jev へ渡す `instructions` / `criteria` 文言を変えた場合**: 組み立て後の payload を base と HEAD で生成してバイト差分を取り、矛盾する指示や解釈が一意に決まらない文がないか確認する。
- 「この経路は変わらない」と書くなら、その経路を実際に実行して base と比較した結果を根拠にする(コード上の位置は根拠にならない)。

## Phase 3: 独立コードレビュー

1. Skill ツールで `code-review` を実行(effort は LOW→`low`、MEDIUM→`medium`、HIGH→`high`。`--fix` は付けない)。
2. 補完レビューを Agent ツール(`model: fable`、無ければ既定)に**必ず**投げる。渡すのは diff・要求仕様と次の観点だけ:
   - **要求との一致**: 依頼どおりか、意図しない挙動変更・互換性破壊はないか
   - **正しさ**: None / 境界 / エンコーディング(cp932)/ 金額・日付の型と精度 / 例外経路 / 二重取込
   - **テスト**: 変更ロジックと edge case に対応するテストがあるか、`parametrize` でまとめられる重複がないか
   - **コード量**: 新規 helper/class ごとに「既存の関数・`constants.py` で代替できないか」を答えさせる。不要な防御コード、一度しか使わない抽象化、死んだ分岐、互換用シム、消し忘れの旧実装を列挙させる
   - **prose**: コメント・docstring・ログ文言から検証できる主張(数値・同一性・保証・経路)を抜き、実行して食い違いのみ報告させる

## Phase 4: セキュリティレビュー(HIGH のみ)

Skill ツールで `security-review` を実行する。CLAUDE.md の禁止事項を重点確認する: API キー・カード番号・会員番号・氏名のログ出力、Jev へ加盟店名・金額・利用日以外を送っていないか、`data/` `logs/` `.env` `*.csv` `*.xlsx` の追跡、パス結合、一時ファイル → 検証 → バックアップ → `os.replace` の順序。該当しなければ「スキップ」と記載。

## Phase 5: 修正

指摘を分類する:

- **MUST FIX**: bug / security / regression / 要求違反 / 重要なテスト不足。加えて severity に関わらず次は MUST FIX: household.xlsx に入る値(金額・日付・カテゴリ・classification_source)が変わる指摘、二重計上の可能性、バックアップ経路の破壊、コメント・docstring・ログ文言が実挙動と逆の指摘
- **SHOULD FIX**: 明確に改善価値があるもの。**行数が減る修正はここに含める**
- **OPTIONAL**: 好み。修正せず最終出力に 1 行残すだけ

MUST FIX は修正する。SHOULD FIX は小さく安全なら修正する。subagent が「実測せよ」と書いた項目は残課題にせず Phase 2 に戻す。

## Phase 6: 簡素化

追加行数が 30 行を超える、または新規 helper / class がある場合、Skill ツールで `simplify` を実行する(自動で修正を適用する)。適用後の diff を見て、**挙動を変えるもの・可読性を落とすもの・CLAUDE.md の規約(docstring・空行・定数の置き場)に反するものだけ revert する**。行数が減るだけの変更は残す。

## Phase 7: 再検証

1. 修正したら Phase 2 の 4 コマンドを再実行する。
2. 修正が大きければ `code-review`(`low`)を再実行、軽微なら修正 diff の確認で足りる。
3. ループ上限は 3 周。収束しなければ NOT READY で残件を報告する。
4. リネーム・実装差し替えをしたら `git diff <base> | grep -nE '^\+.*(旧名)'` で残留を確認する。
5. コミットメッセージ・PR 下書き(あれば)の主張と最終 HEAD を突き合わせる(例示の文言、テスト件数、検証コマンドの再現性)。

## 最終出力(日本語・各項目 3〜5 文以内)

```
## Pre-PR レビュー結果

リスク: MEDIUM
行数: +120 / −40 → 簡素化後 +85 / −40(新規関数 2)

### 検証
* ruff check / format / 空行ツール: PASS
* pytest: PASS (51 passed, 2 deselected)

### レビュー
検出 3 / 修正 3 / 未対応 0
1. `importer.py:88` 同一 CSV の再取込で件数が二重計上される分岐を修正
2. ...

### セキュリティレビュー
スキップ(HIGH 該当なし)

### 簡素化
2 件適用(`normalize_merchant` を再利用 / 一度しか使わない helper を inline)

### 残課題
なし

### 判定
人間レビュー依頼 OK
モデル: メイン=<セッションのモデル ID> / 補完 subagent=<指定値または既定>
```

問題が残る場合は判定を `人間レビュー依頼 NG(要対応)` とし、理由を最大 5 項目。実行しなかった Phase は「スキップ」と理由を 1 行で書く。
