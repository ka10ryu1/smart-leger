"""CSV 取込（プレビュー・確定・取り消し・破棄）のルート"""

from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from werkzeug.wrappers import Response as WerkzeugResponse

from ..models import ImportRecord, LedgerData
from ..services.csv_parser import CsvParseError
from ..services.importer import ImportUndoSummary, summarize_import, undo_import
from .common import bp, load_data, save_and_redirect, svc


def import_history(data: LedgerData) -> tuple[list[ImportRecord], dict[str, ImportUndoSummary]]:
    """取込履歴（新しい順）と、取り消し確認に使う件数のまとめを返す

    Args:
        data: 全データ

    Returns:
        (取込履歴のリスト, import_id → 件数のまとめ)
    """
    imports = sorted(data.imports, key=lambda i: i.imported_at, reverse=True)
    summaries = {i.import_id: summarize_import(data, i) for i in imports}
    return imports, summaries


@bp.route('/import')
def import_page() -> str:
    """CSV 取込画面（ファイル選択と取込履歴）"""
    imports, summaries = import_history(load_data())
    return render_template('import.html', preview=None, imports=imports, summaries=summaries)


@bp.route('/import/preview', methods=['POST'])
def import_preview() -> str | WerkzeugResponse:
    """CSV を解析してプレビューを表示する（この時点では保存しない）"""
    file = request.files.get('csv_file')
    if file is None or not file.filename:
        flash('CSV ファイルを選択してください。', 'error')
        return redirect(url_for('ledger.import_page'))

    content = file.read()
    if not content:
        flash('空のファイルです。', 'error')
        return redirect(url_for('ledger.import_page'))

    data = load_data()
    try:
        preview = svc().importer.preview(data, file.filename, content)
    except CsvParseError as exc:
        flash(f'CSV を解析できませんでした: {exc}', 'error')
        return redirect(url_for('ledger.import_page'))

    imports, summaries = import_history(data)
    return render_template('import.html', preview=preview, imports=imports, summaries=summaries)


@bp.route('/import/commit', methods=['POST'])
def import_commit() -> WerkzeugResponse:
    """プレビューの新規明細だけを分類して Excel に保存する"""
    token = request.form.get('token', '')
    preview = svc().importer.load_preview(token)
    if preview is None:
        flash('プレビュー情報が見つかりません。もう一度 CSV を選択してください。', 'error')
        return redirect(url_for('ledger.import_page'))

    try:
        result = svc().repo.update(lambda data: svc().importer.commit(data, preview))
    except ValueError as exc:  # 主に新規行なし（プレビュー後に取り込まれた、または取込済み CSV）
        flash(str(exc), 'warning')
        return redirect(url_for('ledger.import_page'))

    svc().importer.discard(token)
    msg = (
        f'{result.imported} 件を取り込みました(重複スキップ {result.skipped_duplicates} 件、'
        f'ルール一致 {result.rule_matched} 件、自動採用 {result.auto_accepted} 件、要確認 {result.needs_review} 件)。'
    )
    if result.income_count:
        msg += f' うち {result.income_count} 件は収入として記録しました。'

    if result.added_categories:
        msg += f' カテゴリ「{"」「".join(result.added_categories)}」を追加しました。'

    if result.jev_errors:
        msg += f' Jev 分類エラー {result.jev_errors} 件は「その他」として要確認に入っています。'

    if svc().repo.last_dropbox_result:
        msg += ' Dropbox へもコピーしました。'

    flash(msg, 'success')
    if result.needs_review:
        return redirect(url_for('ledger.review'))

    if result.months:
        return redirect(url_for('ledger.dashboard', month=result.months[-1]))

    return redirect(url_for('ledger.dashboard'))


@bp.route('/import/<import_id>/undo', methods=['POST'])
def import_undo(import_id: str) -> WerkzeugResponse:
    """取込を取り消す（明細・内訳・履歴を削除。同じ CSV を再取込できるようになる）

    Args:
        import_id: 取り消す取込 ID
    """

    def message(summary: ImportUndoSummary) -> str:
        extra = f' 内訳 {summary.allocations} 件も削除しました。' if summary.allocations else ''
        return f'取込「{summary.filename}」を取り消し、明細 {summary.transactions} 件を削除しました。{extra} 同じ CSV を取り込み直せます。'

    return save_and_redirect(lambda data: undo_import(data, import_id), message, url_for('ledger.import_page'))


@bp.route('/import/discard', methods=['POST'])
def import_discard() -> WerkzeugResponse:
    """プレビューを破棄する"""
    svc().importer.discard(request.form.get('token', ''))
    flash('取込をキャンセルしました。', 'info')
    return redirect(url_for('ledger.import_page'))
