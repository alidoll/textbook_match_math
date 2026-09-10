#!/usr/bin/env python3
"""启动 Flask 开发服务。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
REPO_ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
os.chdir(REPO_ROOT)


def _ensure_utf8_stdio() -> None:
    """Windows 终端默认常为 GBK，避免启动中文提示变成乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    _ensure_utf8_stdio()
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "1") not in ("0", "false", "False")
    test_diff = os.getenv("DISTRIBUTION_MODE", "").strip().lower() == "test_diff"
    if test_diff:
        print(f"新旧教材对比 → http://{host}:{port}/")
        print(f"本册工作页   → http://{host}:{port}/textbook-diff/workbook?subject=test")
    else:
        print(f"教材匹配系统 → http://{host}:{port}/")
        print(f"旧库建设     → http://{host}:{port}/old-library/")
    print(f"健康检查     → http://{host}:{port}/api/health")
    # 热重载会中断划分页码等长请求；debug 保留报错页，但不启用 reloader
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)
