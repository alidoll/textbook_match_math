"""数据库连接串生成。

默认使用本地 SQLite 文件（无需起 MySQL、不连远程），repo/data 下一个 .db 文件。
设 ``USE_MYSQL=1`` 时回退到原 MySQL 分支（读 .env 的 MYSQL_* / DATABASE_URL）。
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus, unquote, urlparse

from dotenv import load_dotenv


def sqlite_url(repo_root=None) -> str:
    """本地 SQLite 文件路径（绝对路径，4 斜杠表示绝对路径）。"""
    if repo_root is not None:
        root = Path(repo_root)
    else:
        # 打包后用 app_root()，开发时用 cwd（向后兼容）
        try:
            from .repo_paths import app_root
            root = app_root()
        except ImportError:
            root = Path.cwd()
    db_dir = root / "data"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "textbook_match.db"
    # sqlite:/// = 相对，sqlite://// = 绝对（Posix）；Windows 路径用三斜杠 + 绝对路径
    return f"sqlite:///{db_path.as_posix()}"


def load_mysql_settings(repo_root=None) -> dict:
    """
    优先 MYSQL_* 分项配置（密码可含中文）；
    否则解析 DATABASE_URL。
    """
    if repo_root is not None:
        from pathlib import Path

        env_path = Path(repo_root) / ".env"
        load_dotenv(env_path, encoding="utf-8-sig")
    else:
        load_dotenv(encoding="utf-8-sig")

    if os.getenv("MYSQL_USER") or os.getenv("MYSQL_PASSWORD"):
        return {
            "host": os.getenv("MYSQL_HOST", "localhost").strip(),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root").strip(),
            "password": (os.getenv("MYSQL_PASSWORD") or "").strip(),
            "database": os.getenv("MYSQL_DATABASE", "textbook_match").strip(),
        }

    url = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://root@127.0.0.1:3306/textbook_match?charset=utf8mb4",
    )
    raw = url.replace("mysql+pymysql://", "mysql://").replace("mysql+mysqlconnector://", "mysql://")
    p = urlparse(raw)
    return {
        "host": p.hostname or "127.0.0.1",
        "port": p.port or 3306,
        "user": unquote(p.username or "root"),
        "password": unquote(p.password or ""),
        "database": (p.path or "/textbook_match").lstrip("/").split("?")[0],
    }


def sqlalchemy_url(settings: dict | None = None, *, repo_root=None) -> str:
    """生成 SQLAlchemy 连接串。

    默认返回本地 SQLite 文件 URL；仅当 ``USE_MYSQL=1`` 时走 MySQL 分支
    （支持 Unicode 密码），用于回退到远程 / 自建 MySQL。
    """
    if os.getenv("USE_MYSQL", "").strip() not in ("1", "true", "True"):
        return sqlite_url(repo_root)

    cfg = settings or load_mysql_settings(repo_root)
    user = quote_plus(cfg["user"])
    password = quote_plus(cfg["password"])
    host = cfg["host"]
    port = cfg["port"]
    database = cfg["database"]
    return (
        f"mysql+mysqlconnector://{user}:{password}@{host}:{port}/{database}"
        f"?charset=utf8mb4"
    )
