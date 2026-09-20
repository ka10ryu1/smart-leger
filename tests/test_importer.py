"""CSV 取込（プレビュー → 確定）のテスト"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from smart_ledger.models import ClassificationResult
from smart_ledger.services.classifier import ClassificationPipeline, NullClassifier
from smart_ledger.services.excel_repository import ExcelRepository
from smart_ledger.services.importer import Importer


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
        self, merchant_normalized: str, amount: int, usage_date: date, categories: list[str]
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
