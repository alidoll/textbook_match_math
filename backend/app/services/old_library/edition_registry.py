"""教材版本注册表（数学 · 对齐国家中小学智慧教育平台版本列表）。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EditionDef:
    edition_id: str
    label: str
    benchmark_sheet: str
    subject: str = "科学"
    status: str = "active"
    volume_code_prefix: str = ""
    new_benchmark_sheet: str | None = None
    school_system: str = "63"  # 63=六三学制  54=五·四学制
    grade_min: int = 1
    grade_max: int = 6
    has_old_benchmark: bool = True

    @property
    def code_prefix(self) -> str:
        return self.volume_code_prefix or self.edition_id.upper()[:8]

    @property
    def school_system_label(self) -> str:
        return SCHOOL_SYSTEMS.get(self.school_system, self.school_system)

    def sheet_for_book_type(self, book_type: str) -> str:
        if book_type == "new" and self.new_benchmark_sheet:
            return self.new_benchmark_sheet
        return self.benchmark_sheet


SCHOOL_SYSTEMS: dict[str, str] = {
    "63": "六三学制",
    "54": "五·四学制",
}

GRADE_LABELS: dict[int, str] = {
    1: "一年级",
    2: "二年级",
    3: "三年级",
    4: "四年级",
    5: "五年级",
    6: "六年级",
    7: "七年级",
    8: "八年级",
    9: "九年级",
    10: "高一",
    11: "高二",
    12: "高三",
}

# 小学科学 · 六三学制（1–6 年级）+ 五·四学制（1–5 年级）
# benchmark_sheet / new_benchmark_sheet 与 data/base_data/*.xlsx 工作表名一致
EDITIONS: dict[str, EditionDef] = {
    "shuxue_jijiao": EditionDef(
        edition_id="shuxue_jijiao",
        label="冀教版",
        subject="数学",
        benchmark_sheet="",
        new_benchmark_sheet=None,
        volume_code_prefix="SXJJ",
        school_system="63",
        grade_min=7,
        grade_max=9,
        has_old_benchmark=False,
    ),
}

# 用户口语别名 → edition_id
EDITION_ALIASES: dict[str, str] = {
    "冀教版": "shuxue_jijiao",
}


def resolve_edition_id(raw: str) -> str | None:
    key = (raw or "").strip()
    if key in EDITIONS:
        return key
    return EDITION_ALIASES.get(key)


def get_edition(edition_id: str) -> EditionDef:
    eid = resolve_edition_id(edition_id) or edition_id
    if eid not in EDITIONS:
        raise KeyError(f"未注册的版本：{edition_id}")
    return EDITIONS[eid]


def list_active_editions(*, school_system: str | None = None) -> list[EditionDef]:
    rows = [e for e in EDITIONS.values() if e.status == "active"]
    if school_system:
        rows = [e for e in rows if e.school_system == school_system]
    return sorted(rows, key=lambda e: (_edition_sort_key(e), e.label, e.edition_id))


def _edition_sort_key(ed: EditionDef) -> tuple:
    """六三学制版本按平台展示顺序，五四放后。"""
    order_63 = [
        "renejiao",
        "jiren",
        "daxiang",
        "jiaoke",
        "xiangke",
        "yuejiao",
        "sujiao",
        "qingdao_63",
    ]
    order_54 = ["hukexue_54", "qingdao_54"]
    if ed.school_system == "54":
        try:
            return (1, order_54.index(ed.edition_id))
        except ValueError:
            return (1, 99)
    try:
        return (0, order_63.index(ed.edition_id))
    except ValueError:
        return (0, 99)


def grade_term_pairs_for_edition(ed: EditionDef) -> list[tuple[int, str]]:
    return [
        (grade, term)
        for grade in range(ed.grade_min, ed.grade_max + 1)
        for term in ("上", "下")
    ]


def standard_volumes_for_edition(edition_id: str) -> list[dict]:
    """小学年级 × 上下册，供工作台展示。"""
    ed = get_edition(edition_id)
    volumes = []
    for grade, term in grade_term_pairs_for_edition(ed):
        suffix = "上册" if term == "上" else "下册"
        volumes.append(
            {
                "edition_id": ed.edition_id,
                "edition_label": ed.label,
                "school_system": ed.school_system,
                "school_system_label": ed.school_system_label,
                "grade": grade,
                "grade_label": GRADE_LABELS[grade],
                "term": term,
                "volume_label": f"{GRADE_LABELS[grade]}{suffix}",
            }
        )
    return volumes


def edition_to_api_dict(ed: EditionDef) -> dict:
    return {
        "edition_id": ed.edition_id,
        "label": ed.label,
        "status": ed.status,
        "school_system": ed.school_system,
        "school_system_label": ed.school_system_label,
        "grade_min": ed.grade_min,
        "grade_max": ed.grade_max,
        "has_old_benchmark": ed.has_old_benchmark,
    }
