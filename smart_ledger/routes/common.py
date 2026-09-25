"""Flask ルーティングの共通部（Blueprint・サービスの組み立て・テンプレートフィルタ・CSRF 対策・エラー画面・画面間で共有するヘルパー）"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from werkzeug.exceptions import SecurityError
from werkzeug.wrappers import Response as WerkzeugResponse

from ..config import Config
from ..constants import BANK_CSV_TARGETS, KIND_LABELS, SOURCE_LABELS, UNCLASSIFIED_LABEL
from ..models import LedgerData, Transaction, now_iso
from ..services.aggregation import month_label
from ..services.backup import DropboxBackup
from ..services.classifier import ClassificationPipeline, JevClassifier, NullClassifier
from ..services.excel_repository import ExcelLockedError, ExcelRepository, ExcelSaveError
from ..services.importer import Importer
from ..services.jev_client import JevClient
from ..services.lan_access import LanAccess, lan_ip
from ..services.normalize import merchant_key

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
    lan: LanAccess | None = None  # LAN モード（SMART_LEDGER_LAN=1）のときだけ作る


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
    lan = LanAccess(lan_ip()) if config.lan else None
    return Services(config=config, repo=repo, importer=importer, lan=lan)


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


@bp.app_template_filter('conf')
def conf(value: float | None) -> str:
    """confidence を小数 2 桁にする

    Args:
        value: confidence（None なら '-'）
    """
    if value is None:
        return '-'

    return f'{float(value):.2f}'


@bp.app_template_filter('source_label')
def source_label(value: str) -> str:
    """classification_source を表示用ラベルにする

    Args:
        value: rule / jev / manual / error
    """
    return SOURCE_LABELS.get(value, value or UNCLASSIFIED_LABEL)


# -------------------------------------------------------------------- hooks
@bp.before_app_request
def reject_untrusted_host() -> None:
    """Host が TRUSTED_HOSTS に無いリクエストを 400 にする（Flask は URL の照合まで遅らせ、それまでのフックでは
    url_for が使えないため、最初のフックで止める）
    """
    if isinstance(request.routing_exception, SecurityError):
        raise request.routing_exception


@bp.before_app_request
def reject_cross_site_post() -> None:
    """Origin / Referer のホストが一致しない POST を 403 にする（別サイトのページからの CSRF 対策）"""
    source = request.headers.get('Origin') or request.referrer
    if request.method == 'POST' and source and urlsplit(source).netloc != request.host:
        abort(403)


@bp.app_context_processor
def inject_globals() -> dict[str, object]:
    """全テンプレートで使う設定値を注入する"""
    config = svc().config
    return {
        'threshold': config.confidence_threshold,
        'dropbox_enabled': config.dropbox_path is not None,
        'jev_enabled': bool(config.typesafe_api_key),
        'excel_path': str(config.excel_path),
        'kind_labels': KIND_LABELS,
        'bank_target_categories': [category for _, category in BANK_CSV_TARGETS],  # 取込画面の説明文に使う
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


def safe_back() -> str:
    """リクエストの back パラメータをサイト内の相対パスに限定して返す（外部 URL や javascript: は明細一覧に置き換える）

    '//' で始まる値に加え、タブを挟んだ '/<タブ>/host' も弾く（urlsplit とブラウザはタブを除いて '//host' と解釈する）
    """
    value = request.values.get('back')
    if value and value.startswith('/') and not urlsplit(value).netloc and '\\' not in value:  # ブラウザは \ も / と扱う
        return value

    return url_for('ledger.transactions')


def edit_url(tx_id: str) -> str:
    """明細編集画面の URL を返す（戻り先として safe_back() を引き継ぐ）

    Args:
        tx_id: 明細 ID
    """
    return url_for('ledger.edit_transaction', tx_id=tx_id, back=safe_back())


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


@bp.route('/health')
def health() -> dict[str, str]:
    """起動確認用エンドポイント（Flask が JSON にする。app は app.py の二重起動判定が照合する識別子）"""
    return {'status': 'ok', 'app': 'smart-ledger', 'time': now_iso()}
