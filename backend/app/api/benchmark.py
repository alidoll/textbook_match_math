from __future__ import annotations

from flask import jsonify

from ..repo_paths import (
    BASE_DATA_DIR,
    DEFAULT_BENCHMARK_NEW_XLSX,
    DEFAULT_BENCHMARK_OLD_XLSX,
    benchmark_new_xlsx_path,
    benchmark_old_xlsx_path,
    repo_root,
)
from . import api_bp


def _rel(path) -> str:
    root = repo_root()
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


@api_bp.get("/benchmark/status")
def benchmark_status():
    """检查全局基准库文件是否就位（路径相对仓库根）。"""
    old_path = benchmark_old_xlsx_path()
    new_path = benchmark_new_xlsx_path()
    return jsonify(
        {
            "ok": True,
            "repo_root": _rel(repo_root()),
            "base_data_dir": str(BASE_DATA_DIR).replace("\\", "/"),
            "old": {
                "configured": _rel(old_path),
                "default": str(DEFAULT_BENCHMARK_OLD_XLSX).replace("\\", "/"),
                "exists": old_path.is_file(),
            },
            "new": {
                "configured": _rel(new_path),
                "default": str(DEFAULT_BENCHMARK_NEW_XLSX).replace("\\", "/"),
                "exists": new_path.is_file(),
            },
        }
    )
