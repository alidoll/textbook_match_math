"""页面路由（Jinja）。"""
from flask import Blueprint

pages_bp = Blueprint("pages", __name__)

from . import home  # noqa: E402, F401
