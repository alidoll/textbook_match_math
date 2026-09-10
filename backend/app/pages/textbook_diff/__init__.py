from flask import Blueprint

pages_textbook_diff_bp = Blueprint("pages_textbook_diff", __name__, url_prefix="/textbook-diff")

from . import compare, home, intake, view, workbook  # noqa: E402, F401
