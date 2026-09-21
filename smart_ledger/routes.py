"""Flask ルーティング（画面: ダッシュボード / 年間表 / 明細一覧 / CSV 取込 / 要確認 / 明細編集 / ルール / カテゴリ）"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.wrappers import Response as WerkzeugResponse

from .config import Config
from .constants import (
    CATEGORY_DESCRIPTIONS,
    FALLBACK_CATEGORY,
    MONTH_PATTERN,
    SOURCE_LABELS,
    SOURCE_MANUAL,
    SOURCE_RULE,
    UNCLASSIFIED_LABEL,
    YEAR_PATTERN,
)
from .models import ImportRecord, LedgerData, Transaction, now_iso
from .services.aggregation import (
    annual_table,
    available_months,
    available_years,
    month_label,
    monthly_summary,
    monthly_trend,
    shift_month,
    transactions_in_month,
)
from .services.allocations import AllocationInput, replace_allocations, validate_allocations
from .services.backup import DropboxBackup
from .services.categories import (
    add_category,
    category_usage,
    delete_category,
    edit_category,
    move_category,
)
from .services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from .services.csv_parser import CsvParseError
from .services.excel_repository import ExcelLockedError, ExcelRepository, ExcelSaveError
from .services.export import annual_csv, annual_xlsx
from .services.importer import Importer, ImportUndoSummary, summarize_import, undo_import
from .services.jev_client import JevClient
from .services.merchant_rules import delete_rule, match_rule, preview_rule_targets, rule_targets, upsert_rule
from .services.normalize import merchant_key

logger = logging.getLogger(__name__)

bp = Blueprint('ledger', __name__)
bp.add_app_template_filter(month_label, 'month_label')
# 加盟店名 → ルール用の既定パターン（請求月などを除いた加盟店キー）
bp.add_app_template_filter(merchant_key, 'rule_pattern')


@dataclass
class Services:
    """ルートから使うサービス群"""

    config: Config
    repo: ExcelRepository
    importer: Importer


def build_services(config: Config) -> Services:
    """設定からリポジトリ・分類パイプライン・Importer を組み立てる

    Args:
        config: アプリ設定
    """
    config.ensure_dirs()
    repo = ExcelRepository(
        config.excel_path,
        backup_dir=config.backup_dir,
        backup_generations=config.backup_generations,
        dropbox=DropboxBackup(config.dropbox_path, config.backup_generations),
    )
    jev = JevClient(config.typesafe_api_key, model=config.typesafe_model, base_url=config.typesafe_base_url)
    if jev.configured:
        fallback = JevClassifier(jev)
    else:
        fallback = NullClassifier('TYPESAFE_API_KEY が未設定のため自動分類できませんでした')

    pipeline = ClassificationPipeline(fallback, threshold=config.confidence_threshold)
    importer = Importer(config.staging_dir, pipeline)
    return Services(config=config, repo=repo, importer=importer)


def svc() -> Services:
    """現在のアプリに登録された Services を返す"""
    return current_app.extensions['smart_ledger']


# ------------------------------------------------------------------ filters
@bp.app_template_filter('yen')
def yen(value: object) -> str:
    """金額を 3 桁区切りにする（変換できなければ '-'）

    Args:
        value: 金額
    """
    try:
        return f'{int(str(value)):,}'
    except (TypeError, ValueError):
        return '-'


@bp.app_template_filter('pct')
def pct(value: float | None) -> str:
    """割合を '12.3%' 形式にする

    Args:
        value: 0〜1 の割合（None なら '-'）
    """
    if value is None:
        return '-'

    return f'{value * 100:.1f}%'


@bp.before_app_request
def reject_cross_site_post() -> None:
    """Origin / Referer のホストが一致しない POST を 403 にする（別サイトのページからの CSRF 対策）"""
    source = request.headers.get('Origin') or request.referrer
    if request.method == 'POST' and source and urlsplit(source).netloc != request.host:
        abort(403)


@bp.app_template_filter('conf')
def conf(value: float | None) -> str:
    """confidence を小数 2 桁にする

    Args:
        value: confidence（None なら '-'）
    """
    if value is None:
        return '-'

    return f'{float(value):.2f}'


def safe_back() -> str:
    """リクエストの back パラメータをサイト内の相対パスに限定して返す（外部 URL や javascript: は明細一覧に置き換える）"""
    value = request.values.get('back')
    if value and value.startswith('/') and not value.startswith('//') and '\\' not in value:  # ブラウザは \ も / と扱う
        return value

    return url_for('ledger.transactions')


@bp.app_template_filter('source_label')
def source_label(value: str) -> str:
    """classification_source を表示用ラベルにする

    Args:
        value: rule / jev / manual / error
    """
    return SOURCE_LABELS.get(value, value or UNCLASSIFIED_LABEL)


@bp.app_context_processor
def inject_globals() -> dict[str, object]:
    """全テンプレートで使う設定値を注入する"""
    config = svc().config
    return {
        'threshold': config.confidence_threshold,
        'dropbox_enabled': config.dropbox_path is not None,
        'jev_enabled': bool(config.typesafe_api_key),
        'excel_path': str(config.excel_path),
    }


# ------------------------------------------------------------------- errors
@bp.app_errorhandler(ExcelLockedError)
def handle_locked(exc: ExcelLockedError) -> tuple[str, int]:
    """Excel ロック時のエラー画面

    Args:
        exc: 発生した例外
    """
    logger.error('excel locked: error=%s', exc)
    return render_template('error.html', title='Excel を書き込めません', message=str(exc)), 423


@bp.app_errorhandler(ExcelSaveError)
def handle_save_error(exc: ExcelSaveError) -> tuple[str, int]:
    """Excel 保存失敗時のエラー画面

    Args:
        exc: 発生した例外
    """
    logger.error('excel save failed: error=%s', exc)
    return render_template('error.html', title='Excel の保存に失敗しました', message=str(exc)), 500


# ------------------------------------------------------------------ helpers
def current_month(data: LedgerData) -> str:
    """クエリの month（'YYYY-MM'）を返す（無効なら最新の明細がある月、それも無ければ今月）

    Args:
        data: 全データ
    """
    month = request.args.get('month', '').strip()
    if MONTH_PATTERN.match(month):
        return month

    months = available_months(data.transactions)
    return months[0] if months else date.today().strftime('%Y-%m')


def current_year(data: LedgerData) -> int:
    """クエリの year（4 桁の数字。0000 は無効）を返す（無効なら最新の明細がある年、それも無ければ今年）

    Args:
        data: 全データ
    """
    text = request.args.get('year', '').strip()
    year = int(text) if YEAR_PATTERN.match(text) else 0  # 0000 は 0 になり無効扱い（前年リンクが負の年になるため）
    if year:
        return year

    years = available_years(data.transactions)
    return years[0] if years else date.today().year


def load_data() -> LedgerData:
    """Excel から全データを読み込む"""
    return svc().repo.load()


def needs_review(data: LedgerData) -> list[Transaction]:
    """要確認の明細を返す

    Args:
        data: 全データ
    """
    threshold = svc().config.confidence_threshold
    return [t for t in data.transactions if t.needs_review(threshold)]


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


# ------------------------------------------------------------------- routes
@bp.route('/')
def dashboard() -> str:
    """ダッシュボード（月次集計）"""
    data = load_data()
    month = current_month(data)
    month_txs = sorted(
        transactions_in_month(data.transactions, month), key=lambda t: (t.usage_date, t.id), reverse=True
    )
    return render_template(
        'dashboard.html',
        month=month,
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1),
        summary=monthly_summary(data, month),
        recent=month_txs[:10],
        trend=monthly_trend(data, months=6, end_month=month),
        review_count=len(needs_review(data)),
        months=available_months(data.transactions),
        alloc_ids={a.transaction_id for a in data.allocations},
        has_data=bool(data.transactions),
    )


@bp.route('/annual')
def annual() -> str:
    """年間表（対象年の 12 か月 × カテゴリのマトリクス）"""
    data = load_data()
    return render_template(
        'annual.html',
        table=annual_table(data, current_year(data)),
        years=available_years(data.transactions),
        has_data=bool(data.transactions),
    )


@bp.route('/annual/export.<any(csv, xlsx):fmt>')
def annual_export(fmt: str) -> Response:
    """年間表を CSV / Excel でダウンロードする（月間総支出・カテゴリ別の両方を 1 枚の表に含む）

    Args:
        fmt: 'csv' または 'xlsx'（それ以外は routing が 404 にする）
    """
    data = load_data()
    year = current_year(data)
    table = annual_table(data, year)
    if fmt == 'csv':
        body, mimetype = annual_csv(table), 'text/csv'  # werkzeug が text/* に charset=utf-8 を付ける
    else:
        body, mimetype = annual_xlsx(table), 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

    logger.info('annual table exported: year=%d format=%s bytes=%d', year, fmt, len(body))
    return Response(
        body,
        mimetype=mimetype,
        headers={'Content-Disposition': f'attachment; filename="smart_ledger_{year:04d}.{fmt}"'},
    )


@bp.route('/transactions')
def transactions() -> str:
    """明細一覧（月・カテゴリ・分類元・加盟店名で絞り込み）"""
    data = load_data()
    month = request.args.get('month', '').strip()
    if month and not MONTH_PATTERN.match(month):
        month = ''

    category = request.args.get('category', '').strip()
    query = request.args.get('q', '').strip().casefold()
    source = request.args.get('source', '').strip()
    txs = list(data.transactions)
    if month:
        txs = transactions_in_month(txs, month)

    if category:
        txs = [t for t in txs if (t.category or UNCLASSIFIED_LABEL) == category]

    if source:
        txs = [t for t in txs if t.classification_source == source]

    if query:
        txs = [t for t in txs if query in t.merchant_normalized.casefold() or query in t.merchant_raw.casefold()]

    txs.sort(key=lambda t: (t.usage_date, t.id), reverse=True)
    names = data.category_names()
    # 空カテゴリの明細も絞り込めるようにする（旧データに同名カテゴリが残っていれば重複させない）
    if UNCLASSIFIED_LABEL not in names:
        names.append(UNCLASSIFIED_LABEL)

    return render_template(
        'transactions.html',
        transactions=txs,
        total=sum(t.amount for t in txs),
        months=available_months(data.transactions),
        categories=names,
        filters={'month': month, 'category': category, 'q': request.args.get('q', ''), 'source': source},
        alloc_ids={a.transaction_id for a in data.allocations},
    )


@bp.route('/review')
def review() -> str:
    """要確認一覧"""
    data = load_data()
    txs = sorted(needs_review(data), key=lambda t: (t.usage_date, t.id), reverse=True)
    return render_template('review.html', transactions=txs, categories=data.category_names())


@bp.route('/transactions/<tx_id>/edit')
def edit_transaction(tx_id: str) -> str:
    """明細編集画面

    Args:
        tx_id: 明細 ID
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


