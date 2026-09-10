from __future__ import annotations

from flask import current_app, redirect, render_template

from . import pages_textbook_diff_bp


@pages_textbook_diff_bp.get("/")
def textbook_diff_home():
    if current_app.config.get("IS_TEST_DIFF_DISTRIBUTION"):
        return redirect("/textbook-diff/workbook?subject=test")
    return render_template("textbook_diff/home.html")
