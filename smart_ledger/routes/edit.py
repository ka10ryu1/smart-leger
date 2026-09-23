"""明細編集画面のルート（カテゴリ変更・ルール登録・内訳の保存と削除）"""

from __future__ import annotations

from flask import abort, flash, redirect, render_template, request
from werkzeug.wrappers import Response as WerkzeugResponse

from ..constants import KIND_LABELS
from ..models import LedgerData
from ..services.allocations import (
    AllocationInput,
    copy_previous_allocations,
    replace_allocations,
    validate_allocations,
)
from ..services.merchant_rules import CategoryChange, apply_manual_category, match_rule, preview_rule_targets
from ..services.normalize import merchant_key
from .common import bp, edit_url, load_data, safe_back, save_and_redirect, svc


def parse_allocation_form() -> list[AllocationInput]:
    """内訳フォーム（alloc_category / alloc_amount / alloc_memo の並列リスト）を読む

    Returns:
        空行を除いた内訳入力（金額が数値でなければ ValueError）
    """
    categories = request.form.getlist('alloc_category')
    amounts = request.form.getlist('alloc_amount')
    memos = request.form.getlist('alloc_memo')
    items: list[AllocationInput] = []
    for i in range(max(len(categories), len(amounts))):
        cat = categories[i].strip() if i < len(categories) else ''
        amt_raw = (amounts[i] if i < len(amounts) else '').replace(',', '').strip()
        memo = memos[i].strip() if i < len(memos) else ''
        if not cat and not amt_raw and not memo:
            continue

        try:
            amt = int(amt_raw) if amt_raw else 0
        except ValueError as exc:
            raise ValueError(f'内訳の金額が数値ではありません: {amt_raw}') from exc

        items.append(AllocationInput(category=cat, amount=amt, memo=memo))

    return items


@bp.route('/transactions/<tx_id>/edit')
def edit_transaction(tx_id: str) -> str:
    """明細編集画面（copy_allocations=1 なら内訳フォームを前回の内訳の複写で埋める。保存はしない）

    Args:
        tx_id: 明細 ID
    """
    data = load_data()
    tx = data.find_transaction(tx_id)
    if tx is None:
        abort(404)

    # 提案パターンをルール化したときの反映対象（手動修正済みは除く）
    same_merchant = preview_rule_targets(data, merchant_key(tx.merchant_normalized), tx.id)
    allocations = data.allocations_for(tx_id)
    previous = copy_previous_allocations(data, tx)
    copy_requested = request.args.get('copy_allocations') == '1'
    return render_template(
        'edit.html',
        tx=tx,
        allocations=allocations,
        alloc_rows=previous.items if previous and copy_requested else allocations,
        previous_allocations=previous,
        copy_requested=copy_requested,
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
def update_allocations(tx_id: str) -> WerkzeugResponse:
    """内訳を保存する（合計が明細金額と一致しなければエラー表示）

    Args:
        tx_id: 明細 ID
    """

    def mutate(data: LedgerData) -> bool:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        items = parse_allocation_form()  # 金額が数値でなければ ValueError（保存前に止まる）
        replace_allocations(data, tx_id, validate_allocations(tx, items, data.category_names()))
        return bool(items)

    return save_and_redirect(
        mutate, lambda saved: '内訳を保存しました。' if saved else '内訳を削除しました。', edit_url(tx_id)
    )


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
