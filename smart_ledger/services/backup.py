"""世代バックアップと Dropbox（ローカル同期フォルダ）へのコピー"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def timestamp(now: datetime | None = None) -> str:
    """バックアップファイル名に使う 'YYYYMMDD_HHMMSS' を返す

    Args:
        now: 基準時刻（None なら現在時刻）
    """
    return (now or datetime.now()).strftime('%Y%m%d_%H%M%S')


def create_generation_backup(source: Path, backup_dir: Path, keep: int = 20) -> Path | None:
    """正本を backup_dir/<stem>_YYYYMMDD_HHMMSS<suffix> にコピーし、古い世代を削除する

    Args:
        source: 正本ファイル
        backup_dir: バックアップ先ディレクトリ
        keep: 残す世代数（0 以下なら削除しない）

    Returns:
        作成したバックアップのパス（正本が存在しなければ None）
    """
    if not source.exists():
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f'{source.stem}_{timestamp()}{source.suffix}'
    if dest.exists():  # 同一秒内の連続保存
        dest = backup_dir / f'{source.stem}_{timestamp()}_{datetime.now().microsecond:06d}{source.suffix}'

    shutil.copy2(source, dest)
    prune_backups(backup_dir, source.stem, source.suffix, keep)
    logger.info('generation backup created: file=%s', dest.name)
    return dest


def prune_backups(backup_dir: Path, stem: str, suffix: str, keep: int) -> None:
    """古い世代バックアップを削除して keep 件だけ残す

    Args:
        backup_dir: バックアップディレクトリ
        stem: 正本ファイルの stem（例: household）
        suffix: 拡張子（例: .xlsx）
        keep: 残す世代数（0 以下なら何もしない）
    """
    if keep <= 0:
        return

    files = sorted(backup_dir.glob(f'{stem}_*{suffix}'), key=lambda p: p.name)
    for old in files[:-keep]:
        try:
            old.unlink()
        except OSError as exc:
            logger.warning('old backup delete failed: file=%s error=%s', old.name, exc)


class DropboxBackup:
    """Dropbox デスクトップアプリが同期するローカルフォルダにコピーする（API は使わない）"""

    def __init__(self, dropbox_path: Path | None, keep: int = 20):
        """
        Args:
            dropbox_path: DROPBOX_SMART_LEDGER_PATH のパス（None ならバックアップをスキップ）
            keep: Dropbox 側の backup/ に残す世代数（ローカルの世代数と同じ値を渡す）
        """
        self.dropbox_path = dropbox_path
        self.keep = keep

    def copy(self, source: Path) -> dict[str, Path] | None:
        """latest/<name> と backup/<stem>_YYYYMMDD_HHMMSS<suffix> にコピーする

        正本の保存はすでに成功しているため、失敗しても例外は投げずログに残す

        Args:
            source: コピー元の正本ファイル

        Returns:
            {'latest': パス, 'backup': パス}（未設定または失敗時は None）
        """
        if self.dropbox_path is None:
            logger.debug('dropbox backup skipped: reason=path not set')
            return None

        try:
            latest_dir = self.dropbox_path / 'latest'
            backup_dir = self.dropbox_path / 'backup'
            latest_dir.mkdir(parents=True, exist_ok=True)
            backup_dir.mkdir(parents=True, exist_ok=True)
            latest = latest_dir / source.name
            backup = backup_dir / f'{source.stem}_{timestamp()}{source.suffix}'
            tmp_latest = latest_dir / f'~{source.name}.tmp'
            shutil.copy2(source, tmp_latest)
            tmp_latest.replace(latest)
            shutil.copy2(source, backup)
            prune_backups(backup_dir, source.stem, source.suffix, self.keep)
            logger.info('dropbox copy done: latest=%s backup=%s', latest, backup.name)
            return {'latest': latest, 'backup': backup}
        except OSError as exc:
            logger.error('dropbox copy failed: error=%s', exc)
            return None
