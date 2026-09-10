"""从新课标基准 xlsx 读取课时行。"""
from __future__ import annotations

from ....parsers.benchmark_xlsx import BenchmarkLessonRow, iter_sheet_rows, merge_misgraded_lesson_rows
from ....repo_paths import benchmark_new_xlsx_path
from ...lesson_filters import filter_benchmark_rows, filter_master_class_benchmark_rows
from ...old_library.edition_registry import EditionDef, get_edition


def load_volume_lesson_rows(
    *,
    edition_id: str,
    grade: int,
    term: str,
    xlsx_path=None,
) -> tuple[EditionDef, list[BenchmarkLessonRow]]:
    edition = get_edition(edition_id)
    path = xlsx_path or benchmark_new_xlsx_path()
    if not path.is_file():
        raise FileNotFoundError(f"新课标基准库不存在：{path}")

    sheet = edition.sheet_for_book_type("new")
    term_key = "下" if "下" in str(term) else "上"
    all_term: list[BenchmarkLessonRow] = []
    for row in iter_sheet_rows(path, sheet):
        if row.semester == term_key:
            all_term.append(row)
    primary = [r for r in all_term if r.grade == grade]
    rows = merge_misgraded_lesson_rows(primary, all_term, grade=grade)
    rows = filter_benchmark_rows(rows)
    rows = filter_master_class_benchmark_rows(rows)
    return edition, rows
