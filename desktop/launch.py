"""桌面应用启动器：后台线程跑 Flask，前台 PyWebView 打开原生窗口。"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

# 开发模式：把 backend/ 加入 sys.path
if not getattr(sys, "frozen", False):
    BACKEND = Path(__file__).resolve().parent.parent / "backend"
    sys.path.insert(0, str(BACKEND))

# 打包模式：PyInstaller 把 backend/app 打包到 _MEIPASS，需要加到 sys.path
if getattr(sys, "frozen", False):
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        sys.path.insert(0, str(Path(meipass) / "backend"))

os.chdir(Path(__file__).resolve().parent.parent if not getattr(sys, "frozen", False) else Path(sys.executable).resolve().parent)

from app import create_app  # noqa: E402  — backend/app/__init__.py


def _find_free_port(default: int = 5001) -> int:
    """优先用默认端口；被占用则找空闲端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", default))
            return default
        except OSError:
            pass
    # 默认端口被占用，找空闲端口
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 15) -> bool:
    """等待 Flask 服务就绪。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _ensure_env() -> None:
    """首次运行时在 exe 同级目录生成默认 .env（如不存在）。"""
    from app.repo_paths import app_root

    env_path = app_root() / ".env"
    if env_path.exists():
        return
    env_path.write_text(
        """# 教材匹配系统配置（首次运行自动生成）

# 本地 SQLite（无需 MySQL）
FLASK_DEBUG=0
FLASK_HOST=127.0.0.1
FLASK_PORT=5001
SECRET_KEY=dev-change-me
STATIC_ASSET_VERSION=371

# 整册 PDF 上传上限（MB）
MAX_UPLOAD_MB=2048

# AI 服务鉴权（在首页填写你的 API Key，保存后自动写入下方）
LLM_ENABLED=1
TAL_MLOPS_APP_ID=
TAL_MLOPS_APP_KEY=
LLM_AUTH_STYLE=api-key
LLM_BASE_URL=http://ai-service.tal.com/openai-compatible/v1
LLM_MODEL=glm-5.2
LLM_VISION_MODEL=doubao-seed-2.0-pro
LLM_REASONING_MODE=enabled
LLM_REASONING_EFFORT=low

# OCR 与建块
ATOM_OCR_TWO_PHASE=1
ATOM_OCR_REMOVE_STAMP=1
ATOM_LAYOUT_LLM=1
ATOM_CURATE_HEURISTICS=minimal
NEW_BLOCK_MIRROR_LLM=1
DUAL_TRACK_LLM=1
DUAL_TRACK_SLIDE_OCR=1
DUAL_TRACK_TEXTBOOK_TEXT_OCR=doubao

LLM_TEMPERATURE=0.2
LLM_TIMEOUT=120

LLM_COURSE_MATCH_MODE=batch
LLM_COURSE_MATCH_ENABLED=1
LLM_COURSE_MATCH_MODEL=glm-5.2
LLM_COURSE_MATCH_BATCH_TIMEOUT=120
LLM_COURSE_MATCH_TIMEOUT=45
LLM_COURSE_MATCH_WORKERS=4

LIB_SEMANTIC_BLOCK=1
LIB_SEMANTIC_CROSS_PAGE=1
LIB_FIGURE_PART_ROLE_LLM=1
LIB_TABLE_GRID_LLM=1
LIB_PAGE_OCR_WORKERS=3
LIB_SEMANTIC_BLOCK_WORKERS=3
LIB_OCR_AUDIT_IMAGE_PARALLEL=1
LIB_VOLUME_OCR_PIPELINE_BLOCKS=1
LIB_SEMANTIC_BLOCK_FALLBACK=error
PAGE_TEXT_MATH_AUDIT=1
""",
        encoding="utf-8-sig",
    )


def main() -> None:
    import webview

    _ensure_env()

    port = _find_free_port()
    app = create_app()

    def _run_flask():
        app.run(
            host="127.0.0.1",
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )

    # daemon 线程：窗口关闭后主线程退出，Flask 随之终止
    t = threading.Thread(target=_run_flask, daemon=True)
    t.start()

    if not _wait_for_server(port):
        print("Flask 启动超时，请检查环境配置")
        sys.exit(1)

    window = webview.create_window(
        "教材匹配系统",
        f"http://127.0.0.1:{port}/",
        width=1280,
        height=860,
        min_size=(900, 600),
    )
    webview.start()
    # 窗口关闭后退出
    os._exit(0)


if __name__ == "__main__":
    main()
