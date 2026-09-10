"""沙箱学科：Test / 小科 —— UX 与本地 JSON 落盘相同，册次前缀相互隔离。"""
from __future__ import annotations

from typing import Any

from ..old_library.edition_registry import EditionDef, list_active_editions

_TEST_PREFIX = "TEST"
_LEGACY_XIAOK_PREFIX = "XKRJ"  # 早期单版本遗留


def xiaoke_diff_prefix(edition: EditionDef) -> str:
    """小科 diff 册次前缀，与旧库/新库 DX/YJ… 及学科「科学」KXJR 隔离。"""
    base = (edition.volume_code_prefix or edition.edition_id.upper()[:8]).strip().upper()
    return f"XK{base}"


def list_xiaoke_editions() -> list[EditionDef]:
    """小学科学版本（对齐智慧教育平台列表；含无旧库基准的五四沪科技版）。"""
    return [
        e
        for e in list_active_editions()
        if (e.subject or "").strip() == "科学"
    ]


def _xiaoke_prefixes() -> set[str]:
    prefs = {xiaoke_diff_prefix(e) for e in list_xiaoke_editions()}
    prefs.add(_LEGACY_XIAOK_PREFIX)
    return prefs


def _all_sandbox_prefixes() -> frozenset[str]:
    return frozenset({_TEST_PREFIX, *_xiaoke_prefixes()})


SANDBOX_SUBJECT_LABELS: frozenset[str] = frozenset({"Test", "小科"})
SANDBOX_SUBJECT_IDS: frozenset[str] = frozenset({"test", "xiaoke"})


def sandbox_label_for_id(subject_id: str | None) -> str | None:
    key = (subject_id or "").strip().lower()
    if key == "test":
        return "Test"
    if key == "xiaoke":
        return "小科"
    return None


def sandbox_id_for_label(label: str | None) -> str | None:
    name = (label or "").strip()
    if name == "Test":
        return "test"
    if name == "小科":
        return "xiaoke"
    return None


def is_sandbox_subject_label(label: str | None) -> bool:
    return (label or "").strip() in SANDBOX_SUBJECT_LABELS


def is_sandbox_prefix(prefix: str | None) -> bool:
    return (prefix or "").strip().upper() in _all_sandbox_prefixes()


def prefix_for_sandbox_label(label: str | None) -> str | None:
    sid = sandbox_id_for_label(label)
    if sid == "test":
        return _TEST_PREFIX
    if sid == "xiaoke":
        return _LEGACY_XIAOK_PREFIX
    return None


def label_for_sandbox_prefix(prefix: str | None) -> str | None:
    pfx = (prefix or "").strip().upper()
    if pfx == _TEST_PREFIX:
        return "Test"
    if pfx in _xiaoke_prefixes():
        return "小科"
    return None


def volume_code_prefix(volume_code: str | None) -> str:
    raw = str(volume_code or "").strip().upper()
    if "-" not in raw:
        return raw
    return raw.split("-", 1)[0]


def is_sandbox_volume_code(volume_code: str | None) -> bool:
    return volume_code_prefix(volume_code) in _all_sandbox_prefixes()


def require_sandbox_pair_codes(old_code: str, new_code: str) -> str:
    op = volume_code_prefix(old_code)
    np = volume_code_prefix(new_code)
    prefs = _all_sandbox_prefixes()
    if op not in prefs or np not in prefs:
        raise ValueError("仅允许 Test(TEST-*) / 小科(XK*-*) 册次对")
    if op != np:
        raise ValueError(f"新旧册次前缀不一致（{op} vs {np}），Test 与小科数据相互隔离")
    return op


def register_xiaoke_prefix_meta(prefix_meta: dict[str, tuple[str, str]]) -> None:
    for ed in list_xiaoke_editions():
        prefix_meta[xiaoke_diff_prefix(ed)] = ("小科", ed.label)
    prefix_meta.setdefault(_LEGACY_XIAOK_PREFIX, ("小科", "人教版"))


def sandbox_meta_rows() -> list[dict[str, Any]]:
    return [
        {"id": "test", "label": "Test", "prefixes": [_TEST_PREFIX]},
        {"id": "xiaoke", "label": "小科", "prefixes": sorted(_xiaoke_prefixes())},
    ]
