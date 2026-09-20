"""世代バックアップと Dropbox(ローカル同期フォルダ)へのコピー。"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def create_generation_backup(source: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """正本を backup_dir/household_YYYYMMDD_HHMMSS.xlsx にコピーし、古い世代を削除する。"""
    if not source.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"{source.stem}_{timestamp()}{source.suffix}"
    if dest.exists():  # 同一秒内の連続保存
        dest = backup_dir / f"{source.stem}_{timestamp()}_{datetime.now().microsecond:06d}{source.suffix}"
    shutil.copy2(source, dest)
    prune_backups(backup_dir, source.stem, source.suffix, keep)
    logger.info("世代バックアップ作成: %s", dest.name)
    return dest


def prune_backups(backup_dir: Path, stem: str, suffix: str, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(backup_dir.glob(f"{stem}_*{suffix}"), key=lambda p: p.name)
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError as exc:  # pragma: no cover
            logger.warning("古いバックアップの削除に失敗: %s (%s)", old.name, exc)


class DropboxBackup:
    """Dropbox デスクトップアプリが同期するローカルフォルダにコピーする。API は使わない。"""

    def __init__(self, dropbox_path: Path | None):
        self.dropbox_path = dropbox_path

    @property
    def enabled(self) -> bool:
        return self.dropbox_path is not None

    def copy(self, source: Path) -> dict[str, Path] | None:
        """latest/household.xlsx と backup/household_YYYYMMDD_HHMMSS.xlsx にコピーする。

        未設定なら None を返してスキップ。失敗しても例外は投げず、ログに残す
        (正本の保存はすでに成功しているため)。
        """
        if not self.enabled:
            logger.debug("Dropbox パス未設定のためバックアップをスキップ")
            return None
        assert self.dropbox_path is not None
        try:
            latest_dir = self.dropbox_path / "latest"
            backup_dir = self.dropbox_path / "backup"
            latest_dir.mkdir(parents=True, exist_ok=True)
            backup_dir.mkdir(parents=True, exist_ok=True)
            latest = latest_dir / source.name
            backup = backup_dir / f"{source.stem}_{timestamp()}{source.suffix}"
            tmp_latest = latest_dir / f"~{source.name}.tmp"
            shutil.copy2(source, tmp_latest)
            tmp_latest.replace(latest)
            shutil.copy2(source, backup)
            logger.info("Dropbox へコピー: %s, %s", latest, backup.name)
            return {"latest": latest, "backup": backup}
        except OSError as exc:
            logger.error("Dropbox へのコピーに失敗しました: %s", exc)
            return None
