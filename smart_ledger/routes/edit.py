"""明細編集画面のルート（カテゴリ変更・ルール登録・内訳の保存と削除）"""

from __future__ import annotations

from dataclasses import dataclass

from flask import abort, flash, redirect, render_template, request
from werkzeug.wrappers import Response as WerkzeugResponse

from ..constants import KIND_LABELS
from ..models import LedgerData
from ..services.allocations import (
    AllocationInput,
    ValidatedAllocations,
    copy_previous_allocations,
    replace_allocations,
    validate_allocations,
)
from ..services.merchant_rules import CategoryChange, apply_manual_category, match_rule, preview_rule_targets
from ..services.normalize import merchant_key
from .common import bp, edit_url, load_data, safe_back, save_and_redirect, svc


@dataclass
class AllocationFormRow:
    """内訳フォームの送信値 1 行（金額はカンマを除いた文字列のまま。保存エラー時に入力欄へ戻す）"""

    category: str
    amount: str
    memo: str


def read_allocation_form() -> list[AllocationFormRow]:
    """内訳フォーム（alloc_category / alloc_amount / alloc_memo の並列リスト）を行ごとに読む（すべて空の行は除く）"""
    categories = request.form.getlist('alloc_category')
    amounts = request.form.getlist('alloc_amount')
    memos = request.form.getlist('alloc_memo')
    rows: list[AllocationFormRow] = []
    for i in range(max(len(categories), len(amounts))):
        cat = categories[i].strip() if i < len(categories) else ''
        amt_raw = (amounts[i] if i < len(amounts) else '').replace(',', '').strip()
        memo = memos[i].strip() if i < len(memos) else ''
        if not cat and not amt_raw and not memo:
            continue

        rows.append(AllocationFormRow(category=cat, amount=amt_raw, memo=memo))

    return rows


def parse_allocation_form(rows: list[AllocationFormRow]) -> list[AllocationInput]:
    """内訳フォームの送信値を内訳入力にする

    Args:
        rows: read_allocation_form() で読んだ行

    Returns:
        内訳入力（金額欄が空の行は amount=None。金額が数値でなければ ValueError）
    """
    items: list[AllocationInput] = []
    for row in rows:
        try:
            amt = int(row.amount) if row.amount else None
        except ValueError as exc:
            raise ValueError(f'内訳の金額が数値ではありません: {row.amount}') from exc

        items.append(AllocationInput(category=row.category, amount=amt, memo=row.memo))

    return items


@bp.route('/transactions/<tx_id>/edit')
def edit_transaction(tx_id: str, form_rows: list[AllocationFormRow] | None = None) -> str:
    """明細編集画面（copy_allocations=1 なら内訳フォームを前回の内訳の複写で埋める。保存はしない）

    Args:
        tx_id: 明細 ID
        form_rows: 内訳の入力欄に戻す送信値（保存エラー時。None なら保存済みの内訳か前回の内訳の複写を出す）
    """
    data = load_data()
    tx = data.find_transaction(tx_id)
    if tx is None:
        abort(404)

    # 提案パターンをルール化したときの反映対象（手動修正済みは除く）
    same_merchant = preview_rule_targets(data, merchant_key(tx.merchant_normalized), tx.id)
    return render_template(
        'edit.html',
        tx=tx,
        allocations=data.allocations_for(tx_id),
        form_rows=form_rows,
        prev=copy_previous_allocations(data, tx),
        copy_requested=request.args.get('copy_allocations') == '1',
        categories=data.category_names(),
        rule=match_rule(data.merchant_rules, tx.merchant_normalized),
        same_merchant_count=len(same_merchant),
        back=safe_back(),
    )


@bp.route('/transactions/<tx_id>/rule-preview')
def rule_preview(tx_id: str) -> dict[str, int]:
    """入力中のルールパターンで登録した場合に反映される他の明細の件数を返す（編集画面の件数表示用。Flask が JSON にする）

    Args:
        tx_id: 編集中の明細 ID
    """
    data = load_data()
    if data.find_transaction(tx_id) is None:
        abort(404)

    pattern = request.args.get('pattern', '')
    return {'count': len(preview_rule_targets(data, pattern, tx_id))}


