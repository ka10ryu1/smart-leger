"""allocations（1 明細の複数カテゴリ内訳）の検証と保存、前回の内訳の複写"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Allocation, LedgerData, Transaction
from .normalize import merchant_key


class AllocationError(ValueError):
    """内訳の入力が不正"""


@dataclass
class AllocationInput:
    """フォームから受け取った内訳 1 行"""

    category: str
    amount: int | None  # None は金額欄が空の行（保存時に残額を入れる）
    memo: str = ''


@dataclass
class ValidatedAllocations:
    """検証済みの内訳（保存用）"""

    allocations: list[Allocation]
    remainder: Allocation | None = None  # 金額欄が空の行に残額を入れた場合、その行


@dataclass
class AllocationCopy:
    """前回の内訳を複写した入力（保存前の下書き。合計は複写先の明細金額に合わせてある）"""

    source: Transaction
    items: list[AllocationInput]
    difference: int  # 複写先の金額 - 複写元の内訳合計（最後の行に寄せた額）
    last_row_flipped: bool  # 寄せた結果、最後の行が 0 円か明細金額と逆の符号になった（画面で見直しを促す）


def flips_sign(amount: int, total: int) -> bool:
    """内訳 1 行の金額が 0 円か、明細金額と逆の符号になっているかを返す

    Args:
        amount: 内訳 1 行の金額
        total: 明細金額
    """
    return amount == 0 or (amount < 0) != (total < 0)


def validate_allocations(tx: Transaction, items: list[AllocationInput], categories: list[str]) -> ValidatedAllocations:
    """内訳を検証して Allocation に変換する（金額が空の行が無く、合計が明細金額と一致しなければ AllocationError）

    金額が空（None）の行が 1 つだけなら、明細金額 - 他の行の合計（残額）をその行に入れる。
    空の行が 2 つ以上ある場合や、残額が 0 円か明細金額と逆の符号になる場合は AllocationError

    Args:
        tx: 対象の明細
        items: 入力された内訳（空行は無視する）
        categories: 有効なカテゴリ名

    Returns:
        保存用の内訳と残額を入れた行（入力がすべて空なら内訳は空リスト）
    """
    cleaned = [i for i in items if i.category or i.amount or i.memo]
    if not cleaned:
        return ValidatedAllocations(allocations=[])

    for i in cleaned:
        if not i.category:
            raise AllocationError('内訳のカテゴリが未選択の行があります。')

        if i.category not in categories:
            raise AllocationError(f'不明なカテゴリです: {i.category}')

        if i.amount == 0:
            raise AllocationError('内訳の金額が 0 の行があります。')

    blanks = sum(1 for i in cleaned if i.amount is None)
    if blanks > 1:
        raise AllocationError(f'金額が空の行が {blanks} 行あります。残額を入れられるのは 1 行だけです。')

    total = sum(i.amount for i in cleaned if i.amount is not None)
    remainder = tx.amount - total
    if blanks and flips_sign(remainder, tx.amount):
        raise AllocationError(
            f'金額が空の行に入る残額が {remainder:,} 円になります(明細金額 {tx.amount:,} 円、他の行の合計 {total:,} 円)。'
            '0 円や明細金額と逆の符号になる残額は入れられません。'
        )

    if not blanks and total != tx.amount:
        raise AllocationError(
            f'内訳の合計 {total:,} 円が明細金額 {tx.amount:,} 円と一致しません(差額 {remainder:,} 円)。'
        )

    result = ValidatedAllocations(allocations=[])
    for i in cleaned:
        allocation = Allocation(
            transaction_id=tx.id, category=i.category, amount=remainder if i.amount is None else i.amount, memo=i.memo
        )
        result.allocations.append(allocation)
        if i.amount is None:
            result.remainder = allocation

    return result


def replace_allocations(data: LedgerData, tx_id: str, new_items: list[Allocation]) -> None:
    """明細の内訳を丸ごと置き換える（new_items が空なら削除）

    Args:
        data: 対象の LedgerData
        tx_id: 明細 ID
        new_items: 新しい内訳
    """
    data.allocations = [a for a in data.allocations if a.transaction_id != tx_id] + list(new_items)


def previous_allocation_source(data: LedgerData, tx: Transaction) -> Transaction | None:
    """同じ加盟店キーで内訳のある他の明細のうち、利用日が tx 以前で最も新しいものを返す

    加盟店キーは請求月などを除いて大文字小文字を無視して比べる（「7ガツブン KYASH」と「8ガツブン KYASH」は同じ加盟店）。
    収支（kind）と金額の符号が tx と同じ明細に限る（返金の内訳を通常の支出に写したり、その逆をしたりしない）。
    利用日が同じ明細が複数あれば、取込日時が新しいものを選ぶ（取込日時まで同じならどれが前回かは決められないので、保存順で決める）

    Args:
        data: 対象の LedgerData
        tx: 複写先の明細

    Returns:
        複写元の明細（候補が無ければ None）
    """
    key = merchant_key(tx.merchant_normalized.casefold())
    with_allocations = {a.transaction_id for a in data.allocations}
    candidates = [
        t
        for t in data.transactions
        if t.id != tx.id
        and t.id in with_allocations
        and t.usage_date <= tx.usage_date
        and t.kind == tx.kind
        and (t.amount < 0) == (tx.amount < 0)
        and merchant_key(t.merchant_normalized.casefold()) == key
    ]
    # 取込日時まで同じ候補の並び（明細 ID 順）に意味は無いが、max は先に見たものを返すので逆順に渡して後ろの行に固定する
    return max(reversed(candidates), key=lambda t: (t.usage_date, t.imported_at), default=None)


def copy_previous_allocations(data: LedgerData, tx: Transaction) -> AllocationCopy | None:
    """前回の内訳（previous_allocation_source の明細の内訳）を複写し、金額の差額を最後の行に寄せる

    保存はしない（編集画面のフォームに下書きとして出し、利用者が「内訳を保存」で確定する）

    Args:
        data: 対象の LedgerData
        tx: 複写先の明細

    Returns:
        複写した内訳（複写元が無ければ None）
    """
    source = previous_allocation_source(data, tx)
    if source is None:
        return None

    source_items = data.allocations_for(source.id)
    items = [AllocationInput(category=a.category, amount=a.amount, memo=a.memo) for a in source_items]
    # 複写元の明細金額ではなく内訳の合計との差を取る（Excel を直接編集して合計がずれていても複写先の金額に揃う）
    difference = tx.amount - sum(a.amount for a in source_items)
    last_amount = source_items[-1].amount + difference
    items[-1].amount = last_amount
    flipped = flips_sign(last_amount, tx.amount)
    return AllocationCopy(source=source, items=items, difference=difference, last_row_flipped=flipped)
