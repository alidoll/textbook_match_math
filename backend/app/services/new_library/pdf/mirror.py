"""新教材 PDF 镜像到 data/new-textbook/{版本}/。"""
from __future__ import annotations

from pathlib import Path

from ....repo_paths import new_textbook_mirror_path
from ...pdf_mirror_finalize import abort_pdf_mirror, finalize_pdf_mirror

__all__ = ["abort_pdf_mirror", "finalize_pdf_mirror", "stage_pdf_mirror"]


def stage_pdf_mirror(*, edition_label: str, volume_code: str, content: bytes) -> tuple[Path, Path]:
    final = new_textbook_mirror_path(edition_label, volume_code)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f"{final.name}.uploading")
    tmp.write_bytes(content)
    return tmp, final
