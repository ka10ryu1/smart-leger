"""CSV 取込（プレビュー → 確定）のテスト"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from smart_ledger.constants import DEFAULT_CATEGORIES
from smart_ledger.models import Allocation, ClassificationResult, MerchantRule
from smart_ledger.services.classifier import ClassificationPipeline, NullClassifier
from smart_ledger.services.excel_repository import ExcelRepository
from smart_ledger.services.importer import Importer, ImportNotFoundError, summarize_import, undo_import


class StubClassifier:
    """Jev の代わりに加盟店名ごとの固定結果を返す分類器"""

    def __init__(self, mapping: dict[str, tuple[str, float]]):
        """
        Args:
            mapping: 加盟店名 → (カテゴリ, confidence)。無い加盟店は ('その他', 0.3)
        """
        self.mapping = mapping
        self.calls: list[str] = []

    def classify(
        self, merchant_normalized: str, amount: int, usage_date: date, categories: dict[str, str]
    ) -> ClassificationResult:
        """固定結果を返し、呼び出された加盟店を記録する

        Args:
            merchant_normalized: 正規化済み加盟店名
            amount: 金額（未使用）
            usage_date: 利用日（未使用）
            categories: カテゴリ（未使用）
        """
        self.calls.append(merchant_normalized)
        cat, confidence = self.mapping.get(merchant_normalized, ('その他', 0.3))
        return ClassificationResult(category=cat, confidence=confidence, source='jev')


def test_preview_and_commit_then_duplicate_detection(
    tmp_path: Path, repo: ExcelRepository, fixture_csv_bytes: bytes
) -> None:
    """初回は全件新規、同一ファイルは取込済み判定、追記ファイルは追加行だけ新規になる

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_csv_bytes: CP932 の fixture
    """
    stub = StubClassifier({'テストデンリヨク 8ガツブン': ('住居・光熱', 0.95), 'サンプルカフェ': ('外食', 0.7)})
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(stub, 0.85))
    data = repo.load()

    preview = importer.preview(data, 'a.csv', fixture_csv_bytes)
    assert preview.new_count == 10 and preview.duplicate_count == 0 and not preview.already_imported
    assert importer.load_preview(preview.token) is not None

    result = importer.commit(data, preview)
    repo.save(data)
    assert result.imported == 10
    assert result.auto_accepted == 1
    assert result.needs_review == 9
    assert sorted(result.months) == ['2026-08', '2026-09']
    assert len(set(stub.calls)) == len(stub.calls)

    data = repo.load()
    preview2 = importer.preview(data, 'a.csv', fixture_csv_bytes)
    assert preview2.already_imported is True
    assert preview2.duplicate_count == 10 and preview2.new_count == 0

    extra_line = '2026/09/10,アタラシイミセ,"2,000",,"2,000",１回払,,"2,000",,   ,\r\n'.encode('cp932')
    preview3 = importer.preview(data, 'b.csv', fixture_csv_bytes + extra_line)
    assert preview3.already_imported is False
    assert preview3.new_count == 1 and preview3.duplicate_count == 10

    result3 = importer.commit(data, preview3)
    assert result3.imported == 1 and result3.skipped_duplicates == 10


def test_commit_records_import_and_card(tmp_path: Path, repo: ExcelRepository, fixture_csv_bytes: bytes) -> None:
    """API キー無しでは全件 error / 要確認になり、imports にカード名と件数が記録される

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_csv_bytes: CP932 の fixture
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(NullClassifier(), 0.85))
    data = repo.load()
    preview = importer.preview(data, 'a.csv', fixture_csv_bytes)
    result = importer.commit(data, preview)
    assert result.jev_errors == 10 and result.needs_review == 10
    assert data.imports[0].file_hash == preview.file_hash
    assert data.imports[0].row_count == 10
    assert all(t.card == 'テストカード' for t in data.transactions)
    assert all(t.classification_source == 'error' and t.category == 'その他' for t in data.transactions)


