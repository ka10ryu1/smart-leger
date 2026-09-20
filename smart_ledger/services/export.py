"""年間表の CSV / Excel エクスポート（集計は aggregation.annual_table の結果をそのまま表にする）"""

from __future__ import annotations

import csv
import io

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .aggregation import AnnualTable


def annual_table_rows(table: AnnualTable, total_label: str = '月間総支出') -> list[list[str | int]]:
    """年間表をヘッダー行付きの 2 次元リストにする（CSV / Excel 共通のレイアウト）

    Args:
        table: 年間表
        total_label: 最下行（月間総支出）の見出し

    Returns:
        先頭行が見出し（カテゴリ, 1月〜12月, 年間合計）、続いてカテゴリ行、最後に月間総支出の行
    """
    header: list[str | int] = ['カテゴリ', *(f'{int(m[5:])}月' for m in table.months), '年間合計']
    body: list[list[str | int]] = [[r.category, *r.amounts, r.total] for r in table.rows]
    footer: list[str | int] = [total_label, *table.monthly_totals, table.total]
    return [header, *body, footer]


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
    rows = annual_table_rows(table)
    for row_no, row in enumerate(rows, start=1):
        ws.append(row)
        if isinstance(row[0], str) and row[0].startswith('='):  # '=' 始まりのカテゴリ名を数式として保存しない
            ws.cell(row=row_no, column=1).data_type = 's'

    ws.freeze_panes = 'B2'
    ws.column_dimensions['A'].width = 18
    for col_no in range(2, len(rows[0]) + 1):
        letter = get_column_letter(col_no)
        ws.column_dimensions[letter].width = 12
        for cell in ws[letter][1:]:
            cell.number_format = '#,##0'

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
