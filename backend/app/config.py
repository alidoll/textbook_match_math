"""应用配置。"""
from __future__ import annotations

import os
from pathlib import Path

from .db_settings import load_mysql_settings, sqlalchemy_url
from .repo_paths import app_root

_REPO = app_root()
_USE_MYSQL = os.getenv("USE_MYSQL", "").strip() in ("1", "true", "True")
_settings = load_mysql_settings(_REPO) if _USE_MYSQL else None


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    SQLALCHEMY_DATABASE_URI = sqlalchemy_url(_settings, repo_root=_REPO)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = (
        # MySQL：连接池 + READ COMMITTED
        {
            "pool_pre_ping": True,
            "pool_recycle": 3600,
            "isolation_level": "READ COMMITTED",
            "pool_size": 10,
            "max_overflow": 20,
            "pool_timeout": 30,
        }
        if _USE_MYSQL
        # SQLite：busy_timeout 30s，允许跨线程（Flask threaded=True + 后台管线线程）
        else {
            "connect_args": {"timeout": 30, "check_same_thread": False},
        }
    )
    # 整册教材 PDF 上传上限（默认 2048MB；扫描彩印常超过 512）
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
    # 静态资源版本号（改前端后 bump，避免浏览器强缓存旧 JS）
    STATIC_ASSET_VERSION = os.getenv("STATIC_ASSET_VERSION", "350")
    # 分发模式：test_diff = 仅新旧教材对比（Test 学科），不注册旧库/新库入口
    DISTRIBUTION_MODE = os.getenv("DISTRIBUTION_MODE", "").strip().lower()

    @classmethod
    def is_test_diff_distribution(cls) -> bool:
        return cls.DISTRIBUTION_MODE == "test_diff"
