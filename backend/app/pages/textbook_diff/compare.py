from __future__ import annotations

from flask import render_template

from . import pages_textbook_diff_bp


@pages_textbook_diff_bp.get("/compare")
def compare_page():
    return render_template("textbook_diff/compare.html")
