"""手動明細の追加・削除のルート"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from ..constants import KIND_EXPENSE
from ..models import LedgerData
from ..services.manual_entries import ManualEntryInput, add_manual_transaction, delete_manual_transaction
from .common import bp, load_data, safe_back, svc


@bp.route('/transactions/<tx_id>/delete', methods=['POST'])
def delete_transaction(tx_id: str) -> WerkzeugResponse:
    """手動明細を内訳ごと削除する（CSV から取り込んだ明細は拒否し、取込の取り消しに誘導する）

    Args:
        tx_id: 明細 ID
    """
    back = safe_back()

    def mutate(data: LedgerData) -> tuple[str, int]:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        return tx.merchant_normalized, delete_manual_transaction(data, tx_id)

    try:
        merchant, allocations = svc().repo.update(mutate)
    except ValueError as exc:  # 取込明細の削除要求（ManualEntryError）
        flash(str(exc), 'error')
        return redirect(url_for('ledger.edit_transaction', tx_id=tx_id, back=back))

    extra = f' 内訳 {allocations} 件も削除しました。' if allocations else ''
    flash(f'明細「{merchant}」を削除しました。{extra}', 'success')
    return redirect(back)


@bp.route('/transactions/new', methods=['GET', 'POST'])
def new_transaction() -> str | tuple[str, int] | WerkzeugResponse:
    """手動明細の追加画面（現金支出など CSV に載らない明細を登録する。POST で検証して保存し、その月の明細一覧へ移る）"""
    back = safe_back()
    if request.method == 'GET':
        entry = ManualEntryInput(
            usage_date=date.today().isoformat(), merchant='', amount='', category='', kind=KIND_EXPENSE, card='現金'
        )
        return render_template('manual.html', form=asdict(entry), categories=load_data().category_names(), back=back)

    entry = ManualEntryInput(
        usage_date=request.form.get('usage_date', ''),
        merchant=request.form.get('merchant', ''),
        amount=request.form.get('amount', ''),
        category=request.form.get('category', ''),
        kind=request.form.get('kind', ''),
        card=request.form.get('card', ''),
        memo=request.form.get('memo', ''),
    )
    try:
        tx = svc().repo.update(lambda data: add_manual_transaction(data, entry))
    except ValueError as exc:  # ManualEntryError（入力不正）。入力値を残したまま再表示する
        flash(str(exc), 'error')
        categories = load_data().category_names()
        return render_template('manual.html', form=asdict(entry), categories=categories, back=back), 400

    sign = '+' if tx.is_income else ''
    flash(f'明細「{tx.merchant_normalized}」({sign}{tx.amount:,} 円)を手動で追加しました。', 'success')
    return redirect(url_for('ledger.transactions', month=tx.month))
