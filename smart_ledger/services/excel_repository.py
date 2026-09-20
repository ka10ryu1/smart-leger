"""household.xlsx を DB として読み書きするリポジトリ。

保存手順:
    1. 同じフォルダの一時ファイルへ書き出す
    2. 一時ファイルを openpyxl で再オープンして壊れていないことを確認
    3. 既存の正本を世代バックアップ
    4. os.replace で正本を置換(同一ボリューム内なので実質 atomic)
    5. Dropbox が設定されていれば latest / backup へコピー

Excel が他アプリで開かれてロックされている場合は ExcelLockedError を投げる。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter

from ..models import (
    DEFAULT_CATEGORIES,
    Allocation,
    Category,
    ImportRecord,
    LedgerData,
    MerchantRule,
    Transaction,
)
from .backup import DropboxBackup, create_generation_backup

logger = logging.getLogger(__name__)

SHEETS: dict[str, tuple[str, ...]] = {
    "transactions": Transaction.COLUMNS,
    "merchant_rules": MerchantRule.COLUMNS,
    "categories": Category.COLUMNS,
    "imports": ImportRecord.COLUMNS,
    "allocations": Allocation.COLUMNS,
}

COLUMN_WIDTHS: dict[str, int] = {
    "id": 18,
    "usage_date": 12,
    "merchant_raw": 36,
    "merchant_normalized": 36,
    "merchant_pattern": 36,
    "amount": 12,
    "category": 16,
    "confidence": 11,
    "classification_source": 14,
    "card": 22,
    "import_id": 18,
    "imported_at": 20,
    "created_at": 20,
    "row_key": 20,
    "memo": 30,
    "filename": 32,
    "file_hash": 20,
    "row_count": 10,
    "transaction_id": 18,
    "sort_order": 10,
}


class ExcelLockedError(Exception):
    """Excel が他のアプリで開かれていて書き込めない。"""


class ExcelSaveError(Exception):
    """一時ファイルの保存・検証に失敗した。"""


def default_ledger() -> LedgerData:
    data = LedgerData()
    data.categories = [Category(category=c, sort_order=i + 1) for i, c in enumerate(DEFAULT_CATEGORIES)]
    return data


def _rows_as_dicts(ws, columns: tuple[str, ...]) -> Iterable[dict[str, Any]]:
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return
    header = [str(h).strip() if h is not None else "" for h in header]
    for values in rows:
        if values is None or all(v is None or str(v).strip() == "" for v in values):
            continue
        record = {h: v for h, v in zip(header, values) if h}
        # 欠けている列は None で補う(将来列追加しても旧ファイルを読める)
        for col in columns:
            record.setdefault(col, None)
        yield record


class ExcelRepository:
    def __init__(
        self,
        excel_path: Path,
        backup_dir: Path | None = None,
        backup_generations: int = 20,
        dropbox: DropboxBackup | None = None,
    ):
        self.excel_path = Path(excel_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.excel_path.parent / "backup"
        self.backup_generations = backup_generations
        self.dropbox = dropbox or DropboxBackup(None)
        self.last_dropbox_result: dict[str, Path] | None = None

    # ------------------------------------------------------------------ read
    def exists(self) -> bool:
        return self.excel_path.exists()

    def load(self) -> LedgerData:
        """全シートを読み込む。正本が無ければ初期カテゴリ入りの新規ファイルを作成する。"""
        if not self.excel_path.exists():
            logger.info("household.xlsx が存在しないため初期データを作成します: %s", self.excel_path)
            data = default_ledger()
            self.save(data)
            return data
        try:
            wb = load_workbook(self.excel_path, read_only=True, data_only=True)
        except PermissionError as exc:
            raise ExcelLockedError(self._locked_message()) from exc
        try:
            data = LedgerData()
            if "transactions" in wb.sheetnames:
                data.transactions = [
                    Transaction.from_row(r) for r in _rows_as_dicts(wb["transactions"], Transaction.COLUMNS)
                ]
            if "merchant_rules" in wb.sheetnames:
                data.merchant_rules = [
                    MerchantRule.from_row(r) for r in _rows_as_dicts(wb["merchant_rules"], MerchantRule.COLUMNS)
                ]
            if "categories" in wb.sheetnames:
                data.categories = [Category.from_row(r) for r in _rows_as_dicts(wb["categories"], Category.COLUMNS)]
            if "imports" in wb.sheetnames:
                data.imports = [
                    ImportRecord.from_row(r) for r in _rows_as_dicts(wb["imports"], ImportRecord.COLUMNS)
                ]
            if "allocations" in wb.sheetnames:
                data.allocations = [
                    Allocation.from_row(r) for r in _rows_as_dicts(wb["allocations"], Allocation.COLUMNS)
                ]
        finally:
            wb.close()
        if not data.categories:
            data.categories = default_ledger().categories
        return data

    # ----------------------------------------------------------------- write
    def build_workbook(self, data: LedgerData) -> Workbook:
        wb = Workbook()
        wb.remove(wb.active)
        sheet_rows: dict[str, list[list[Any]]] = {
            "transactions": [t.to_row() for t in data.transactions],
            "merchant_rules": [r.to_row() for r in data.merchant_rules],
            "categories": [c.to_row() for c in sorted(data.categories, key=lambda c: c.sort_order)],
            "imports": [i.to_row() for i in data.imports],
            "allocations": [a.to_row() for a in data.allocations],
        }
        for name, columns in SHEETS.items():
            ws = wb.create_sheet(name)
            ws.append(list(columns))
            for row in sheet_rows[name]:
                ws.append(row)
            for idx, col in enumerate(columns, start=1):
                ws.column_dimensions[get_column_letter(idx)].width = COLUMN_WIDTHS.get(col, 14)
            ws.freeze_panes = "A2"
            if name == "transactions":
                for cell in ws["E"][1:]:
                    cell.number_format = "#,##0"
            if name == "allocations":
                for cell in ws["C"][1:]:
                    cell.number_format = "#,##0"
        return wb

    def save(self, data: LedgerData) -> Path:
        self.excel_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.excel_path.with_name(f"~{self.excel_path.stem}.{os.getpid()}.tmp.xlsx")
        try:
            wb = self.build_workbook(data)
            wb.save(tmp_path)
            self._verify(tmp_path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise ExcelSaveError(f"Excel の一時保存に失敗しました: {exc}") from exc

        if self.excel_path.exists():
            self._ensure_writable(self.excel_path, tmp_path)
            create_generation_backup(self.excel_path, self.backup_dir, self.backup_generations)
        try:
            os.replace(tmp_path, self.excel_path)
        except PermissionError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ExcelLockedError(self._locked_message()) from exc
        logger.info(
            "Excel 保存完了: %s (transactions=%d, rules=%d, allocations=%d)",
            self.excel_path.name,
            len(data.transactions),
            len(data.merchant_rules),
            len(data.allocations),
        )
        self.last_dropbox_result = self.dropbox.copy(self.excel_path)
        return self.excel_path

    def update(self, mutator: Callable[[LedgerData], Any]) -> LedgerData:
        """読み込み → 変更 → 保存 をまとめて行う。"""
        data = self.load()
        mutator(data)
        self.save(data)
        return data

    # --------------------------------------------------------------- helpers
    def _verify(self, path: Path) -> None:
        wb = load_workbook(path, read_only=True)
        try:
            missing = [s for s in SHEETS if s not in wb.sheetnames]
            if missing:
                raise ExcelSaveError(f"保存した Excel にシートが不足しています: {missing}")
        finally:
            wb.close()

    def _ensure_writable(self, target: Path, tmp_path: Path) -> None:
        """Windows で Excel が開いていると排他ロックされるため、追記オープンで確認する。"""
        try:
            with open(target, "r+b"):
                pass
        except PermissionError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ExcelLockedError(self._locked_message()) from exc

    def _locked_message(self) -> str:
        return (
            f"{self.excel_path.name} が他のアプリ(Excel など)で開かれているため書き込めません。"
            "ファイルを閉じてから再度お試しください。"
        )