@bp.route('/transactions/<tx_id>/category', methods=['POST'])
def update_category(tx_id: str) -> WerkzeugResponse:
    """カテゴリ変更（scope=once なら今回だけ、always ならルール登録してパターンに一致する明細にも反映）

    ルールのパターンはフォームの rule_pattern（省略時は請求月などを除いた加盟店キー）を使う。
    フォームに kind があればこの明細の収支も変える（ルールには載せない。要確認画面のフォームには無いので変えない）

    Args:
        tx_id: 明細 ID
    """
    category = request.form.get('category', '').strip()
    scope = request.form.get('scope', 'once')
    memo = request.form.get('memo', '').strip()
    rule_pattern = request.form.get('rule_pattern', '').strip()
    kind = request.form.get('kind', '')
    back = safe_back()

    def mutate(data: LedgerData) -> CategoryChange:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        return apply_manual_category(
            data, tx, category, kind=kind, memo=memo, remember=scope == 'always', rule_pattern=rule_pattern
        )

    try:
        change = svc().repo.update(mutate)
    except ValueError as exc:  # 不明なカテゴリなど
        flash(str(exc), 'error')
        return redirect(edit_url(tx_id))

    if change.rule_pattern:
        msg = f'カテゴリを「{category}」に変更し、ルール「{change.rule_pattern}」を登録しました。'
        if change.applied_others:
            msg += f' 一致する {change.applied_others} 件にも適用しました。'
    else:
        msg = f'カテゴリを「{category}」に変更しました(今回だけ)。'

    if change.changed_kind:
        msg += f' 収支を「{KIND_LABELS[change.changed_kind]}」に変更しました。'

    flash(msg, 'success')
    if not change.matches_self:
        flash(
            f'ルール「{change.rule_pattern}」はこの明細の加盟店名に一致しません。パターンを確認してください。',
            'warning',
        )

    return redirect(back)


@bp.route('/transactions/<tx_id>/allocations', methods=['POST'])
def update_allocations(tx_id: str) -> WerkzeugResponse | tuple[str, int]:
    """内訳を保存する（金額欄が空の 1 行には残額を入れて文言で知らせる）

    合計が明細金額と一致しないなどのエラーはリダイレクトせず、送信された行を入力欄に残した編集画面を HTTP 400 で返す

    Args:
        tx_id: 明細 ID
    """
    rows = read_allocation_form()

    def mutate(data: LedgerData) -> ValidatedAllocations:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        validated = validate_allocations(tx, items, data.category_names())
        replace_allocations(data, tx_id, validated.allocations)
        return validated

    try:
        items = parse_allocation_form(rows)  # 金額が数値でなければ ValueError（保存処理に入る前に止まる）
        validated = svc().repo.update(mutate)
    except ValueError as exc:  # AllocationError / 金額が数値でない（打ち直さずに直せるよう入力を残す）
        flash(str(exc), 'error')
        return edit_transaction(tx_id, rows), 400

    remainder = validated.remainder
    if not validated.allocations:
        flash('内訳を削除しました。', 'success')
    elif remainder is None:
        flash('内訳を保存しました。', 'success')
    else:
        flash(f'内訳を保存しました。残額 {remainder.amount:,} 円を「{remainder.category}」に入れました。', 'success')

    return redirect(edit_url(tx_id))


@bp.route('/transactions/<tx_id>/allocations/clear', methods=['POST'])
def clear_allocations(tx_id: str) -> WerkzeugResponse:
    """内訳をすべて削除する

    Args:
        tx_id: 明細 ID
    """

    def mutate(data: LedgerData) -> None:
        if data.find_transaction(tx_id) is None:
            abort(404)

        replace_allocations(data, tx_id, [])

    return save_and_redirect(mutate, lambda _: '内訳を削除しました。', edit_url(tx_id))
