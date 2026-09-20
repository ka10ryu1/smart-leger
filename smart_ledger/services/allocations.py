"""allocations(1 明細の複数カテゴリ内訳)の検証と保存。"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Allocation, LedgerData, Transaction


class AllocationError(ValueError):
    pass


@dataclass
class AllocationInput:
    category: str
    amount: int
    memo: str = ""


def validate_allocations(tx: Transaction, items: list[AllocationInput], categories: list[str]) -> list[Allocation]:
    cleaned = [i for i in items if i.category or i.amount or i.memo]
    if not cleaned:
        return []
    for i in cleaned:
        if not i.category:
            raise AllocationError("内訳のカテゴリが未選択の行があります。")
        if i.category not in categories:
            raise AllocationError(f"不明なカテゴリです: {i.category}")
        if i.amount == 0:
            raise AllocationError("内訳の金額が 0 の行があります。")
    total = sum(i.amount for i in cleaned)
    if total != tx.amount:
        raise AllocationError(
            f"内訳の合計 {total:,} 円が明細金額 {tx.amount:,} 円と一致しません(差額 {tx.amount - total:,} 円)。"
        )
    return [Allocation(transaction_id=tx.id, category=i.category, amount=i.amount, memo=i.memo) for i in cleaned]


def replace_allocations(data: LedgerData, tx_id: str, new_items: list[Allocation]) -> None:
    data.allocations = [a for a in data.allocations if a.transaction_id != tx_id] + list(new_items)
