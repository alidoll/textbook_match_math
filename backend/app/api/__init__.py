"""JSON API 路由。"""
from flask import Blueprint

api_bp = Blueprint("api", __name__, url_prefix="/api")

from . import health, volumes, benchmark, file_blobs, llm_key  # noqa: E402, F401