def save_and_redirect[T](
    mutator: Callable[[LedgerData], T], message: Callable[[T], str], target: str
) -> WerkzeugResponse:
    """LedgerData の変更を保存し、成功文言（または ValueError の内容）を flash して target へ戻る

    Args:
        mutator: LedgerData を書き換えて結果を返す関数（入力不正は ValueError 系で通知する）
        message: mutator の戻り値から成功時の flash 文言を作る関数
        target: リダイレクト先 URL
    """
    try:
        flash(message(svc().repo.update(mutator)), 'success')
    except ValueError as exc:  # CategoryError / AllocationError / 不明なカテゴリなど
        flash(str(exc), 'error')

    return redirect(target)


@bp.route('/transactions/<tx_id>/category', methods=['POST'])
def update_category(tx_id: str) -> WerkzeugResponse:
    """カテゴリ変更（scope=once なら今回だけ、always ならルール登録してパターンに一致する明細にも反映）

    ルールのパターンはフォームの rule_pattern（省略時は請求月などを除いた加盟店キー）を使う

    Args:
        tx_id: 明細 ID
    """
    category = request.form.get('category', '').strip()
    scope = request.form.get('scope', 'once')
    memo = request.form.get('memo', '').strip()
    rule_pattern = request.form.get('rule_pattern', '').strip()
    back = safe_back()

    def mutate(data: LedgerData) -> tuple[int, str, bool]:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        if category not in data.category_names():
            raise ValueError(f'不明なカテゴリです: {category}')

        tx.category = category
        tx.confidence = None
        tx.classification_source = SOURCE_MANUAL
        tx.memo = memo
        if scope != 'always':
            return 0, '', True

        rule = upsert_rule(data, rule_pattern or merchant_key(tx.merchant_normalized), category)
        targets = [
            o for o in rule_targets(data.transactions, data.merchant_rules, rule, tx.id) if o.category != category
        ]
        for other in targets:
            other.category = category
            other.confidence = None
            other.classification_source = SOURCE_RULE

        return len(targets), rule.merchant_pattern, match_rule([rule], tx.merchant_normalized) is not None

    try:
        applied_others, pattern, matches_self = svc().repo.update(mutate)
    except ValueError as exc:  # 不明なカテゴリなど
        flash(str(exc), 'error')
        return redirect(url_for('ledger.edit_transaction', tx_id=tx_id, back=back))

    if scope == 'always':
        msg = f'カテゴリを「{category}」に変更し、ルール「{pattern}」を登録しました。'
        if applied_others:
            msg += f' 一致する {applied_others} 件にも適用しました。'
    else:
        msg = f'カテゴリを「{category}」に変更しました(今回だけ)。'

    flash(msg, 'success')
    if not matches_self:
        flash(f'ルール「{pattern}」はこの明細の加盟店名に一致しません。パターンを確認してください。', 'warning')

    return redirect(back)


