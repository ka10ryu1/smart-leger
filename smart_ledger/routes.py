"""Flask ルーティング。画面: ダッシュボード / 明細一覧 / CSV取込 / 要確認 / 明細編集 / ルール。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

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

from .config import Config
from .models import SOURCE_MANUAL, SOURCE_RULE, LedgerData, Transaction, now_iso
from .services.aggregation import (
    available_months,
    month_label,
    monthly_summary,
    monthly_trend,
    shift_month,
    transactions_in_month,
)
from .services.allocations import AllocationError, AllocationInput, replace_allocations, validate_allocations
from .services.backup import DropboxBackup
from .services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from .services.csv_parser import CsvParseError
from .services.excel_repository import ExcelLockedError, ExcelRepository, ExcelSaveError
from .services.importer import Importer
from .services.jev_client import JevClient
from .services.merchant_rules import delete_rule, match_rule, upsert_rule

logger = logging.getLogger(__name__)

bp = Blueprint("ledger", __name__)
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass
class Services:
    config: Config
    repo: ExcelRepository
    pipeline: ClassificationPipeline
    importer: Importer


def build_services(config: Config) -> Services:
    config.ensure_dirs()
    repo = ExcelRepository(
        config.excel_path,
        backup_dir=config.backup_dir,
        backup_generations=config.backup_generations,
        dropbox=DropboxBackup(config.dropbox_path),
    )
    jev = JevClient(config.typesafe_api_key, model=config.typesafe_model, base_url=config.typesafe_base_url)
    fallback = (
        JevClassifier(jev)
        if jev.configured
        else NullClassifier("TYPESAFE_API_KEY が未設定のため自動分類できませんでした")
    )
    pipeline = ClassificationPipeline(fallback, threshold=config.confidence_threshold)
    importer = Importer(config.staging_dir, pipeline)
    return Services(config=config, repo=repo, pipeline=pipeline, importer=importer)


def svc() -> Services:
    return current_app.extensions["smart_ledger"]


# ------------------------------------------------------------------ filters
@bp.app_template_filter("yen")
def yen(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


@bp.app_template_filter("pct")
def pct(value) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.1f}%"


@bp.app_template_filter("conf")
def conf(value) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}"


@bp.app_template_filter("month_label")
def _month_label(value: str) -> str:
    return month_label(value)


@bp.app_template_filter("source_label")
def source_label(value: str) -> str:
    return {"rule": "ルール", "jev": "Jev", "manual": "手動", "error": "エラー"}.get(value, value or "未分類")


@bp.app_context_processor
def inject_globals():
    config = svc().config
    return {
        "threshold": config.confidence_threshold,
        "dropbox_enabled": config.dropbox_path is not None,
        "jev_enabled": bool(config.typesafe_api_key),
        "excel_path": str(config.excel_path),
    }


# ------------------------------------------------------------------- errors
@bp.app_errorhandler(ExcelLockedError)
def handle_locked(exc):
    logger.error("Excel ロック: %s", exc)
    return render_template("error.html", title="Excel を書き込めません", message=str(exc)), 423


@bp.app_errorhandler(ExcelSaveError)
def handle_save_error(exc):
    logger.error("Excel 保存失敗: %s", exc)
    return render_template("error.html", title="Excel の保存に失敗しました", message=str(exc)), 500


# ------------------------------------------------------------------ helpers
def _current_month(data: LedgerData) -> str:
    month = request.args.get("month", "").strip()
    if _MONTH_RE.match(month):
        return month
    months = available_months(data.transactions)
    return months[0] if months else date.today().strftime("%Y-%m")


def _load() -> LedgerData:
    return svc().repo.load()


def _needs_review(data: LedgerData) -> list[Transaction]:
    th = svc().config.confidence_threshold
    return [t for t in data.transactions if t.needs_review(th)]


# ------------------------------------------------------------------- routes
@bp.route("/")
def dashboard():
    data = _load()
    month = _current_month(data)
    summary = monthly_summary(data, month)
    month_txs = sorted(transactions_in_month(data.transactions, month), key=lambda t: (t.usage_date, t.id), reverse=True)
    alloc_ids = {a.transaction_id for a in data.allocations}
    return render_template(
        "dashboard.html",
        month=month,
        prev_month=shift_month(month, -1),
        next_month=shift_month(month, 1),
        summary=summary,
        recent=month_txs[:10],
        trend=monthly_trend(data, months=6, end_month=month),
        review_count=len(_needs_review(data)),
        months=available_months(data.transactions),
        alloc_ids=alloc_ids,
        has_data=bool(data.transactions),
    )


@bp.route("/transactions")
def transactions():
    data = _load()
    month = request.args.get("month", "").strip()
    if month and not _MONTH_RE.match(month):
        month = ""
    category = request.args.get("category", "").strip()
    query = request.args.get("q", "").strip().casefold()
    source = request.args.get("source", "").strip()
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
    alloc_ids = {a.transaction_id for a in data.allocations}
    return render_template(
        "transactions.html",
        transactions=txs,
        total=sum(t.amount for t in txs),
        months=available_months(data.transactions),
        categories=data.category_names(),
        filters={"month": month, "category": category, "q": request.args.get("q", ""), "source": source},
        alloc_ids=alloc_ids,
    )


@bp.route("/review")
def review():
    data = _load()
    txs = sorted(_needs_review(data), key=lambda t: (t.usage_date, t.id), reverse=True)
    return render_template("review.html", transactions=txs, categories=data.category_names())


@bp.route("/transactions/<tx_id>/edit")
def edit_transaction(tx_id: str):
    data = _load()
    tx = data.find_transaction(tx_id)
    if tx is None:
        abort(404)
    allocations = data.allocations_for(tx_id)
    rule = match_rule(data.merchant_rules, tx.merchant_normalized)
    same_merchant = [t for t in data.transactions if t.merchant_normalized == tx.merchant_normalized and t.id != tx.id]
    return render_template(
        "edit.html",
        tx=tx,
        allocations=allocations,
        categories=data.category_names(),
        rule=rule,
        same_merchant_count=len(same_merchant),
        back=request.args.get("back") or request.referrer or url_for("ledger.transactions"),
    )


@bp.route("/transactions/<tx_id>/category", methods=["POST"])
def update_category(tx_id: str):
    category = request.form.get("category", "").strip()
    scope = request.form.get("scope", "once")
    memo = request.form.get("memo", "").strip()
    back = request.form.get("back") or url_for("ledger.transactions")
    applied_others = 0

    def mutate(data: LedgerData) -> None:
        nonlocal applied_others
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)
        if category not in data.category_names():
            raise ValueError(f"不明なカテゴリです: {category}")
        tx.category = category
        tx.confidence = None
        tx.classification_source = SOURCE_MANUAL
        tx.memo = memo
        if scope == "always":
            upsert_rule(data, tx.merchant_normalized, category)
            # 同じ加盟店で手動修正されていない明細にも反映する
            for other in data.transactions:
                if (
                    other.id != tx.id
                    and other.merchant_normalized == tx.merchant_normalized
                    and other.classification_source != SOURCE_MANUAL
                    and other.category != category
                ):
                    other.category = category
                    other.confidence = None
                    other.classification_source = SOURCE_RULE
                    applied_others += 1

    try:
        svc().repo.update(mutate)
    except ValueError as exc:  # 不明なカテゴリなど
        flash(str(exc), "error")
        return redirect(url_for("ledger.edit_transaction", tx_id=tx_id, back=back))

    if scope == "always":
        msg = f"カテゴリを「{category}」に変更し、この加盟店のルールを登録しました。"
        if applied_others:
            msg += f" 同じ加盟店の {applied_others} 件にも適用しました。"
    else:
        msg = f"カテゴリを「{category}」に変更しました(今回だけ)。"
    flash(msg, "success")
    return redirect(back)


@bp.route("/transactions/<tx_id>/allocations", methods=["POST"])
def update_allocations(tx_id: str):
    back = request.form.get("back") or url_for("ledger.transactions")
    categories = request.form.getlist("alloc_category")
    amounts = request.form.getlist("alloc_amount")
    memos = request.form.getlist("alloc_memo")
    items: list[AllocationInput] = []
    for i in range(max(len(categories), len(amounts))):
        cat = categories[i].strip() if i < len(categories) else ""
        amt_raw = (amounts[i] if i < len(amounts) else "").replace(",", "").strip()
        memo = memos[i].strip() if i < len(memos) else ""
        if not cat and not amt_raw and not memo:
            continue
        try:
            amt = int(amt_raw) if amt_raw else 0
        except ValueError:
            flash(f"内訳の金額が数値ではありません: {amt_raw}", "error")
            return redirect(url_for("ledger.edit_transaction", tx_id=tx_id, back=back))
        items.append(AllocationInput(category=cat, amount=amt, memo=memo))

    def mutate(data: LedgerData) -> None:
        tx = data.find_transaction(tx_id)
        if tx is None:
            abort(404)
        new_allocs = validate_allocations(tx, items, data.category_names())
        replace_allocations(data, tx_id, new_allocs)

    try:
        svc().repo.update(mutate)
    except AllocationError as exc:
        flash(str(exc), "error")
        return redirect(url_for("ledger.edit_transaction", tx_id=tx_id, back=back))
    flash("内訳を保存しました。" if items else "内訳を削除しました。", "success")
    return redirect(url_for("ledger.edit_transaction", tx_id=tx_id, back=back))


@bp.route("/transactions/<tx_id>/allocations/clear", methods=["POST"])
def clear_allocations(tx_id: str):
    back = request.form.get("back") or url_for("ledger.transactions")
    svc().repo.update(lambda data: replace_allocations(data, tx_id, []))
    flash("内訳を削除しました。", "success")
    return redirect(url_for("ledger.edit_transaction", tx_id=tx_id, back=back))


# ------------------------------------------------------------------- import
@bp.route("/import")
def import_page():
    data = _load()
    imports = sorted(data.imports, key=lambda i: i.imported_at, reverse=True)
    return render_template("import.html", preview=None, imports=imports)


@bp.route("/import/preview", methods=["POST"])
def import_preview():
    file = request.files.get("csv_file")
    if file is None or not file.filename:
        flash("CSV ファイルを選択してください。", "error")
        return redirect(url_for("ledger.import_page"))
    content = file.read()
    if not content:
        flash("空のファイルです。", "error")
        return redirect(url_for("ledger.import_page"))
    data = _load()
    try:
        preview = svc().importer.preview(data, file.filename, content)
    except CsvParseError as exc:
        flash(f"CSV を解析できませんでした: {exc}", "error")
        return redirect(url_for("ledger.import_page"))
    imports = sorted(data.imports, key=lambda i: i.imported_at, reverse=True)
    return render_template("import.html", preview=preview, imports=imports)


@bp.route("/import/commit", methods=["POST"])
def import_commit():
    token = request.form.get("token", "")
    preview = svc().importer.load_preview(token)
    if preview is None:
        flash("プレビュー情報が見つかりません。もう一度 CSV を選択してください。", "error")
        return redirect(url_for("ledger.import_page"))
    if preview.already_imported:
        flash("このCSVはすでに取り込み済みです。", "warning")
        return redirect(url_for("ledger.import_page"))

    result_holder = {}

    def mutate(data: LedgerData) -> None:
        if data.has_file_hash(preview.file_hash):
            raise CsvParseError("このCSVはすでに取り込み済みです。")
        result_holder["result"] = svc().importer.commit(data, preview)

    try:
        svc().repo.update(mutate)
    except CsvParseError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("ledger.import_page"))
    svc().importer.discard(token)
    result = result_holder["result"]
    dropbox = svc().repo.last_dropbox_result
    msg = (
        f"{result.imported} 件を取り込みました(重複スキップ {result.skipped_duplicates} 件、"
        f"ルール一致 {result.rule_matched} 件、自動採用 {result.auto_accepted} 件、要確認 {result.needs_review} 件)。"
    )
    if result.jev_errors:
        msg += f" Jev 分類エラー {result.jev_errors} 件は「その他」として要確認に入っています。"
    if dropbox:
        msg += " Dropbox へもコピーしました。"
    flash(msg, "success")
    if result.needs_review:
        return redirect(url_for("ledger.review"))
    target_month = result.months[-1] if result.months else None
    return redirect(url_for("ledger.dashboard", month=target_month) if target_month else url_for("ledger.dashboard"))


@bp.route("/import/discard", methods=["POST"])
def import_discard():
    svc().importer.discard(request.form.get("token", ""))
    flash("取込をキャンセルしました。", "info")
    return redirect(url_for("ledger.import_page"))


# -------------------------------------------------------------------- rules
@bp.route("/rules")
def rules():
    data = _load()
    return render_template(
        "rules.html",
        rules=sorted(data.merchant_rules, key=lambda r: r.created_at, reverse=True),
        categories=data.category_names(),
    )


@bp.route("/rules/add", methods=["POST"])
def add_rule():
    pattern = request.form.get("merchant_pattern", "").strip()
    category = request.form.get("category", "").strip()
    if not pattern or not category:
        flash("加盟店パターンとカテゴリを入力してください。", "error")
        return redirect(url_for("ledger.rules"))
    svc().repo.update(lambda data: upsert_rule(data, pattern, category))
    flash(f"ルールを登録しました: {pattern} → {category}", "success")
    return redirect(url_for("ledger.rules"))


@bp.route("/rules/delete", methods=["POST"])
def remove_rule():
    pattern = request.form.get("merchant_pattern", "")
    svc().repo.update(lambda data: delete_rule(data, pattern))
    flash("ルールを削除しました。", "success")
    return redirect(url_for("ledger.rules"))


@bp.route("/health")
def health():
    return {"status": "ok", "time": now_iso()}