def test_discard_ignores_traversal_token(tmp_path: Path) -> None:
    """staging 外を指すトークンでは何も削除しない

    Args:
        tmp_path: pytest の一時ディレクトリ
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(NullClassifier(), 0.85))
    outside = tmp_path / 'outside.json'
    outside.write_text('{}', encoding='utf-8')
    importer.discard('../outside')
    assert outside.exists()
    assert importer.load_preview('../outside') is None


def test_same_rows_from_another_card_are_not_duplicates(
    tmp_path: Path, repo: ExcelRepository, fixture_csv_bytes: bytes
) -> None:
    """別カードの CSV に同日・同加盟店・同金額の明細があっても重複扱いにしない

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_csv_bytes: CP932 の fixture
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(NullClassifier(), 0.85))
    data = repo.load()
    importer.commit(data, importer.preview(data, 'a.csv', fixture_csv_bytes))
    other_card = fixture_csv_bytes.replace('テストカード'.encode('cp932'), 'カゾクカード'.encode('cp932'))
    preview = importer.preview(data, 'b.csv', other_card)
    assert preview.new_count == 10 and preview.duplicate_count == 0
    assert importer.preview(data, 'c.csv', fixture_csv_bytes).duplicate_count == 10


def test_commit_retry_reuses_classification_cache(
    tmp_path: Path, repo: ExcelRepository, fixture_csv_bytes: bytes
) -> None:
    """保存失敗後に同じプレビューを確定し直しても分類器を呼び直さない（キャッシュはプレビュー時にだけクリア）

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_csv_bytes: CP932 の fixture
    """
    stub = StubClassifier({})
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(stub, 0.85))
    preview = importer.preview(repo.load(), 'a.csv', fixture_csv_bytes)
    importer.commit(repo.load(), preview)
    first_calls = len(stub.calls)
    importer.commit(repo.load(), preview)  # 保存に失敗して再試行した想定
    assert first_calls > 0 and len(stub.calls) == first_calls
    importer.preview(repo.load(), 'a.csv', fixture_csv_bytes)
    importer.commit(repo.load(), preview)
    assert len(stub.calls) == first_calls * 2


def test_undo_import_removes_only_that_import(tmp_path: Path, repo: ExcelRepository, fixture_csv_bytes: bytes) -> None:
    """取り消しは対象 import の明細・内訳・履歴だけを消し、他の取込・ルール・カテゴリは残す

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_csv_bytes: CP932 の fixture
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(NullClassifier(), 0.85))
    data = repo.load()
    first = importer.commit(data, importer.preview(data, 'a.csv', fixture_csv_bytes))
    extra_line = '2026/09/10,アタラシイミセ,"2,000",,"2,000",１回払,,"2,000",,   ,\r\n'.encode('cp932')
    second = importer.commit(data, importer.preview(data, 'b.csv', fixture_csv_bytes + extra_line))
    assert second.imported == 1

    # 手動修正・メモ・内訳を付けておく
    edited = next(t for t in data.transactions if t.import_id == first.import_id)
    edited.classification_source = 'manual'
    edited.memo = 'メモ'
    data.allocations.append(Allocation(edited.id, '食費', edited.amount))
    kept = next(t for t in data.transactions if t.import_id == second.import_id)
    data.allocations.append(Allocation(kept.id, '食費', kept.amount))
    data.merchant_rules.append(MerchantRule('サンプル', '食費'))

    summary = summarize_import(data, next(i for i in data.imports if i.import_id == first.import_id))
    assert (summary.transactions, summary.manual_edits, summary.allocations) == (10, 1, 1)

    result = undo_import(data, first.import_id)
    assert result.transactions == 10
    assert len(data.transactions) == 1 and data.transactions[0].import_id == second.import_id
    assert [a.transaction_id for a in data.allocations] == [kept.id]
    assert [i.import_id for i in data.imports] == [second.import_id]
    assert len(data.merchant_rules) == 1
    assert len(data.category_names()) == len(DEFAULT_CATEGORIES)

    # 履歴が消えたので同じ CSV を取り込み直せる。b.csv 取込時に重複としてスキップされた 10 行は
    # a.csv 側の明細のままだったので、a.csv の取り消しで消え、再取込では全行が新規になる
    preview = importer.preview(data, 'a.csv', fixture_csv_bytes)
    assert preview.already_imported is False
    assert preview.new_count == 10 and preview.duplicate_count == 0
    # b.csv は履歴（ハッシュ）が残るので「取込済み」表示になるが、消えた 10 行は新規として取り込める
    preview = importer.preview(data, 'b.csv', fixture_csv_bytes + extra_line)
    assert preview.already_imported is True and preview.new_count == 10
    result = importer.commit(data, preview)
    assert (result.imported, result.skipped_duplicates) == (10, 1)


def test_commit_keeps_rows_previewed_as_duplicate_skipped(
    tmp_path: Path, repo: ExcelRepository, fixture_csv_bytes: bytes
) -> None:
    """プレビューで重複だった行は、確定までに取り消しで消えていても取り込まない（画面の件数どおりに確定する）

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_csv_bytes: CP932 の fixture
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(NullClassifier(), 0.85))
    data = repo.load()
    first = importer.commit(data, importer.preview(data, 'a.csv', fixture_csv_bytes))
    extra_line = '2026/09/10,アタラシイミセ,"2,000",,"2,000",１回払,,"2,000",,   ,\r\n'.encode('cp932')
    stale = importer.preview(data, 'b.csv', fixture_csv_bytes + extra_line)
    assert stale.new_count == 1 and stale.duplicate_count == 10
    undo_import(data, first.import_id)

    result = importer.commit(data, stale)
    assert (result.imported, result.skipped_duplicates) == (1, 10)
    with pytest.raises(ValueError, match='新規の明細がありません'):
        importer.commit(data, stale)


