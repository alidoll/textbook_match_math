"""Flask 应用工厂。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, url_for
from sqlalchemy import event
from sqlalchemy.engine import Engine

from .extensions import db
from .api import api_bp
from .pages import pages_bp
from .pages.textbook_diff import pages_textbook_diff_bp
from .api.textbook_diff import api_textbook_diff_bp


def _install_sqlite_pragmas() -> None:
    """SQLite 连接级 PRAGMA：WAL 写并发 + busy_timeout，缓解后台管线线程锁等待。"""

    @event.listens_for(Engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
        # 只对 SQLite 连接生效（MySQL 连接走各自驱动，无 PRAGMA）
        mod = type(dbapi_conn).__module__ or ""
        if not mod.startswith("sqlite"):
            return
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _install_safe_tojson(app: Flask) -> None:
    """tojson 遇到未定义模板变量时不抛 TypeError。"""
    from jinja2.runtime import Undefined
    from markupsafe import Markup

    def _safe_tojson(obj, *, indent=None):
        if isinstance(obj, Undefined):
            obj = None
        return Markup(app.json.dumps(obj, indent=indent))

    app.jinja_env.filters["tojson"] = _safe_tojson


def create_app() -> Flask:
    from .repo_paths import app_root, resource_root

    root = app_root()
    load_dotenv(root / ".env", encoding="utf-8-sig", override=True)

    # 模板/静态资源：打包后在 _MEIPASS 下，开发时在 backend/app/ 下
    res = resource_root()
    app = Flask(
        __name__,
        template_folder=str(res / "backend" / "app" / "templates"),
        static_folder=str(res / "backend" / "app" / "static"),
    )
    app.config.from_object("app.config.Config")
    # 必须在 load_dotenv 之后再设，避免进程残留/导入时序导致仍用旧上限
    app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    # 静态资源版本：取 Config / 环境变量中较大者，避免 .env 旧号卡住缓存
    try:
        _cfg_v = int(str(app.config.get("STATIC_ASSET_VERSION") or "1"))
        _env_v = int(os.getenv("STATIC_ASSET_VERSION", str(_cfg_v)))
        app.config["STATIC_ASSET_VERSION"] = str(max(_cfg_v, _env_v, 371))
    except ValueError:
        app.config["STATIC_ASSET_VERSION"] = "336"
    distribution_mode = os.getenv("DISTRIBUTION_MODE", "").strip().lower()
    app.config["DISTRIBUTION_MODE"] = distribution_mode
    app.config["IS_TEST_DIFF_DISTRIBUTION"] = distribution_mode == "test_diff"
    # classic = 现网四卡；pipeline = 教材比对/课件/选题 + 小科完整工作台（可随时切回）
    product_shell = os.getenv("PRODUCT_SHELL", "classic").strip().lower() or "classic"
    if product_shell not in ("classic", "pipeline"):
        product_shell = "classic"
    app.config["PRODUCT_SHELL"] = product_shell
    app.config["IS_PIPELINE_SHELL"] = product_shell == "pipeline"

    db.init_app(app)

    # SQLite 连接级 PRAGMA（MySQL 分支自动跳过）
    _install_sqlite_pragmas()

    # 本地 SQLite 自动建表（IF NOT EXISTS，幂等）。MySQL 走已建好的库，默认也建一次无害。
    if os.getenv("AUTO_CREATE_SCHEMA", "1").strip() not in ("0", "false", "False"):
        with app.app_context():
            # 触发所有模型注册到 metadata（phase_a / phase_b）
            from . import models  # noqa: F401

            db.create_all()

    # 核心：health / file-blobs + 教材对比（含 Test）
    app.register_blueprint(api_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(pages_textbook_diff_bp)
    app.register_blueprint(api_textbook_diff_bp)

    @app.context_processor
    def _inject_template_globals():
        return {
            "static_v": app.config.get("STATIC_ASSET_VERSION", "1"),
            "distribution_mode": app.config.get("DISTRIBUTION_MODE", ""),
            "is_test_diff_distribution": bool(app.config.get("IS_TEST_DIFF_DISTRIBUTION")),
            "product_shell": app.config.get("PRODUCT_SHELL", "classic"),
            "is_pipeline_shell": bool(app.config.get("IS_PIPELINE_SHELL")),
        }

    _install_safe_tojson(app)

    @app.get("/favicon.ico")
    def favicon():
        return redirect(url_for("static", filename="favicon.svg"))

    @app.errorhandler(413)
    def _payload_too_large(_exc):
        from flask import jsonify, request

        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "PDF 文件过大，请调大 MAX_UPLOAD_MB 或压缩后重试"}), 413
        return "文件过大", 413

    return app
