"""PDF 镜像落盘：Windows 下目标文件被占用时尽量覆盖写入。"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

_log = logging.getLogger(__name__)


def _clear_readonly(path: Path) -> None:
    if not path.is_file():
        return
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def finalize_pdf_mirror(tmp: Path, final: Path) -> Path:
    """临时 .uploading → 正式路径；失败时尝试删除/覆盖并给出可读错误。"""
    if not tmp.exists():
        return final

    _clear_readonly(final)
    for attempt, action in enumerate(("replace", "unlink_replace", "overwrite")):
        try:
            if action == "replace":
                os.replace(tmp, final)
            elif action == "unlink_replace":
                if final.exists():
                    _clear_readonly(final)
                    final.unlink()
                os.replace(tmp, final)
            else:
                data = tmp.read_bytes()
                if final.exists():
                    _clear_readonly(final)
                with open(final, "wb") as fh:
                    fh.write(data)
                tmp.unlink(missing_ok=True)
                if attempt > 0:
                    _log.warning("PDF 镜像已覆盖写入（非原子）: %s", final)
            return final
        except OSError as exc:
            if attempt == 2:
                _log.error("PDF 镜像写入失败 %s: %s", final, exc)
                raise PermissionError(
                    f"无法写入镜像 {final.name}：文件可能被 PDF 阅读器、WPS 或后台解析进程占用，"
                    "请关闭相关程序后重试"
                ) from exc
            _log.debug("PDF 镜像 %s 第 %d 次尝试失败: %s", final.name, attempt + 1, exc)

    return final


def abort_pdf_mirror(tmp: Path) -> None:
    try:
        tmp.unlink(missing_ok=True)
    except OSError as exc:
        _log.warning("删除临时 PDF 失败 %s: %s", tmp, exc)
