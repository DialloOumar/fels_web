"""Excel export of a list, as the page shows it but every row, not one page.

Figures go out as numbers and dates as dates, so the sheet can be summed and
sorted; the header is bold and frozen, and filters are switched on. A money
column carries its currency in a column of its own -- a USD and a GNF figure
never share one."""
import io
from datetime import date, datetime
from decimal import Decimal

from flask import send_file
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="0F172A")
HEADER_FONT = Font(bold=True, color="FFFFFF")
MONEY_FORMAT = "#,##0.00"
DATE_FORMAT = "DD/MM/YYYY"


def wants_xlsx(request):
    return request.args.get("format") == "xlsx"


def xlsx_response(filename, title, headers, rows):
    """`headers`: column titles. `rows`: lists of cell values (str, int,
    Decimal, date, None). Returns a download of filename-YYYY-MM-DD.xlsx."""
    wb = Workbook()
    ws = wb.active
    ws.title = title[:31]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    widths = [len(str(h)) for h in headers]
    for row in rows:
        out = []
        for v in row:
            if isinstance(v, Decimal):
                v = float(v)
            out.append(v)
        ws.append(out)
        r = ws.max_row
        for i, v in enumerate(row):
            cell = ws.cell(row=r, column=i + 1)
            if isinstance(v, Decimal) or (isinstance(v, float)):
                cell.number_format = MONEY_FORMAT
            elif isinstance(v, (date, datetime)):
                cell.number_format = DATE_FORMAT
            text = v.strftime("%d/%m/%Y") if isinstance(v, (date, datetime)) else ("" if v is None else str(v))
            widths[i] = max(widths[i], min(len(text), 60))
    for i, w in enumerate(widths):
        ws.column_dimensions[get_column_letter(i + 1)].width = w + 3
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name="%s-%s.xlsx" % (filename, date.today().isoformat()),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
