"""CSV / PDF export of a scan's reconciliation. CSV cells are neutralised against spreadsheet formula injection."""
from __future__ import annotations

import csv
import io
import re
from typing import Any

_FORMULA = re.compile(r"^[=+\-@\t\r]")


def _cell(v: Any) -> Any:
    return "'" + v if isinstance(v, str) and _FORMULA.match(v) else v  # only strings: negative numbers stay numeric


def scan_csv(view: dict[str, Any], shelf_name: str) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    for k, v in (("scan_id", view["scan_id"]), ("status", view["status"]), ("shelf", shelf_name), ("created_by", view["created_by"]["name"])):
        w.writerow([k, _cell(v)])
    w.writerow([])
    w.writerow(["sku", "name", "detected", "reviewed", "expected", "variance", "status", "within_tolerance"])
    for r in view["summary"]:
        w.writerow([_cell(r["sku"]), _cell(r["name"]), r["detected"], r["reviewed"], r["expected"], r["variance"], r["status"], r["within_tolerance"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")  # BOM so Excel reads UTF-8


def scan_pdf(view: dict[str, Any], shelf_name: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    st = getSampleStyleSheet()
    cell_style = ParagraphStyle("ReportCell", parent=st["BodyText"], fontSize=8, leading=9, wordWrap="CJK")
    header_style = ParagraphStyle("ReportHeader", parent=cell_style, textColor=colors.white)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=f"Calibrex scan {view['scan_id']}")
    t = view["totals"]
    story: list[Any] = [
        Paragraph("Calibrex count report", st["Title"]),
        Paragraph(escape(f"{shelf_name} · status {view['status']} · counted by {view['created_by']['name']}"), st["Normal"]),
        Paragraph(f"Detected {t['detected']} · sure {t['sure']} · check {t['check']} · unknown {t['unknown']} · unresolved {t['unresolved']}", st["Normal"]),
        Spacer(1, 12)]
    labels = ("SKU", "Product", "Detected", "Reviewed", "Expected", "Variance", "Status")
    data = [[Paragraph(label, header_style) for label in labels]] + [
        [Paragraph(escape(str(r["sku"])), cell_style), Paragraph(escape(str(r["name"])), cell_style),
         Paragraph(str(r["detected"]), cell_style), Paragraph(str(r["reviewed"]), cell_style),
         Paragraph(str(r["expected"]), cell_style), Paragraph(f"{r['variance']:+d}", cell_style),
         Paragraph(escape(str(r["status"])), cell_style)] for r in view["summary"]]
    # Keep the table inside the usable page width and let long values wrap.
    col_widths = [doc.width * fraction for fraction in (0.14, 0.36, 0.09, 0.09, 0.10, 0.10, 0.12)]
    tbl = Table(data, repeatRows=1, colWidths=col_widths)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0C1F4A")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                             ("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [tbl, Spacer(1, 12), Paragraph("Timeline", st["Heading2"])]
    story += [Paragraph(escape(f"{e['at']} · {e['actor']} · {e['event']}" + (f" — {e['note']}" if e.get("note") else "")), st["BodyText"]) for e in view["timeline"]]
    doc.build(story)
    return buf.getvalue()
