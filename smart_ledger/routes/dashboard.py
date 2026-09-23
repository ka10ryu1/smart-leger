"""ダッシュボード（月次集計）と年間表（CSV / Excel エクスポートを含む）のルート"""

from __future__ import annotations

import logging
from datetime import date

from flask import Response, render_template, request

from ..constants import MONTH_PATTERN, YEAR_PATTERN
from ..models import LedgerData
from ..services.aggregation import (
    annual_table,
    available_months,
    available_years,
    monthly_summary,
    monthly_trend,
    shift_month,
    transactions_in_month,
)
from ..services.export import annual_csv, annual_xlsx
from .common import bp, load_data, needs_review

logger = logging.getLogger(__name__)


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
        allocs=data.allocations_by_transaction(),
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
