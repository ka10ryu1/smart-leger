"""allocations（1 明細の複数カテゴリ内訳）の検証と保存"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Allocation, LedgerData, Transaction


class AllocationError(ValueError):
    """内訳の入力が不正"""


@dataclass
class AllocationInput:
    """フォームから受け取った内訳 1 行"""

    category: str
    amount: int
    memo: str = ''


def validate_allocations(tx: Transaction, items: list[AllocationInput], categories: list[str]) -> list[Allocation]:
    """内訳を検証して Allocation に変換する（合計が明細金額と一致しなければ AllocationError）

    Args:
        tx: 対象の明細
        items: 入力された内訳（空行は無視する）
        categories: 有効なカテゴリ名

    Returns:
        保存用の Allocation リスト（入力がすべて空なら空リスト）
    """
    cleaned = [i for i in items if i.category or i.amount or i.memo]
    if not cleaned:
        return []

    for i in cleaned:
        if not i.category:
            raise AllocationError('内訳のカテゴリが未選択の行があります。')

        if i.category not in categories:
            raise AllocationError(f'不明なカテゴリです: {i.category}')

        if i.amount == 0:
            raise AllocationError('内訳の金額が 0 の行があります。')

    total = sum(i.amount for i in cleaned)
    if total != tx.amount:
        raise AllocationError(
            f'内訳の合計 {total:,} 円が明細金額 {tx.amount:,} 円と一致しません(差額 {tx.amount - total:,} 円)。'
        )

    return [Allocation(transaction_id=tx.id, category=i.category, amount=i.amount, memo=i.memo) for i in cleaned]


def replace_allocations(data: LedgerData, tx_id: str, new_items: list[Allocation]) -> None:
    """明細の内訳を丸ごと置き換える（new_items が空なら削除）

    Args:
        data: 対象の LedgerData
        tx_id: 明細 ID
        new_items: 新しい内訳
    """
    data.allocations = [a for a in data.allocations if a.transaction_id != tx_id] + list(new_items)
