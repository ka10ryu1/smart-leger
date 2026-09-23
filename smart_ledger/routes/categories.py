"""カテゴリ管理画面（追加・名称変更・並び替え・削除）のルート"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from ..constants import CATEGORY_DESCRIPTIONS, FALLBACK_CATEGORY
from ..services.categories import add_category, category_usage, delete_category, edit_category, move_category
from .common import bp, load_data, save_and_redirect


@bp.route('/categories')
def categories_page() -> str:
    """カテゴリ管理画面（追加・名称変更・並び替え・削除）"""
    data = load_data()
    return render_template(
        'categories.html',
        categories=data.sorted_categories(),
        usage=category_usage(data),
        fallback=FALLBACK_CATEGORY,
        default_descriptions=CATEGORY_DESCRIPTIONS,
    )


@bp.route('/categories/add', methods=['POST'])
def add_category_route() -> WerkzeugResponse:
    """カテゴリを追加する（メッセージには正規化後の名前を使う）"""
    name = request.form.get('name', '')
    description = request.form.get('description', '')
    return save_and_redirect(
        lambda data: add_category(data, name, description),
        lambda added: f'カテゴリ「{added.category}」を追加しました。',
        url_for('ledger.categories_page'),
    )


@bp.route('/categories/edit', methods=['POST'])
def edit_category_route() -> WerkzeugResponse:
    """カテゴリの名称・説明を変更する（名称変更は明細・ルール・内訳に伝播し、件数を表示する）"""
    name = request.form.get('category', '')
    new_name = request.form.get('new_name', '')
    description = request.form.get('description', '')
    return save_and_redirect(
        lambda data: edit_category(data, name, new_name, description),
        lambda changed: (
            f'カテゴリを保存し、名称変更を明細・ルール・内訳の {changed} 件に反映しました。'
            if changed
            else 'カテゴリを保存しました。'
        ),
        url_for('ledger.categories_page'),
    )


@bp.route('/categories/move', methods=['POST'])
def move_category_route() -> WerkzeugResponse:
    """カテゴリの表示順を上下に動かす（端にあって動かない場合はエラー表示のみで保存しない）"""
    name = request.form.get('category', '')
    direction = request.form.get('direction', '')
    if direction not in ('up', 'down'):
        flash('移動方向が不正です。', 'error')
        return redirect(url_for('ledger.categories_page'))

    delta = -1 if direction == 'up' else 1
    return save_and_redirect(
        lambda data: move_category(data, name, delta),
        lambda _: '並び順を変更しました。',
        url_for('ledger.categories_page'),
    )


@bp.route('/categories/delete', methods=['POST'])
def delete_category_route() -> WerkzeugResponse:
    """未使用のカテゴリを削除する"""
    name = request.form.get('category', '')
    return save_and_redirect(
        lambda data: delete_category(data, name),
        lambda _: f'カテゴリ「{name}」を削除しました。',
        url_for('ledger.categories_page'),
    )
