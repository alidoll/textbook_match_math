"""旧教材 PDF 镜像到 data/old-textbook/{版本}/。"""
from __future__ import annotations

from pathlib import Path

from ....repo_paths import old_textbook_mirror_path
from ...pdf_mirror_finalize import abort_pdf_mirror, finalize_pdf_mirror

__all__ = ["abort_pdf_mirror", "finalize_pdf_mirror", "stage_pdf_mirror"]


def stage_pdf_mirror(*, edition_label: str, volume_code: str, content: bytes) -> tuple[Path, Path]:
    """
    先写入临时文件，DB 提交成功后再 replace 到正式路径。
    返回 (临时路径, 正式路径)。
    """
    final = old_textbook_mirror_path(edition_label, volume_code)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{final.name}.uploading")
    tmp.write_bytes(content)
    return tmp, final
