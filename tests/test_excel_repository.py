from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import load_workbook

from smart_ledger.models import DEFAULT_CATEGORIES, Allocation, ImportRecord, MerchantRule, Transaction
from smart_ledger.services.backup import DropboxBackup
from smart_ledger.services.excel_repository import SHEETS, ExcelRepository


def test_load_creates_workbook_with_all_sheets_and_categories(repo: ExcelRepository):
    data = repo.load()
    assert repo.excel_path.exists()
    wb = load_workbook(repo.excel_path)
    assert set(SHEETS) <= set(wb.sheetnames)
    assert [c.category for c in sorted(data.categories, key=lambda c: c.sort_order)] == list(DEFAULT_CATEGORIES)
    # ヘッダー行
    assert [c.value for c in wb["transactions"][1]] == list(Transaction.COLUMNS)
    assert [c.value for c in wb["allocations"][1]] == list(Allocation.COLUMNS)


def test_roundtrip_write_read(repo: ExcelRepository):
    data = repo.load()
    data.transactions.append(
        Transaction(
            id="tx_1",
            usage_date=date(2026, 8, 15),
            merchant_raw="ＫＹＡＳＨ",
            merchant_normalized="KYASH",
            amount=10000,
            category="その他",
            confidence=0.55,
            classification_source="jev",
            card="テストカード",
            import_id="imp_1",
            imported_at="2026-09-20 10:00:00",
            row_key="abc",
            memo="メモ",
        )
    )
    data.merchant_rules.append(MerchantRule("KYASH", "その他", "2026-09-20 10:00:00"))
    data.imports.append(ImportRecord("imp_1", "a.csv", "hash", "2026-09-20 10:00:00", "テストカード", 1))
    data.allocations.append(Allocation("tx_1", "食費", 10000, "内訳"))
    repo.save(data)

    loaded = repo.load()
    t = loaded.transactions[0]
    assert t.id == "tx_1" and t.usage_date == date(2026, 8, 15) and t.amount == 10000
    assert t.merchant_raw == "ＫＹＡＳＨ" and t.merchant_normalized == "KYASH"
    assert t.confidence == 0.55 and t.classification_source == "jev" and t.memo == "メモ"
    assert loaded.merchant_rules[0].merchant_pattern == "KYASH"
    assert loaded.imports[0].file_hash == "hash"
    assert loaded.allocations[0].amount == 10000


def test_save_creates_generation_backup_and_no_temp_left(repo: ExcelRepository):
    data = repo.load()  # 1st save (no backup yet)
    repo.save(data)  # 2nd save -> backup of 1st
    repo.save(data)  # 3rd
    backups = list(repo.backup_dir.glob("household_*.xlsx"))
    assert len(backups) == 2
    assert not list(repo.excel_path.parent.glob("~*.tmp.xlsx"))


def test_backup_pruning(tmp_path: Path):
    repo = ExcelRepository(tmp_path / "h.xlsx", backup_dir=tmp_path / "b", backup_generations=2)
    data = repo.load()
    for _ in range(5):
        repo.save(data)
    assert len(list((tmp_path / "b").glob("h_*.xlsx"))) == 2


def test_dropbox_disabled_is_noop(repo: ExcelRepository):
    repo.save(repo.load())
    assert repo.last_dropbox_result is None
    assert DropboxBackup(None).enabled is False
    assert DropboxBackup(None).copy(repo.excel_path) is None


def test_dropbox_enabled_copies_latest_and_backup(tmp_path: Path):
    dropbox = tmp_path / "Dropbox" / "SmartLedger"
    repo = ExcelRepository(tmp_path / "household.xlsx", backup_dir=tmp_path / "backup", dropbox=DropboxBackup(dropbox))
    repo.save(repo.load())
    assert (dropbox / "latest" / "household.xlsx").exists()
    backups = list((dropbox / "backup").glob("household_*.xlsx"))
    assert len(backups) == 1
    assert repo.last_dropbox_result is not None


def test_load_tolerates_missing_columns(tmp_path: Path):
    """将来列を追加しても古い household.xlsx を読めること。"""
    from openpyxl import Workbook

    path = tmp_path / "old.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("transactions")
    ws.append(["id", "usage_date", "merchant_raw", "merchant_normalized", "amount", "category"])
    ws.append(["tx_old", "2026-07-01", "A", "A", 100, "食費"])
    for name in ("merchant_rules", "categories", "imports", "allocations"):
        wb.create_sheet(name).append(list(SHEETS[name]))
    wb.save(path)
    repo = ExcelRepository(path, backup_dir=tmp_path / "b")
    data = repo.load()
    assert data.transactions[0].id == "tx_old"
    assert data.transactions[0].row_key == "" and data.transactions[0].memo == ""
