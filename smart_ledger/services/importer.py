"""CSV 取込のオーケストレーション（プレビュー → 確定）

- ファイル SHA-256 を imports.file_hash と照合し、同一ファイルなら取込済みと判定
- 明細単位は row_key（利用日・正規化加盟店・金額・同ファイル内の出現回数）を同じカード名の明細どうしで照合
- プレビュー結果は staging/<token>.json に一時保存し、「取り込む」で確定
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from ..constants import SOURCE_ERROR, SOURCE_RULE
from ..models import ImportRecord, LedgerData, Transaction, new_id, now_iso
from .classifier import ClassificationPipeline
from .csv_parser import parse_statement_bytes

logger = logging.getLogger(__name__)


@dataclass
class PreviewRow:
    """プレビュー画面に出す明細 1 行"""

    usage_date: str
    merchant_raw: str
    merchant_normalized: str
    amount: int
    card: str
    row_key: str
    source_line: int
    duplicate: bool
    existing_category: str = ''


@dataclass
class ImportPreview:
    """CSV 解析後・確定前の状態（staging に JSON で保存する）"""

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
        """JSON 文字列にする"""
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> ImportPreview:
        """JSON 文字列から復元する

        Args:
            text: to_json の出力
        """
        raw = json.loads(text)
        raw['rows'] = [PreviewRow(**r) for r in raw['rows']]
        return cls(**raw)


@dataclass
class ImportResult:
    """取込確定の結果"""

    import_id: str
    imported: int
    skipped_duplicates: int
    auto_accepted: int
    needs_review: int
    jev_errors: int
    rule_matched: int
    months: list[str]


class Importer:
    """CSV のプレビュー作成・一時保存・確定を行う"""

    def __init__(self, staging_dir: Path, pipeline: ClassificationPipeline, staging_keep: int = 10):
        """
        Args:
            staging_dir: プレビュー JSON の保存先
            pipeline: 新規明細の分類に使うパイプライン
            staging_keep: staging に残すプレビュー数
        """
        self.staging_dir = Path(staging_dir)
        self.pipeline = pipeline
        self.staging_keep = staging_keep

    # --------------------------------------------------------------- preview
    def preview(self, data: LedgerData, filename: str, content: bytes) -> ImportPreview:
        """CSV を解析し、重複判定を付けたプレビューを作って staging に保存する

        Args:
            data: 現在の全データ（重複判定に使う）
            filename: アップロードされたファイル名
            content: CSV のバイト列
        """
        parsed = parse_statement_bytes(content)
        self.pipeline.clear_cache()  # 前回の取込結果を持ち越さない（確定の再試行では使い回す）
        previous = data.has_file_hash(parsed.file_hash)
        existing_by_key = {(t.row_key, t.card): t for t in data.transactions if t.row_key}

        rows: list[PreviewRow] = []
        for r in parsed.rows:
            existing = existing_by_key.get((r.row_key, r.card))
            rows.append(
                PreviewRow(
                    usage_date=r.usage_date.isoformat(),
                    merchant_raw=r.merchant_raw,
                    merchant_normalized=r.merchant_normalized,
                    amount=r.amount,
                    card=r.card,
                    row_key=r.row_key,
                    source_line=r.source_line,
                    duplicate=existing is not None,
                    existing_category=existing.category if existing is not None else '',
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
        self.store(preview)
        logger.info(
            'import preview: file=%s rows=%d new=%d dup=%d already imported=%s',
            filename,
            len(rows),
            new_count,
            preview.duplicate_count,
            preview.already_imported,
        )
        return preview

    def store(self, preview: ImportPreview) -> None:
        """プレビューを staging/<token>.json に保存し、古いものを削除する

        Args:
            preview: 保存するプレビュー
        """
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        (self.staging_dir / f'{preview.token}.json').write_text(preview.to_json(), encoding='utf-8')
        self.cleanup_staging()

    def staging_path(self, token: str) -> Path | None:
        """トークンに対応する staging ファイルのパスを返す（token_urlsafe 以外の文字を含むトークンは None）

        Args:
            token: preview() が発行したトークン
        """
        if not token or not token.replace('-', '').replace('_', '').isalnum():
            return None

        return self.staging_dir / f'{token}.json'

    def load_preview(self, token: str) -> ImportPreview | None:
        """トークンからプレビューを復元する（不正なトークンや期限切れは None）

        Args:
            token: preview() が発行したトークン
        """
        path = self.staging_path(token)
        if path is None or not path.exists():
            return None

        return ImportPreview.from_json(path.read_text(encoding='utf-8'))

    def discard(self, token: str) -> None:
        """プレビューを破棄する（不正なトークンは無視する）

        Args:
            token: 破棄するプレビューのトークン
        """
        path = self.staging_path(token)
        if path is not None:
            path.unlink(missing_ok=True)

    def cleanup_staging(self) -> None:
        """staging_keep 件を超えた古いプレビューを削除する"""
        files = sorted(self.staging_dir.glob('*.json'), key=lambda p: p.stat().st_mtime)
        for old in files[: -self.staging_keep]:
            old.unlink(missing_ok=True)

    # ---------------------------------------------------------------- commit
    def commit(self, data: LedgerData, preview: ImportPreview) -> ImportResult:
        """新規行だけ分類して LedgerData に追加する（保存は呼び出し側が行う）

        Args:
            data: 追加先の全データ
            preview: 確定するプレビュー
        """
        existing_keys = data.existing_row_keys()  # プレビュー後に変わっている可能性があるので再確認
        categories = data.category_names()
        import_id = new_id('imp')
        imported_at = now_iso()

        counts = {'imported': 0, 'dup': 0, 'auto': 0, 'review': 0, 'errors': 0, 'rules': 0}
        months: set[str] = set()
        for row in preview.rows:
            if (row.row_key, row.card) in existing_keys:
                counts['dup'] += 1
                continue

            usage_date = date.fromisoformat(row.usage_date)
            result = self.pipeline.classify(
                row.merchant_normalized, row.amount, usage_date, categories, data.merchant_rules
            )
            tx = Transaction(
                id=new_id('tx'),
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
            existing_keys.add((row.row_key, row.card))
            months.add(tx.month)
            counts['imported'] += 1
            counts['rules'] += int(result.source == SOURCE_RULE)
            counts['errors'] += int(result.source == SOURCE_ERROR)
            if self.pipeline.is_auto_accepted(result):
                counts['auto'] += 1
            else:
                counts['review'] += 1

        data.imports.append(
            ImportRecord(
                import_id=import_id,
                filename=preview.filename,
                file_hash=preview.file_hash,
                imported_at=imported_at,
                card=preview.card,
                row_count=counts['imported'],
            )
        )
        data.transactions.sort(key=lambda t: (t.usage_date, t.imported_at, t.id))
        logger.info(
            'import committed: import id=%s imported=%d dup=%d auto=%d review=%d errors=%d',
            import_id,
            counts['imported'],
            counts['dup'],
            counts['auto'],
            counts['review'],
            counts['errors'],
        )
        return ImportResult(
            import_id=import_id,
            imported=counts['imported'],
            skipped_duplicates=counts['dup'],
            auto_accepted=counts['auto'],
            needs_review=counts['review'],
            jev_errors=counts['errors'],
            rule_matched=counts['rules'],
            months=sorted(months),
        )
