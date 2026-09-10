from __future__ import annotations

from flask import render_template

from . import pages_textbook_diff_bp


@pages_textbook_diff_bp.get("/workbook")
def textbook_diff_workbook():
    return render_template("textbook_diff/workbook.html")
