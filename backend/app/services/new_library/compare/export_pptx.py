"""从 export.json 调用 Node PPTX 生成器。"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ....repo_paths import repo_root
from .export import build_export_payload, export_file_path, persist_compare_export


def pptx_output_path(new_lesson_uid: str) -> Path:
    safe = new_lesson_uid.replace("\\", "_").replace("/", "_")
    return repo_root() / "outputs" / f"课件_{safe}.pptx"


def _find_node() -> str | None:
    return shutil.which("node")


def generate_pptx(
    *,
    new_lesson_uid: str,
    lesson_match_id: str | None = None,
    mode: str = "student",
) -> Path:
    """生成 PPTX；需本机安装 Node.js 且 scripts/pptx 已 npm install。"""
    node = _find_node()
    if not node:
        raise RuntimeError("未找到 node，请安装 Node.js 后重试")

    persist_compare_export(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match_id
    )
    config_path = export_file_path(new_lesson_uid)
    if not config_path.is_file():
        raise RuntimeError("export.json 写入失败")

    script = repo_root() / "scripts" / "pptx" / "gen_ppt_v5.js"
    if not script.is_file():
        raise RuntimeError(f"缺少 PPTX 脚本：{script}")

    out_path = pptx_output_path(new_lesson_uid)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    assets_env = repo_root() / "scripts" / "pptx" / "assets"
    env = {
        **dict(__import__("os").environ),
        "PPTX_ASSETS_DIR": str(assets_env) if assets_env.is_dir() else "",
    }

    cmd = [
        node,
        str(script),
        "--config",
        str(config_path),
        "--out",
        str(out_path),
        "--mode",
        mode if mode in ("student", "teacher") else "student",
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(repo_root() / "scripts" / "pptx"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(err or "PPTX 生成失败")

    if not out_path.is_file():
        raise RuntimeError("PPTX 文件未生成")
    return out_path
