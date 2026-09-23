"""手動明細（現金支出や CSV の無いカードの支出）の追加と削除

- 手動明細は import_id を manual、row_key を空、classification_source を manual にして transactions に追加する
- imports シートには載せないので取込の取り消し（importer.undo_import）の対象にならない。消すときは delete_manual_transaction を使う
- 取込明細（CSV 由来）はこのモジュールでは削除しない（取込の取り消しで扱う）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from ..constants import KIND_INCOME, KIND_LABELS, MANUAL_IMPORT_ID, SOURCE_MANUAL
from ..models import LedgerData, Transaction, new_id, now_iso
from .allocations import replace_allocations
from .normalize import normalize_merchant

logger = logging.getLogger(__name__)


class ManualEntryError(ValueError):
    """手動明細の入力・操作が不正"""


@dataclass
class ManualEntryInput:
    """手動明細の追加フォームの入力（未検証の文字列のまま受け取る）"""

    usage_date: str
    merchant: str
    amount: str
    category: str
    kind: str
    card: str = ''
    memo: str = ''


def parse_manual_amount(raw: str) -> int:
    """金額の入力を整数にする（カンマ区切り可。0 や数値でない入力は ManualEntryError。負の値は返金として受け付ける）

    Args:
        raw: 金額の入力文字列
    """
    text = raw.replace(',', '').strip()
    try:
        amount = int(text)
    except ValueError as exc:
        raise ManualEntryError(f'金額は整数で入力してください: {raw}') from exc

    if amount == 0:
        raise ManualEntryError('金額に 0 は登録できません。')

    return amount


def add_manual_transaction(data: LedgerData, entry: ManualEntryInput) -> Transaction:
    """入力を検証して手動明細を追加する（保存は呼び出し側が行う）

    Args:
        data: 追加先の全データ
        entry: 追加フォームの入力

    Returns:
        追加した明細
    """
    try:
        usage_date = date.fromisoformat(entry.usage_date.strip())
    except ValueError as exc:
        raise ManualEntryError('利用日を YYYY-MM-DD の形式で入力してください。') from exc

    merchant = normalize_merchant(entry.merchant)
    if not merchant:
        raise ManualEntryError('加盟店・内容を入力してください。')

    amount = parse_manual_amount(entry.amount)
    if entry.category not in data.category_names():
        raise ManualEntryError(f'不明なカテゴリです: {entry.category}')

    if entry.kind not in KIND_LABELS:
        raise ManualEntryError(f'不明な収支です: {entry.kind}')

    if entry.kind == KIND_INCOME and amount < 0:  # 収入も amount は正の数で持つ（向きは kind で表す）
        raise ManualEntryError('収入は正の金額で入力してください。')

    tx = Transaction(
        id=new_id('tx'),
        usage_date=usage_date,
        merchant_raw=entry.merchant.strip(),
        merchant_normalized=merchant,
        amount=amount,
        category=entry.category,
        classification_source=SOURCE_MANUAL,
        card=normalize_merchant(entry.card),
        import_id=MANUAL_IMPORT_ID,
        imported_at=now_iso(),
        memo=entry.memo.strip(),
        kind=entry.kind,
    )
    data.transactions.append(tx)
    data.transactions.sort(key=lambda t: (t.usage_date, t.imported_at, t.id))  # 取込確定と同じ並び
    logger.info('manual transaction added: id=%s month=%s kind=%s', tx.id, tx.month, tx.kind)
    return tx


def delete_manual_transaction(data: LedgerData, tx: Transaction) -> int:
    """手動明細とその内訳を削除する（保存は呼び出し側が行う）

    Args:
        data: 対象の全データ
        tx: 削除する明細（data に含まれるもの）

    Returns:
        あわせて削除した内訳の件数

    Raises:
        ManualEntryError: CSV から取り込んだ明細
    """
    if not tx.is_manual_entry:
        raise ManualEntryError(
            'CSV から取り込んだ明細は削除できません。取込画面の取込履歴から取込ごと取り消してください。'
        )

    allocations = len(data.allocations_for(tx.id))
    data.transactions = [t for t in data.transactions if t.id != tx.id]
    replace_allocations(data, tx.id, [])
    logger.info('manual transaction deleted: id=%s allocations=%d', tx.id, allocations)
    return allocations
