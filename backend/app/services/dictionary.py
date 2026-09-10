"""码表查询。"""
from __future__ import annotations

from ..models import DictionaryEntry

_DEFAULT_BLOCK_STAGE: list[dict[str, str]] = [
    {"code": "cw_title", "label": "课件名称页"},
    {"code": "intro", "label": "导入"},
    {"code": "knowledge", "label": "知识点讲解"},
    {"code": "lab_activity", "label": "实验/活动操作"},
    {"code": "example", "label": "典例剖析"},
    {"code": "summary", "label": "知识小结"},
    {"code": "cw_footer", "label": "课件尾页（练习提醒）"},
]

_DEFAULT_BLOCK_STAGE_NEW: list[dict[str, str]] = [
    {"code": "cover_title", "label": "封面标题"},
    {"code": "problem_context", "label": "问题情境"},
    {"code": "science_inquiry", "label": "科学探究"},
    {"code": "guide_mailbox", "label": "指南车信箱"},
    {"code": "safety_warning", "label": "安全警示"},
    {"code": "core_concept", "label": "核心概念"},
    {"code": "extension_activity", "label": "拓展活动"},
    {"code": "summary", "label": "归纳总结"},
    {"code": "thinking_method", "label": "思想方法-归纳法"},
    {"code": "extension_transfer", "label": "拓展迁移"},
    {"code": "reflection", "label": "反思评价"},
    {"code": "rubric", "label": "评价量规"},
]

_DEFAULT_CHANGE_TYPE: list[dict[str, str]] = [
    {"code": "L001", "label": "素养类新增"},
    {"code": "L002", "label": "名称/措辞变化"},
    {"code": "L003", "label": "结构重组"},
    {"code": "L004", "label": "活动流程调整"},
    {"code": "L005", "label": "实验器材变化"},
    {"code": "L006", "label": "图示/素材替换"},
    {"code": "L007", "label": "内容删除"},
    {"code": "L008", "label": "内容新增"},
    {"code": "L009", "label": "其他"},
]

_DEFAULT_REUSE_ACTION: list[dict[str, str]] = [
    {"code": "reuse_as_is", "label": "直接沿用"},
    {"code": "optimize", "label": "优化调整"},
    {"code": "reference", "label": "参考素材重制"},
    {"code": "new_build", "label": "全新制作"},
    {"code": "remove", "label": "完全删除"},
]

_CATEGORY_DEFAULTS: dict[str, list[dict[str, str]]] = {
    "block_stage": _DEFAULT_BLOCK_STAGE,
    "block_stage_new": _DEFAULT_BLOCK_STAGE_NEW,
    "change_type": _DEFAULT_CHANGE_TYPE,
    "reuse_action": _DEFAULT_REUSE_ACTION,
}


def list_dictionary_entries(*, category: str) -> list[dict[str, str]]:
    rows = (
        DictionaryEntry.query.filter_by(category=category)
        .order_by(DictionaryEntry.sort_order, DictionaryEntry.code)
        .all()
    )
    if not rows and category in _CATEGORY_DEFAULTS:
        return list(_CATEGORY_DEFAULTS[category])
    return [{"code": r.code, "label": r.label} for r in rows]
