"""仓库根入口（Flask debug 热重载会 chdir 到这里后执行 python run.py）。"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_RUN = ROOT / "backend" / "run.py"


def _require_venv() -> None:
    """避免系统 Python 抢占 5000，导致改代码后页面仍跑旧逻辑。"""
    if os.getenv("ALLOW_SYSTEM_PYTHON", "").strip() in ("1", "true", "True"):
        return
    venv_dir = (ROOT / ".venv").resolve()
    if not venv_dir.is_dir():
        return
    exe = Path(sys.executable).resolve()
    try:
        exe.relative_to(venv_dir)
        return
    except ValueError:
        pass
    tip = venv_dir / "Scripts" / "python.exe"
    if not tip.is_file():
        tip = venv_dir / "bin" / "python"
    print("错误：请用项目虚拟环境启动，否则改动不会进当前服务：")
    print(f"  当前解释器: {exe}")
    print(f"  请执行:     {tip} run.py")
    print("  （临时放行可设 ALLOW_SYSTEM_PYTHON=1）")
    raise SystemExit(2)


os.chdir(ROOT)
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

if __name__ == "__main__":
    _require_venv()
    runpy.run_path(str(BACKEND_RUN), run_name="__main__")
