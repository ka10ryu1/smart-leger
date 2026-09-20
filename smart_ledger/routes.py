"""Flask ルーティング（画面: ダッシュボード / 明細一覧 / CSV 取込 / 要確認 / 明細編集 / ルール）"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable
from urllib.parse import urlsplit

from flask import (
    Blueprint,
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
)
from .models import LedgerData, Transaction, now_iso
from .services.aggregation import (
    available_months,
    month_label,
    monthly_summary,
    monthly_trend,
    shift_month,
    transactions_in_month,
)
from .services.allocations import AllocationInput, replace_allocations, validate_allocations
from .services.backup import DropboxBackup
from .services.categories import (
    CategoryError,
    add_category,
    category_usage,
    delete_category,
    edit_category,
    move_category,
    sorted_categories,
)
from .services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from .services.csv_parser import CsvParseError
from .services.excel_repository import ExcelLockedError, ExcelRepository, ExcelSaveError
from .services.importer import Importer, ImportResult
from .services.jev_client import JevClient
from .services.merchant_rules import delete_rule, match_rule, upsert_rule

logger = logging.getLogger(__name__)

bp = Blueprint('ledger', __name__)
bp.add_app_template_filter(month_label, 'month_label')


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
        txs = [t for t in txs if t.category == category]

    if source:
        txs = [t for t in txs if t.classification_source == source]

    if query:
        txs = [t for t in txs if query in t.merchant_normalized.casefold() or query in t.merchant_raw.casefold()]

    txs.sort(key=lambda t: (t.usage_date, t.id), reverse=True)
    return render_template(
        'transactions.html',
        transactions=txs,
        total=sum(t.amount for t in txs),
        months=available_months(data.transactions),
        categories=data.category_names(),
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

    same_merchant = [t for t in data.transactions if t.merchant_normalized == tx.merchant_normalized and t.id != tx.id]
    return render_template(
        'edit.html',
        tx=tx,
        allocations=data.allocations_for(tx_id),
        categories=data.category_names(),
        rule=match_rule(data.merchant_rules, tx.merchant_normalized),
        same_merchant_count=len(same_merchant),
        back=safe_back(),
    )


@bp.route('/transactions/<tx_id>/category', methods=['POST'])
def update_category(tx_id: str) -> WerkzeugResponse:
    """カテゴリ変更（scope=once なら今回だけ、always ならルール登録して同じ加盟店にも反映）

    Args:
        tx_id: 明細 ID
    """
    category = request.form.get('category', '').strip()
    scope = request.form.get('scope', 'once')
    memo = request.form.get('memo', '').strip()
    back = safe_back()
    applied_others = 0

    def mutate(data: LedgerData) -> None:
        nonlocal applied_others
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
            return

        upsert_rule(data, tx.merchant_normalized, category)
        for other in data.transactions:  # 同じ加盟店で手動修正されていない明細にも反映する
            is_target = (
                other.id != tx.id
                and other.merchant_normalized == tx.merchant_normalized
                and other.classification_source != SOURCE_MANUAL
                and other.category != category
            )
            if not is_target:
                continue

            other.category = category
            other.confidence = None
            other.classification_source = SOURCE_RULE
            applied_others += 1

    try:
        svc().repo.update(mutate)
    except ValueError as exc:  # 不明なカテゴリなど
        flash(str(exc), 'error')
        return redirect(url_for('ledger.edit_transaction', tx_id=tx_id, back=back))

    if scope == 'always':
        msg = f'カテゴリを「{category}」に変更し、この加盟店のルールを登録しました。'
        if applied_others:
            msg += f' 同じ加盟店の {applied_others} 件にも適用しました。'
    else:
        msg = f'カテゴリを「{category}」に変更しました(今回だけ)。'

    flash(msg, 'success')
    return redirect(back)


@bp.route('/transactions/<tx_id>/allocations', methods=['POST'])
def update_allocations(tx_id: str) -> WerkzeugResponse:
    """内訳を保存する（合計が明細金額と一致しなければエラー表示）

    Args:
        tx_id: 明細 ID
    """
    edit_url = url_for('ledger.edit_transaction', tx_id=tx_id, back=safe_back())

    def mutate(data: LedgerData) -> None:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)

        replace_allocations(data, tx_id, validate_allocations(tx, items, data.category_names()))

    try:
        items = parse_allocation_form()
        svc().repo.update(mutate)
    except ValueError as exc:  # 金額が数値でない / AllocationError
        flash(str(exc), 'error')
        return redirect(edit_url)

    flash('内訳を保存しました。' if items else '内訳を削除しました。', 'success')
    return redirect(edit_url)


@bp.route('/transactions/<tx_id>/allocations/clear', methods=['POST'])
def clear_allocations(tx_id: str) -> WerkzeugResponse:
    """内訳をすべて削除する

    Args:
        tx_id: 明細 ID
    """
    back = safe_back()
    svc().repo.update(lambda data: replace_allocations(data, tx_id, []))
    flash('内訳を削除しました。', 'success')
    return redirect(url_for('ledger.edit_transaction', tx_id=tx_id, back=back))


# ------------------------------------------------------------------- import
@bp.route('/import')
def import_page() -> str:
    """CSV 取込画面（ファイル選択と取込履歴）"""
    data = load_data()
    imports = sorted(data.imports, key=lambda i: i.imported_at, reverse=True)
    return render_template('import.html', preview=None, imports=imports)


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

    imports = sorted(data.imports, key=lambda i: i.imported_at, reverse=True)
    return render_template('import.html', preview=preview, imports=imports)


@bp.route('/import/commit', methods=['POST'])
def import_commit() -> WerkzeugResponse:
    """プレビューの新規明細だけを分類して Excel に保存する"""
    token = request.form.get('token', '')
    preview = svc().importer.load_preview(token)
    if preview is None:
        flash('プレビュー情報が見つかりません。もう一度 CSV を選択してください。', 'error')
        return redirect(url_for('ledger.import_page'))

    results: list[ImportResult] = []

    def mutate(data: LedgerData) -> None:
        if data.has_file_hash(preview.file_hash):
            raise CsvParseError('このCSVはすでに取り込み済みです。')

        results.append(svc().importer.commit(data, preview))

    try:
        svc().repo.update(mutate)
    except CsvParseError as exc:
        flash(str(exc), 'warning')
        return redirect(url_for('ledger.import_page'))

    svc().importer.discard(token)
    result = results[0]
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

    try:
        svc().repo.update(lambda data: upsert_rule(data, pattern, category))
    except ValueError as exc:  # 不明なカテゴリなど
        flash(str(exc), 'error')
        return redirect(url_for('ledger.rules'))

    flash(f'ルールを登録しました: {pattern} → {category}', 'success')
    return redirect(url_for('ledger.rules'))


@bp.route('/rules/delete', methods=['POST'])
def remove_rule() -> WerkzeugResponse:
    """ルールを削除する"""
    pattern = request.form.get('merchant_pattern', '')
    svc().repo.update(lambda data: delete_rule(data, pattern))
    flash('ルールを削除しました。', 'success')
    return redirect(url_for('ledger.rules'))


# --------------------------------------------------------------- categories
@bp.route('/categories')
def categories_page() -> str:
    """カテゴリ管理画面（追加・名称変更・並び替え・削除）"""
    data = load_data()
    return render_template(
        'categories.html',
        categories=sorted_categories(data),
        usage=category_usage(data),
        fallback=FALLBACK_CATEGORY,
        default_descriptions=CATEGORY_DESCRIPTIONS,
    )


def apply_category_change(mutator: Callable[[LedgerData], object], success: str) -> WerkzeugResponse:
    """カテゴリ操作を保存し、結果を flash してカテゴリ画面へ戻る

    Args:
        mutator: LedgerData を書き換える関数（CategoryError で失敗を通知する）
        success: 成功時に表示するメッセージ
    """
    try:
        svc().repo.update(mutator)
    except CategoryError as exc:
        flash(str(exc), 'error')
        return redirect(url_for('ledger.categories_page'))

    flash(success, 'success')
    return redirect(url_for('ledger.categories_page'))


@bp.route('/categories/add', methods=['POST'])
def add_category_route() -> WerkzeugResponse:
    """カテゴリを追加する"""
    name = request.form.get('name', '')
    description = request.form.get('description', '')
    return apply_category_change(
        lambda data: add_category(data, name, description), f'カテゴリ「{name.strip()}」を追加しました。'
    )


@bp.route('/categories/edit', methods=['POST'])
def edit_category_route() -> WerkzeugResponse:
    """カテゴリの名称・説明を変更する（名称変更は明細・ルール・内訳に伝播）"""
    name = request.form.get('category', '')
    new_name = request.form.get('new_name', '')
    description = request.form.get('description', '')
    changed: list[int] = []
    response = apply_category_change(
        lambda data: changed.append(edit_category(data, name, new_name, description)),
        f'カテゴリ「{new_name.strip()}」を保存しました。',
    )
    if changed and changed[0]:
        flash(f'名称変更を明細・ルール・内訳の {changed[0]} 件に反映しました。', 'info')

    return response


@bp.route('/categories/move', methods=['POST'])
def move_category_route() -> WerkzeugResponse:
    """カテゴリの表示順を上下に動かす"""
    name = request.form.get('category', '')
    delta = -1 if request.form.get('direction') == 'up' else 1
    return apply_category_change(lambda data: move_category(data, name, delta), '並び順を変更しました。')


@bp.route('/categories/delete', methods=['POST'])
def delete_category_route() -> WerkzeugResponse:
    """未使用のカテゴリを削除する"""
    name = request.form.get('category', '')
    return apply_category_change(lambda data: delete_category(data, name), f'カテゴリ「{name}」を削除しました。')


@bp.route('/health')
def health() -> dict[str, str]:
    """起動確認用エンドポイント（Flask が JSON にする）"""
    return {'status': 'ok', 'time': now_iso()}
