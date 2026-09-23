"""加盟店ルール画面（一覧・追加・削除）のルート"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from ..services.merchant_rules import delete_rule, upsert_rule
from .common import bp, load_data, save_and_redirect


@bp.route('/rules')
def rules() -> str:
    """加盟店ルール一覧"""
    data = load_data()
    return render_template(
        'rules.html',
        rules=sorted(data.merchant_rules, key=lambda r: r.created_at, reverse=True),
        categories=data.category_names(),
    )


@bp.route('/rules/add', methods=['POST'])
def add_rule() -> WerkzeugResponse:
    """ルールを手動で追加する"""
    pattern = request.form.get('merchant_pattern', '').strip()
    category = request.form.get('category', '').strip()
    if not pattern or not category:
        flash('加盟店パターンとカテゴリを入力してください。', 'error')
        return redirect(url_for('ledger.rules'))

    return save_and_redirect(
        lambda data: upsert_rule(data, pattern, category),
        lambda _: f'ルールを登録しました: {pattern} → {category}',
        url_for('ledger.rules'),
    )


@bp.route('/rules/delete', methods=['POST'])
def remove_rule() -> WerkzeugResponse:
    """ルールを削除する"""
    pattern = request.form.get('merchant_pattern', '')
    return save_and_redirect(
        lambda data: delete_rule(data, pattern), lambda _: 'ルールを削除しました。', url_for('ledger.rules')
    )
