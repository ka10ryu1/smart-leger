"""CSV 取込のオーケストレーション（プレビュー → 確定）

- カード明細 CSV と銀行口座明細 CSV のどちらも同じ流れで扱う（種別は csv_parser がヘッダーから判定する）
- ファイル SHA-256 を imports.file_hash と照合し、同一ファイルなら取込済みと表示する（確定できるかは新規行の有無で決まる）
- 明細単位は row_key（利用日・正規化加盟店・金額・同ファイル内の出現回数）を同じカード名の明細どうしで照合
- プレビュー結果は staging/<token>.json に一時保存し、「取り込む」で確定
- 取り消し（undo_import）は import_id 単位で明細・内訳・履歴を削除する（加盟店ルールは残す）
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from ..constants import BANK_CSV_TARGETS, KIND_EXPENSE, KIND_INCOME, SOURCE_ERROR, SOURCE_MANUAL, SOURCE_RULE
from ..models import ImportRecord, LedgerData, Transaction, new_id, now_iso
from .categories import ensure_categories
from .classifier import ClassificationPipeline
from .csv_parser import parse_statement_bytes
from .merchant_rules import prepare_rules

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
    kind: str = KIND_EXPENSE
    category_hint: str = ''  # CSV の許可リストで決まったカテゴリ（空なら分類器に任せる）

    @property
    def is_income(self) -> bool:
        """収入の行か（kind が income）"""
        return self.kind == KIND_INCOME


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
    excluded_lines: int = 0
    profile: str = 'card'  # 旧バージョンが保存したプレビュー JSON には無いので既定値付き

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

    def new_rows(self, data: LedgerData) -> list[PreviewRow]:
        """プレビューで新規だった行のうち、今も取込済み明細に無いもの（プレビュー後に取り込まれた行を除く）

        Args:
            data: 現在の全データ
        """
        existing = data.existing_row_keys()
        return [r for r in self.rows if not r.duplicate and (r.row_key, r.card) not in existing]


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
    income_count: int = 0  # 取り込んだ明細のうち収入の件数
    added_categories: list[str] = field(default_factory=list)  # 許可リストのカテゴリが無かったので足したもの


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
                    kind=r.kind,
                    category_hint=r.category_hint,
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
            profile=parsed.profile,
            already_imported=previous is not None,
            previous_import=asdict(previous) if previous else None,
            rows=rows,
            new_count=new_count,
            duplicate_count=len(rows) - new_count,
            warnings=parsed.warnings,
            skipped_lines=parsed.skipped_lines,
            excluded_lines=parsed.excluded_lines,
        )
        self.store(preview)
        logger.info(
            'import preview: profile=%s rows=%d new=%d dup=%d excluded=%d already imported=%s',
            parsed.profile,
            len(rows),
            new_count,
            preview.duplicate_count,
            parsed.excluded_lines,
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
        """プレビューで新規だった行だけ分類して LedgerData に追加する（保存は呼び出し側が行う）

        銀行明細の許可リストで決まったカテゴリが categories シートに無ければ、あわせて追加する

        Args:
            data: 追加先の全データ
            preview: 確定するプレビュー

        Raises:
            ValueError: 取り込める新規行が無い（プレビュー後に取り込まれた、または取込済み CSV）
        """
        rows = preview.new_rows(data)
        if not rows:
            raise ValueError(
                '取り込める新規の明細がありません（プレビュー後に取り込まれたか、すでに取り込み済みの CSV です）。'
            )

        # 名前 → 説明（categories シートの description を Jev に渡す）。許可リストのカテゴリ（住宅ローン・売電収入）は
        # 銀行明細にしか付かず、銀行明細は Jev に問い合わせないので、カード明細の選択肢から外す
        bank_only = {category for _, category in BANK_CSV_TARGETS}
        categories = {name: desc for name, desc in data.category_criteria().items() if name not in bank_only}
        import_id = new_id('imp')
        imported_at = now_iso()
        prepared_rules = prepare_rules(data.merchant_rules)

        counts = dict.fromkeys(('imported', 'auto', 'review', 'errors', 'rules', 'income'), 0)
        counts['dup'] = len(preview.rows) - len(rows)
        months: set[str] = set()
        # 許可リストのカテゴリがそのまま採用された行のカテゴリ（重複は ensure_categories が除く）
        hinted_categories: list[str] = []
        for row in rows:
            usage_date = date.fromisoformat(row.usage_date)
            result = self.pipeline.classify(
                row.merchant_normalized, row.amount, usage_date, categories, prepared_rules, row.category_hint
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
                kind=row.kind,
            )
            data.transactions.append(tx)
            months.add(tx.month)
            counts['imported'] += 1
            counts['income'] += int(tx.is_income)
            counts['rules'] += int(result.source == SOURCE_RULE)
            counts['errors'] += int(result.source == SOURCE_ERROR)
            if self.pipeline.is_auto_accepted(result):
                counts['auto'] += 1
            else:
                counts['review'] += 1

            if row.category_hint and result.category == row.category_hint:
                hinted_categories.append(result.category)

        # 許可リストのカテゴリ（住宅ローン・売電収入など）が古いファイルの categories シートに無ければ足す。
        # 加盟店ルールで別カテゴリに振られた行は対象にしない（改名・削除したカテゴリを復活させないには加盟店ルールが要る）
        added_categories = ensure_categories(data, hinted_categories)

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
            'import committed: import id=%s profile=%s imported=%d income=%d dup=%d auto=%d review=%d errors=%d',
            import_id,
            preview.profile,
            counts['imported'],
            counts['income'],
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
            income_count=counts['income'],
            added_categories=added_categories,
        )


# ------------------------------------------------------------------- undo
class ImportNotFoundError(ValueError):
    """指定した import_id の取込履歴が無い"""


@dataclass
class ImportUndoSummary:
    """取り消しで消える内容の件数（確認ダイアログと結果表示に使う）"""

    filename: str
    transactions: int
    manual_edits: int  # カテゴリを手動変更した、またはメモを書いた明細
    allocations: int


def summarize_import(data: LedgerData, record: ImportRecord) -> ImportUndoSummary:
    """取込に属する明細・手動修正・内訳の件数を数える

    Args:
        data: 全データ
        record: 取込履歴
    """
    txs = [t for t in data.transactions if t.import_id == record.import_id]
    tx_ids = {t.id for t in txs}
    return ImportUndoSummary(
        filename=record.filename,
        transactions=len(txs),
        manual_edits=sum(1 for t in txs if t.classification_source == SOURCE_MANUAL or t.memo),
        allocations=sum(1 for a in data.allocations if a.transaction_id in tx_ids),
    )


def undo_import(data: LedgerData, import_id: str) -> ImportUndoSummary:
    """取込を取り消す（明細・内訳・履歴を削除。加盟店ルールとカテゴリは残す）

    履歴が消えるためファイルハッシュの重複判定も外れ、同じ CSV を取り込み直せる

    Args:
        data: 全データ
        import_id: 取り消す取込 ID

    Returns:
        削除した件数のまとめ
    """
    record = next((i for i in data.imports if i.import_id == import_id), None)
    if record is None:
        raise ImportNotFoundError(f'取込履歴が見つかりません: {import_id}')

    summary = summarize_import(data, record)
    tx_ids = {t.id for t in data.transactions if t.import_id == import_id}
    data.transactions = [t for t in data.transactions if t.import_id != import_id]
    data.allocations = [a for a in data.allocations if a.transaction_id not in tx_ids]
    data.imports = [i for i in data.imports if i.import_id != import_id]
    logger.info(
        'import undone: import id=%s transactions=%d allocations=%d manual edits=%d',
        import_id,
        summary.transactions,
        summary.allocations,
        summary.manual_edits,
    )
    return summary
