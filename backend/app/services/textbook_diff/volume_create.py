"""创建教材对比册次（复用 Volume 模型）。"""
from __future__ import annotations

from ...extensions import db
from ...models import Volume
from .sandbox_subjects import (
    SANDBOX_SUBJECT_LABELS,
    register_xiaoke_prefix_meta,
    sandbox_label_for_id,
)

# 册次码前缀 → (subject, edition)
# HXRJ/YWRJ/SXJJ/SWRJ/KXJR/TEST；XK* = 小科（小学科学各版本）
_DIFF_PREFIX_META: dict[str, tuple[str, str]] = {
    "HXRJ": ("化学", "人教版"),
    "YWRJ": ("语文", "人教版"),
    "SXJJ": ("数学", "冀教版"),
    "SWRJ": ("生物", "人教版"),
    "KXJR": ("科学", "冀人版"),
    "TEST": ("Test", "人教版"),
}
register_xiaoke_prefix_meta(_DIFF_PREFIX_META)

_SUBJECT_ID_TO_LABEL: dict[str, str] = {
    "huaxue": "化学",
    "yuwen": "语文",
    "shuxue": "数学",
    "shengwu": "生物",
    "kexue": "科学",
    "test": "Test",
    "xiaoke": "小科",
}


def subject_label_from_param(raw: str | None) -> str | None:
    """接受学科 id 或中文名；空则 None。"""
    key = (raw or "").strip()
    if not key:
        return None
    lower = key.lower()
    if lower in _SUBJECT_ID_TO_LABEL:
        return _SUBJECT_ID_TO_LABEL[lower]
    sand = sandbox_label_for_id(lower)
    if sand:
        return sand
    for label in list(_SUBJECT_ID_TO_LABEL.values()) + sorted(SANDBOX_SUBJECT_LABELS):
        if key == label:
            return label
    return None


def make_diff_volume_code(
    *,
    grade: int,
    term: str,
    book_type: str,
    prefix: str = "HXRJ",
    version: int | None = None,
) -> str:
    """{PREFIX}-{grade}{S|X}-{DOLD|DNEW} 或 …-DNEW-v{n}（n≥2）。"""
    term_key = "S" if term in ("上", "shang") else "X"
    pfx = (prefix or "HXRJ").strip().upper()
    if book_type == "diff_old":
        return f"{pfx}-{grade}{term_key}-DOLD"
    if book_type != "diff_new":
        raise ValueError("book_type 须为 diff_old 或 diff_new")
    ver = 1 if version is None else int(version)
    if ver < 1:
        raise ValueError("version 须 ≥ 1")
    if ver == 1:
        return f"{pfx}-{grade}{term_key}-DNEW"
    return f"{pfx}-{grade}{term_key}-DNEW-v{ver}"


def normalize_term(term: str) -> str:
    t = (term or "").strip()
    if t in ("上", "shang", "S"):
        return "上"
    if t in ("下", "xia", "X"):
        return "下"
    raise ValueError(f"无效学期：{term}")


def _prefix_meta(prefix: str) -> tuple[str, str]:
    pfx = (prefix or "").strip().upper()
    meta = _DIFF_PREFIX_META.get(pfx)
    if not meta:
        raise ValueError(f"不支持的教材对比册次前缀：{prefix}")
    return meta


def _display_title(subject: str, edition: str, grade: int, term: str) -> str:
    grade_labels = {9: "九年级", 10: "高一", 11: "高二", 12: "高三", 7: "七年级", 8: "八年级"}
    suffix = "上册" if term == "上" else "下册"
    return f"{edition}{subject}{grade_labels.get(grade, f'{grade}年级')}{suffix}"