@bp.route('/transactions/<tx_id>/allocations', methods=['POST'])
def update_allocations(tx_id: str) -> WerkzeugResponse:
    """内訳を保存する（合計が明細金額と一致しなければエラー表示）

    Args:
        tx_id: 明細 ID
    """
    edit_url = url_for('ledger.edit_transaction', tx_id=tx_id, back=safe_back())

    def mutate(data: LedgerData) -> bool:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        items = parse_allocation_form()  # 金額が数値でなければ ValueError（保存前に止まる）
        replace_allocations(data, tx_id, validate_allocations(tx, items, data.category_names()))
        return bool(items)

    return save_and_redirect(
        mutate, lambda saved: '内訳を保存しました。' if saved else '内訳を削除しました。', edit_url
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

    return save_and_redirect(
        mutate, lambda _: '内訳を削除しました。', url_for('ledger.edit_transaction', tx_id=tx_id, back=safe_back())
    )


# ------------------------------------------------------------------- import
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


# -------------------------------------------------------------------- rules
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


# --------------------------------------------------------------- categories
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


@bp.route('/health')
def health() -> dict[str, str]:
    """起動確認用エンドポイント（Flask が JSON にする。app は app.py の二重起動判定が照合する識別子）"""
    return {'status': 'ok', 'app': 'smart-ledger', 'time': now_iso()}
