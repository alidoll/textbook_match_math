"""从全局基准 xlsx 读取指定版本/年级/学期课时行。"""
from __future__ import annotations

from ....parsers.benchmark_xlsx import BenchmarkLessonRow, iter_sheet_rows
from ....repo_paths import benchmark_old_xlsx_path
from ..edition_registry import EditionDef, get_edition


def load_volume_lesson_rows(
    *,
    edition_id: str,
    grade: int,
    term: str,
    xlsx_path=None,
) -> tuple[EditionDef, list[BenchmarkLessonRow]]:
    edition = get_edition(edition_id)
    path = xlsx_path or benchmark_old_xlsx_path()
    if not path.is_file():
        raise FileNotFoundError(f"基准库文件不存在：{path}")
    if not edition.has_old_benchmark or not (edition.benchmark_sheet or "").strip():
        raise FileNotFoundError(f"旧课标基准库暂无「{edition.label}」工作表，请从新教材侧上传 PDF 导入")

    term_key = "下" if "下" in str(term) else "上"
    rows: list[BenchmarkLessonRow] = []
    for row in iter_sheet_rows(path, edition.benchmark_sheet):
        if row.grade == grade and row.semester == term_key:
            rows.append(row)
    return edition, rows
