from __future__ import annotations

from flask import render_template

from . import pages_textbook_diff_bp


@pages_textbook_diff_bp.get("/old")
def old_intake():
    return render_template("textbook_diff/workbench.html", book_type="diff_old")


@pages_textbook_diff_bp.get("/new")
def new_intake():
    return render_template("textbook_diff/workbench.html", book_type="diff_new")


@pages_textbook_diff_bp.get("/old/intake")
def old_intake_detail():
    from flask import request
    code = request.args.get("code", "")
    return render_template("textbook_diff/intake.html", book_type="diff_old", volume_code=code)


@pages_textbook_diff_bp.get("/new/intake")
def new_intake_detail():
    from flask import request
    code = request.args.get("code", "")
    return render_template("textbook_diff/intake.html", book_type="diff_new", volume_code=code)


@pages_textbook_diff_bp.get("/old/intake/compare")
@pages_textbook_diff_bp.get("/new/intake/compare")
@pages_textbook_diff_bp.get("/intake/compare")
def intake_compare_page():
    from flask import request
    code = request.args.get("code", "").strip()
    kind = request.args.get("kind", "full").strip()
    preview_blob_id = request.args.get("preview_blob_id", "").strip()
    return render_template(
        "textbook_diff/intake_compare.html",
        volume_code=code,
        kind=kind,
        preview_blob_id=preview_blob_id or "",
    )
