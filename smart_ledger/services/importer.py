"""CSV 取込のオーケストレーション(プレビュー → 確定)。

- ファイル SHA-256 を imports.file_hash と照合し、同一ファイルなら取込済みと判定
- 明細単位は row_key(利用日・正規化加盟店・金額・同ファイル内の出現回数)で照合
- プレビュー結果は data/staging/<token>.json に一時保存し、「取り込む」で確定
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from ..models import (
    SOURCE_ERROR,
    SOURCE_RULE,
    ImportRecord,
    LedgerData,
    Transaction,
    new_id,
    now_iso,
)
from .classifier import ClassificationPipeline
from .csv_parser import parse_statement_bytes

logger = logging.getLogger(__name__)


@dataclass
class PreviewRow:
    usage_date: str
    merchant_raw: str
    merchant_normalized: str
    amount: int
    card: str
    row_key: str
    source_line: int
    duplicate: bool
    existing_category: str = ""


@dataclass
class ImportPreview:
    token: str
    filename: str
    file_hash: str
    card: str
    encoding: str
    header_line: int
    already_imported: bool
    previous_import: dict | None
    rows: list[PreviewRow]
    new_count: int
    duplicate_count: int
    warnings: list[str] = field(default_factory=list)
    skipped_lines: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "ImportPreview":
        raw = json.loads(text)
        raw["rows"] = [PreviewRow(**r) for r in raw["rows"]]
        return cls(**raw)


@dataclass
class ImportResult:
    import_id: str
    imported: int
    skipped_duplicates: int
    auto_accepted: int
    needs_review: int
    jev_errors: int
    rule_matched: int
    months: list[str]


class Importer:
    def __init__(self, staging_dir: Path, pipeline: ClassificationPipeline):
        self.staging_dir = Path(staging_dir)
        self.pipeline = pipeline

    # --------------------------------------------------------------- preview
    def preview(self, data: LedgerData, filename: str, content: bytes) -> ImportPreview:
        parsed = parse_statement_bytes(content)
        previous = data.has_file_hash(parsed.file_hash)
        existing_keys = data.existing_row_keys()
        existing_by_key = {t.row_key: t for t in data.transactions if t.row_key}

        rows: list[PreviewRow] = []
        for r in parsed.rows:
            dup = r.row_key in existing_keys
            rows.append(
                PreviewRow(
                    usage_date=r.usage_date.isoformat(),
                    merchant_raw=r.merchant_raw,
                    merchant_normalized=r.merchant_normalized,
                    amount=r.amount,
                    card=r.card,
                    row_key=r.row_key,
                    source_line=r.source_line,
                    duplicate=dup,
                    existing_category=existing_by_key[r.row_key].category if dup else "",
                )
            )
        new_count = sum(1 for r in rows if not r.duplicate)
        preview = ImportPreview(
            token=secrets.token_urlsafe(16),
            filename=filename,
            file_hash=parsed.file_hash,
            card=parsed.card,
            encoding=parsed.encoding,
            header_line=parsed.header_line,
            already_imported=previous is not None,
            previous_import=asdict(previous) if previous else None,
            rows=rows,
            new_count=new_count,
            duplicate_count=len(rows) - new_count,
            warnings=parsed.warnings,
            skipped_lines=parsed.skipped_lines,
        )
        self._store(preview)
        logger.info(
            "取込プレビュー: file=%s rows=%d new=%d dup=%d already_imported=%s",
            filename,
            len(rows),
            new_count,
            preview.duplicate_count,
            preview.already_imported,
        )
        return preview

    def _store(self, preview: ImportPreview) -> None:
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        (self.staging_dir / f"{preview.token}.json").write_text(preview.to_json(), encoding="utf-8")
        self._cleanup_staging(keep=10)

    def load_preview(self, token: str) -> ImportPreview | None:
        if not token or not token.replace("-", "").replace("_", "").isalnum():
            return None
        path = self.staging_dir / f"{token}.json"
        if not path.exists():
            return None
        return ImportPreview.from_json(path.read_text(encoding="utf-8"))

    def discard(self, token: str) -> None:
        path = self.staging_dir / f"{token}.json"
        path.unlink(missing_ok=True)

    def _cleanup_staging(self, keep: int) -> None:
        files = sorted(self.staging_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-keep]:
            old.unlink(missing_ok=True)

    # ---------------------------------------------------------------- commit
    def commit(self, data: LedgerData, preview: ImportPreview) -> ImportResult:
        """新規行だけ分類して LedgerData に追加する(保存は呼び出し側)。"""
        existing_keys = data.existing_row_keys()  # プレビュー後に変わっている可能性があるので再確認
        categories = data.category_names()
        import_id = new_id("imp")
        imported_at = now_iso()
        self.pipeline.clear_cache()

        imported = auto = review = errors = rules = dup = 0
        months: set[str] = set()
        for row in preview.rows:
            if row.row_key in existing_keys:
                dup += 1
                continue
            usage_date = date.fromisoformat(row.usage_date)
            result = self.pipeline.classify(
                row.merchant_normalized, row.amount, usage_date, categories, data.merchant_rules
            )
            tx = Transaction(
                id=new_id("tx"),
                usage_date=usage_date,
                merchant_raw=row.merchant_raw,
                merchant_normalized=row.merchant_normalized,
                amount=row.amount,
                category=result.category,
                confidence=result.confidence,
                classification_source=result.source,
                card=row.card or preview.card,
                import_id=import_id,
                imported_at=imported_at,
                row_key=row.row_key,
            )
            data.transactions.append(tx)
            existing_keys.add(row.row_key)
            imported += 1
            months.add(tx.month)
            if result.source == SOURCE_RULE:
                rules += 1
            if result.source == SOURCE_ERROR:
                errors += 1
            if self.pipeline.is_auto_accepted(result):
                auto += 1
            else:
                review += 1

        data.imports.append(
            ImportRecord(
                import_id=import_id,
                filename=preview.filename,
                file_hash=preview.file_hash,
                imported_at=imported_at,
                card=preview.card,
                row_count=imported,
            )
        )
        data.transactions.sort(key=lambda t: (t.usage_date, t.imported_at, t.id))
        logger.info(
            "取込確定: import_id=%s imported=%d dup=%d auto=%d review=%d errors=%d",
            import_id,
            imported,
            dup,
            auto,
            review,
            errors,
        )
        return ImportResult(
            import_id=import_id,
            imported=imported,
            skipped_duplicates=dup,
            auto_accepted=auto,
            needs_review=review,
            jev_errors=errors,
            rule_matched=rules,
            months=sorted(months),
        )