def create_diff_volume(
    *,
    grade: int,
    term: str,
    book_type: str,
    prefix: str = "HXRJ",
    version: int | None = None,
    version_label: str | None = None,
) -> dict:
    if grade < 1 or grade > 12:
        raise ValueError("年级须为 1–12")
    if book_type not in ("diff_old", "diff_new"):
        raise ValueError("book_type 须为 diff_old 或 diff_new")

    from .volumes import diff_volume_detail

    subject, edition = _prefix_meta(prefix)
    term_key = normalize_term(term)
    volume_code = make_diff_volume_code(
        grade=grade,
        term=term_key,
        book_type=book_type,
        prefix=prefix,
        version=version,
    )

    existing = Volume.query.filter_by(volume_code=volume_code).first()
    if existing:
        return {"ok": True, "created": False, **diff_volume_detail(existing)}

    label = (version_label or "").strip() or None
    volume = Volume(
        volume_code=volume_code,
        subject=subject,
        edition=edition,
        grade=grade,
        semester=term_key,
        book_type=book_type,
        display_title=_display_title(subject, edition, grade, term_key),
        version_label=label,
        parse_status="pending",
    )
    db.session.add(volume)
    db.session.commit()
    return {"ok": True, "created": True, **diff_volume_detail(volume)}


def parse_diff_volume_code(volume_code: str) -> tuple[str, int, str, str, int | None]:
    """返回 (prefix, grade, term, book_type, version)。

    DOLD → version=None；无后缀 DNEW → 1；DNEW-vN → N。
    """
    code = (volume_code or "").strip()
    parts = code.split("-")
    if len(parts) not in (3, 4):
        raise ValueError(f"无效的教材对比册次代码：{code}")

    prefix = parts[0].strip().upper()
    if prefix not in _DIFF_PREFIX_META:
        raise ValueError(f"无效的教材对比册次代码：{code}")

    grade_term = parts[1]
    if len(grade_term) < 2:
        raise ValueError(f"无效的教材对比册次代码：{code}")
    grade_str = grade_term[:-1]
    term_char = grade_term[-1]
    bt_str = parts[2]

    try:
        grade = int(grade_str)
    except ValueError as exc:
        raise ValueError(f"无效的教材对比册次代码：{code}") from exc
    if term_char not in ("S", "X"):
        raise ValueError(f"无效的教材对比册次代码：{code}")
    term = "上" if term_char == "S" else "下"

    if bt_str == "DOLD":
        if len(parts) != 3:
            raise ValueError(f"无效的教材对比册次代码：{code}")
        return prefix, grade, term, "diff_old", None

    if bt_str != "DNEW":
        raise ValueError(f"无效的教材对比册次代码：{code}")

    if len(parts) == 3:
        return prefix, grade, term, "diff_new", 1

    ver_token = parts[3]
    if not ver_token.startswith("v") or not ver_token[1:].isdigit():
        raise ValueError(f"无效的教材对比册次代码：{code}")
    ver = int(ver_token[1:])
    if ver < 2:
        raise ValueError(f"无效的教材对比册次代码：{code}")
    return prefix, grade, term, "diff_new", ver


def next_dnew_version(*, prefix: str, grade: int, term: str) -> int:
    """同册次下一本 DNEW 版本号（已有无后缀视为 1）。"""
    term_key = normalize_term(term)
    pfx = (prefix or "").strip().upper()
    base = make_diff_volume_code(
        grade=grade, term=term_key, book_type="diff_new", prefix=pfx, version=1
    )
    like_prefix = f"{base}-v%"
    codes: set[str] = set()
    if Volume.query.filter_by(volume_code=base).first():
        codes.add(base)
    for row in Volume.query.filter(Volume.volume_code.like(like_prefix)).all():
        codes.add(row.volume_code)

    max_ver = 0
    for code in codes:
        try:
            _, _, _, bt, ver = parse_diff_volume_code(code)
        except ValueError:
            continue
        if bt != "diff_new":
            continue
        max_ver = max(max_ver, int(ver or 0))
    return max_ver + 1 if max_ver >= 1 else 1


def ensure_diff_volume_by_code(volume_code: str) -> dict:
    prefix, grade, term, book_type, version = parse_diff_volume_code(volume_code)
    return create_diff_volume(
        grade=grade,
        term=term,
        book_type=book_type,
        prefix=prefix,
        version=version,
    )
