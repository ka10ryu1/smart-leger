"""明細一覧（絞り込み）と要確認一覧のルート"""

from __future__ import annotations

from flask import render_template, request

from ..constants import CONFIDENCE_FILTER_PATTERN, FALLBACK_CATEGORY, KIND_LABELS, MONTH_PATTERN, UNCLASSIFIED_LABEL
from ..services.aggregation import available_months, total_income, total_spending, transactions_in_month
from .common import bp, conf, load_data, needs_review


def parse_confidence_filter(
    raw: str, op: str, ops: tuple[str, ...] = ('gte', 'lte', 'none')
) -> tuple[float | None, str]:
    """明細一覧の確信度の絞り込み条件を解釈する（CONFIDENCE_FILTER_PATTERN に合わない入力は None にして絞り込まない）

    Args:
        raw: 閾値の入力文字列（conf パラメータ）
        op: 比較方法（gte: 以上 / lte: 以下 / none: 未設定。それ以外はフォームの既定と同じ lte）
        ops: 受け付ける比較方法

    Returns:
        (閾値, 比較方法) のタプル（閾値は不正な入力なら None）
    """
    if op not in ops:
        op = 'lte'

    if not CONFIDENCE_FILTER_PATTERN.match(raw):
        return None, op

    return float(raw), op


def match_confidence(value: float | None, threshold: float | None, op: str) -> bool:
    """明細の confidence が確信度の絞り込み条件に一致するか（一覧の表示と揃えるため conf フィルタの 2 桁表示で比較する）

    Args:
        value: 明細の confidence
        threshold: 閾値（None なら未入力・不正な入力として条件を掛けない）
        op: 比較方法（gte / lte / none。none は閾値を無視して confidence が空の明細だけに一致する）
    """
    if op == 'none':
        return value is None

    if threshold is None:
        return True

    if value is None:  # ルール・手動の明細は confidence が空なので、数値の条件では除外する
        return False

    shown = float(conf(value))
    return shown >= threshold if op == 'gte' else shown <= threshold


@bp.route('/transactions')
def transactions() -> str:
    """明細一覧（月・収支・カテゴリ・分類元・加盟店名・確信度で絞り込み。カテゴリは内訳のカテゴリにも一致させる）"""
    data = load_data()
    month = request.args.get('month', '').strip()
    if month and not MONTH_PATTERN.match(month):
        month = ''

    category = request.args.get('category', '').strip()
    query = request.args.get('q', '').strip().casefold()
    source = request.args.get('source', '').strip()
    kind = request.args.get('kind', '').strip()
    if kind not in KIND_LABELS:
        kind = ''

    threshold, conf_op = parse_confidence_filter(
        request.args.get('conf', '').strip(), request.args.get('conf_op', '').strip()
    )
    allocs = data.allocations_by_transaction()
    txs = list(data.transactions)
    if month:
        txs = transactions_in_month(txs, month)

    if kind:
        txs = [t for t in txs if t.kind == kind]

    if category:
        # 内訳のカテゴリはカテゴリ別集計（aggregation.category_rows）と同じく空なら その他 とみなす
        txs = [
            t
            for t in txs
            if (t.category or UNCLASSIFIED_LABEL) == category
            or any((a.category or FALLBACK_CATEGORY) == category for a in allocs.get(t.id, []))
        ]

    if source:
        txs = [t for t in txs if t.classification_source == source]

    if query:
        txs = [t for t in txs if query in t.merchant_normalized.casefold() or query in t.merchant_raw.casefold()]

    txs = [t for t in txs if match_confidence(t.confidence, threshold, conf_op)]

    txs.sort(key=lambda t: (t.usage_date, t.id), reverse=True)
    names = data.category_names()
    # 空カテゴリの明細も絞り込めるようにする（旧データに同名カテゴリが残っていれば重複させない）
    if UNCLASSIFIED_LABEL not in names:
        names.append(UNCLASSIFIED_LABEL)

    return render_template(
        'transactions.html',
        transactions=txs,
        total=total_spending(txs),
        income_total=total_income(txs),
        months=available_months(data.transactions),
        categories=names,
        filters={
            'month': month,
            'category': category,
            'q': request.args.get('q', ''),
            'source': source,
            'kind': kind,
            'conf': '' if threshold is None else f'{threshold:.2f}',  # .8 なども一覧の表示と同じ 2 桁表記で戻す
            'conf_op': conf_op,
        },
        allocs=allocs,
    )


@bp.route('/review')
def review() -> str:
    """要確認一覧"""
    data = load_data()
    txs = sorted(needs_review(data), key=lambda t: (t.usage_date, t.id), reverse=True)
    return render_template('review.html', transactions=txs, categories=data.category_names())
