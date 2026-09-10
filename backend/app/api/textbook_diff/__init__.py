from flask import Blueprint

api_textbook_diff_bp = Blueprint("api_textbook_diff", __name__, url_prefix="/api/textbook-diff")

from . import (  # noqa: E402, F401
    compare,
    intake,
    intake_compare,
    test_tools,
    view,
    volumes,
    workbook,
)
