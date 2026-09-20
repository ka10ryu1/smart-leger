from __future__ import annotations

from smart_ledger.models import ClassificationResult
from smart_ledger.services.classifier import ClassificationPipeline, NullClassifier
from smart_ledger.services.importer import Importer


class StubClassifier:
    """Jev の代わり: 加盟店名に応じた固定結果を返す。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def classify(self, merchant_normalized, amount, usage_date, categories):
        self.calls.append(merchant_normalized)
        cat, conf = self.mapping.get(merchant_normalized, ("その他", 0.3))
        return ClassificationResult(category=cat, confidence=conf, source="jev")


def test_preview_and_commit_then_duplicate_detection(tmp_path, repo, fixture_csv_bytes):
    stub = StubClassifier({"テストデンリヨク 8ガツブン": ("住居・光熱", 0.95), "サンプルカフェ": ("外食", 0.7)})
    importer = Importer(tmp_path / "staging", ClassificationPipeline(stub, 0.85))
    data = repo.load()

    preview = importer.preview(data, "a.csv", fixture_csv_bytes)
    assert preview.new_count == 10 and preview.duplicate_count == 0 and not preview.already_imported
    assert importer.load_preview(preview.token) is not None

    result = importer.commit(data, preview)
    repo.save(data)
    assert result.imported == 10
    assert result.auto_accepted == 1  # 住居・光熱 0.95 のみ
    assert result.needs_review == 9
    assert sorted(result.months) == ["2026-08", "2026-09"]
    assert len(set(stub.calls)) == len(stub.calls)  # 同一加盟店は 1 回だけ

    # 同じファイルを再度 → ファイルハッシュで取込済み判定 + 全行重複
    data = repo.load()
    preview2 = importer.preview(data, "a.csv", fixture_csv_bytes)
    assert preview2.already_imported is True
    assert preview2.duplicate_count == 10 and preview2.new_count == 0

    # 1 行追加した別ファイル → 追加行だけ新規、同日同加盟店同金額 2 行は両方重複扱い
    extra = fixture_csv_bytes + "2026/09/10,アタラシイミセ,\"2,000\",,\"2,000\",１回払,,\"2,000\",,   ,\r\n".encode("cp932")
    preview3 = importer.preview(data, "b.csv", extra)
    assert preview3.already_imported is False
    assert preview3.new_count == 1 and preview3.duplicate_count == 10
    result3 = importer.commit(data, preview3)
    assert result3.imported == 1 and result3.skipped_duplicates == 10


def test_commit_records_import_and_card(tmp_path, repo, fixture_csv_bytes):
    importer = Importer(tmp_path / "staging", ClassificationPipeline(NullClassifier(), 0.85))
    data = repo.load()
    preview = importer.preview(data, "a.csv", fixture_csv_bytes)
    result = importer.commit(data, preview)
    assert result.jev_errors == 10 and result.needs_review == 10
    assert data.imports[0].file_hash == preview.file_hash
    assert data.imports[0].row_count == 10
    assert all(t.card == "テストカード" for t in data.transactions)
    assert all(t.classification_source == "error" and t.category == "その他" for t in data.transactions)
