"""年間表の CSV / Excel エクスポート（集計は aggregation.annual_table の結果をそのまま表にする）"""

from __future__ import annotations

import csv
import io

from openpyxl import Workbook

from .aggregation import AnnualTable


def annual_table_rows(table: AnnualTable) -> list[list[str | int]]:
    """年間表をヘッダー行付きの 2 次元リストにする（CSV / Excel 共通のレイアウト）

    Args:
        table: 年間表

    Returns:
        先頭行が見出し（カテゴリ, 1月〜12月, 年間合計）、続いてカテゴリ行、最後に月間総支出の行
    """
    return [
        ['カテゴリ', *(f'{int(m[5:])}月' for m in table.months), '年間合計'],
        *([r.category, *r.amounts, r.total] for r in table.rows),
        ['月間総支出', *table.monthly_totals, table.total],
    ]


def annual_csv(table: AnnualTable) -> bytes:
    """年間表を BOM 付き UTF-8 の CSV にする（Windows の Excel で開いても文字化けしない）

    Args:
        table: 年間表
    """
    buf = io.StringIO()
    csv.writer(buf).writerows(annual_table_rows(table))
    return buf.getvalue().encode('utf-8-sig')


def annual_xlsx(table: AnnualTable) -> bytes:
    """年間表を 1 シートの Excel にする（金額は 3 桁区切り、見出し行と見出し列を固定）

    Args:
        table: 年間表
    """
    wb = Workbook()
    ws = wb.active
    ws.title = f'{table.year}年'
    for row in annual_table_rows(table):
        ws.append(row)

    for cell in ws['A']:  # '=' 始まりのカテゴリ名を数式として保存しない
        cell.data_type = 's'

    ws.freeze_panes = 'B2'
    ws.column_dimensions['A'].width = 18
    for col in ws.iter_cols(min_col=2):
        ws.column_dimensions[col[0].column_letter].width = 12
        for cell in col[1:]:
            cell.number_format = '#,##0'

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