def test_undo_import_unknown_id_raises(repo: ExcelRepository) -> None:
    """存在しない import_id は ImportNotFoundError

    Args:
        repo: 一時ディレクトリのリポジトリ
    """
    with pytest.raises(ImportNotFoundError):
        undo_import(repo.load(), 'imp_nothing')


def test_bank_csv_commit_sets_kind_and_category_without_classifier(
    tmp_path: Path, repo: ExcelRepository, fixture_bank_csv_bytes: bytes
) -> None:
    """銀行 CSV は許可リストでカテゴリが決まるため分類器を呼ばず、収入は kind=income で記録される

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    stub = StubClassifier({})
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(stub, 0.85))
    data = repo.load()

    preview = importer.preview(data, 'bank.csv', fixture_bank_csv_bytes)
    assert (preview.profile, preview.card) == ('bank', '銀行口座')
    assert preview.new_count == 5 and preview.excluded_lines == 4

    result = importer.commit(data, preview)
    assert stub.calls == []  # Jev には 1 件も問い合わせない
    assert (result.imported, result.income, result.rule_matched, result.needs_review) == (5, 2, 5, 0)

    loans = [t for t in data.transactions if t.category == '住宅ローン']
    solar = [t for t in data.transactions if t.category == '売電収入']
    assert [t.amount for t in loans] == [69000, 70000, 70000]
    assert all(t.kind == 'expense' and t.card == '銀行口座' for t in loans)
    assert [t.amount for t in solar] == [6500, 7000]
    assert all(t.is_income and t.classification_source == 'rule' for t in solar)


def test_bank_csv_commit_adds_missing_categories(
    tmp_path: Path, repo: ExcelRepository, fixture_bank_csv_bytes: bytes
) -> None:
    """許可リストのカテゴリが categories シートに無い旧ファイルでは、取込時に追加される

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(StubClassifier({}), 0.85))
    data = repo.load()
    data.categories = [c for c in data.categories if c.category not in ('住宅ローン', '売電収入')]

    result = importer.commit(data, importer.preview(data, 'bank.csv', fixture_bank_csv_bytes))
    assert sorted(result.added_categories) == ['住宅ローン', '売電収入']  # 追加順は明細の並び順に従う
    assert sorted(data.category_names()[-2:]) == ['住宅ローン', '売電収入']
    assert data.category_criteria()['売電収入'].startswith('太陽光発電')


def test_bank_csv_duplicate_detection_ignores_added_period(
    tmp_path: Path, repo: ExcelRepository, fixture_bank_csv_bytes: bytes
) -> None:
    """期間を重ねてダウンロードしても、重なった行は重複として弾かれる

    Args:
        tmp_path: pytest の一時ディレクトリ
        repo: 一時ディレクトリのリポジトリ
        fixture_bank_csv_bytes: CP932 の銀行 fixture
    """
    importer = Importer(tmp_path / 'staging', ClassificationPipeline(StubClassifier({}), 0.85))
    data = repo.load()
    importer.commit(data, importer.preview(data, 'bank.csv', fixture_bank_csv_bytes))

    # 新しい月の住宅ローン 1 行を先頭（新しい日付が先）に足した再ダウンロード
    lines = fixture_bank_csv_bytes.decode('cp932').split('\r\n')
    added = '"2026/10/28","約定返済　円　住宅","70,000",,"120,000","-"'
    preview = importer.preview(data, 'bank2.csv', '\r\n'.join([lines[0], added, *lines[1:]]).encode('cp932'))
    assert preview.new_count == 1 and preview.duplicate_count == 5
    assert importer.commit(data, preview).imported == 1
