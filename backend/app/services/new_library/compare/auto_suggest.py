"""compare AI 建议草稿（规则引擎，对齐旧项目 Excel 的 AI 列）。"""
from __future__ import annotations

import logging
from datetime import datetime

from rapidfuzz import fuzz

from ....extensions import db
from ....models import Block, BlockMatch, TextbookAtom
from .resolve import get_lesson_match_for_new_lesson
from .block_judge import judge_block_pair
from .workspace import _block_payload, build_compare_workspace

CHANGE_TO_REUSE: dict[str, str] = {
    "L001": "reference",
    "L002": "optimize",
    "L003": "reference",
    "L004": "reference",
    "L005": "reference",
    "L006": "optimize",
    "L007": "remove",
    "L008": "new_build",
    "L009": "new_build",
}

_LITERACY_KEYWORDS = ("反思", "评价", "量规", "归纳法", "思想方法", "素养", "信箱")
_ACTIVITY_KEYWORDS = ("实验", "探究", "活动", "操作", "游戏")
_MEDIA_KEYWORDS = ("图", "插图", "照片", "素材", "示意图")
_EQUIP_KEYWORDS = ("器材", "材料", "工具", "仪器")
_FIXED_CW_PAGE_KEYWORDS = ("课件例题", "课件尾页")

logger = logging.getLogger(__name__)


def is_fixed_cw_page(block: dict | None) -> bool:
    """课件固定页：例题页、尾页等，通常无新教材块，旧课件直接沿用。"""
    if not block:
        return False
    name = (block.get("block_name") or "").strip()
    return any(k in name for k in _FIXED_CW_PAGE_KEYWORDS)


def format_cw_range(cw_pgs: list[int] | None) -> str:
    nums = sorted(int(x) for x in (cw_pgs or []) if str(x).strip().isdigit())
    if not nums:
        return ""
    if len(nums) == 1:
        return f"P{nums[0]}"
    consecutive = all(nums[i] == nums[i - 1] + 1 for i in range(1, len(nums)))
    if consecutive:
        return f"P{nums[0]}-{nums[-1]}"
    return "P" + "/P".join(str(n) for n in nums)


def block_body_text(block: dict | None, atoms_by_code: dict) -> str:
    if not block:
        return ""
    parts: list[str] = []
    for code in block.get("atom_codes") or []:
        atom = atoms_by_code.get(str(code).strip())
        if not atom:
            continue
        if isinstance(atom, dict):
            text = (atom.get("content") or atom.get("ocr_text") or "").strip()
        else:
            text = (atom.content or atom.ocr_text or "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def guess_change_type(
    old_block: dict | None,
    new_block: dict | None,
    old_text: str,
    new_text: str,
) -> str:
    if not old_block and new_block:
        combined = (new_text + (new_block.get("block_name") or "")).strip()
        if any(k in combined for k in _LITERACY_KEYWORDS):
            return "L001"
        return "L008"
    if old_block and not new_block:
        if is_fixed_cw_page(old_block):
            return "L002"
        return "L007"

    old_name = (old_block or {}).get("block_name") or ""
    new_name = (new_block or {}).get("block_name") or ""
    combined = f"{old_name} {new_name} {old_text} {new_text}"

    name_ratio = fuzz.ratio(old_name, new_name) if old_name and new_name else 0
    text_ratio = fuzz.token_set_ratio(old_text, new_text) if old_text and new_text else 0

    if name_ratio >= 88 and text_ratio >= 78:
        return "L002"
    if any(k in combined for k in _EQUIP_KEYWORDS):
        return "L005"
    if any(k in combined for k in _MEDIA_KEYWORDS):
        return "L006"
    if any(k in combined for k in _ACTIVITY_KEYWORDS) and text_ratio < 62:
        return "L004"
    if any(k in combined for k in _LITERACY_KEYWORDS) and text_ratio < 55:
        return "L001"
    old_pgs = (old_block or {}).get("old_tb_pgs") or []
    new_pgs = (new_block or {}).get("new_tb_pgs") or []
    if old_pgs and new_pgs and old_pgs != new_pgs and text_ratio < 70:
        return "L003"
    if text_ratio >= 72:
        return "L002"
    if text_ratio >= 42:
        return "L004"
    return "L009"


def guess_reuse_action(
    change_type: str,
    old_block: dict | None,
    new_block: dict | None,
    old_text: str,
    new_text: str,
) -> str:
    if not old_block and new_block:
        return "new_build"
    if old_block and not new_block:
        if is_fixed_cw_page(old_block):
            return "reuse_as_is"
        return "remove"
    if change_type == "L007":
        return "remove"
    if change_type in ("L008", "L001"):
        return "new_build"

    text_ratio = fuzz.token_set_ratio(old_text, new_text) if old_text and new_text else 0
    has_cw = bool((old_block or {}).get("cw_pgs"))

    if change_type in ("L002", "L003", "L006"):
        if text_ratio >= 84 and has_cw:
            return "reuse_as_is"
        return "optimize"
    if change_type == "L004":
        return "reference"
    if text_ratio >= 86 and has_cw:
        return "reuse_as_is"
    return CHANGE_TO_REUSE.get(change_type, "optimize")


def draft_teacher_note(
    *,
    old_block: dict | None,
    new_block: dict | None,
    old_text: str,
    new_text: str,
    reuse_action: str,
    change_type: str,
) -> str:
    del change_type  # reserved for future finer hints
    parts: list[str] = []

    cw = format_cw_range((old_block or {}).get("cw_pgs"))
    if cw:
        parts.append(f"旧课件{cw}")

    if old_block and new_block:
        on = old_block.get("block_name") or ""
        nn = new_block.get("block_name") or ""
        nb = new_block.get("block_id") or ""
        if on and nn and on != nn:
            parts.append(f"「{on}」→「{nn}」")
        if nb:
            parts.append(f"→新区块 {nb}")
        old_pgs = old_block.get("old_tb_pgs") or []
        new_pgs = new_block.get("new_tb_pgs") or []
        if old_pgs and new_pgs and old_pgs != new_pgs:
            parts.append(
                f"教材页旧P{'/'.join(str(p) for p in old_pgs)}"
                f"→新P{'/'.join(str(p) for p in new_pgs)}"
            )
    elif new_block and not old_block:
        parts.append(
            f"新教材新增「{new_block.get('block_name') or ''}」"
            f"（{new_block.get('block_id') or ''}）"
        )
        parts.append("旧教材无对应块")
    elif old_block and not new_block:
        if is_fixed_cw_page(old_block):
            label = "课件例题" if "课件例题" in (old_block.get("block_name") or "") else "课件尾页"
            parts.append(f"课件固定{label}，无新教材对应块")
            parts.append("旧课件该页可直接沿用")
        else:
            parts.append(f"旧块「{old_block.get('block_name') or ''}」在新教材已删除")

    if old_text and new_text:
        ratio = fuzz.token_set_ratio(old_text, new_text)
        if ratio >= 80:
            parts.append("核心内容基本一致")
        elif ratio >= 50:
            hint_old = old_text.replace("\n", " ")[:36]
            hint_new = new_text.replace("\n", " ")[:36]
            if hint_old != hint_new:
                parts.append(f"内容有调整（旧：{hint_old}…→新：{hint_new}…）")
        else:
            parts.append("内容差异较大，需按新教材重制")
    elif new_text and not old_text:
        parts.append("旧侧无原子文本，以新教材为准")
    elif old_text and not new_text:
        parts.append("新侧无原子文本，需核对建块")

    reuse_hints = {
        "reuse_as_is": "旧课件可直接沿用",
        "optimize": "可参考旧课件，更新文字/排版/页码",
        "reference": "可参考旧课件素材，按新教材结构重制",
        "new_build": "需全新制作",
        "remove": "新教材已无此模块，课件可跳过",
    }
    hint = reuse_hints.get(reuse_action)
    if hint:
        parts.append(hint)

    note = "；".join(p for p in parts if p)
    return note or "请补充：旧课件哪页→新教材哪块→怎么改"


def suggest_group_rules(
    *,
    new_block: dict,
    old_blocks: list[dict],
    old_atoms_by_code: dict,
    new_atoms_by_code: dict,
) -> dict:
    """1 新区块 ↔ N 连续旧块：合并旧侧文本后整组判定。"""
    olds = [ob for ob in old_blocks if ob]
    if not olds:
        return suggest_pair_rules(
            None, new_block,
            old_atoms_by_code=old_atoms_by_code,
            new_atoms_by_code=new_atoms_by_code,
        )
    if len(olds) == 1:
        return suggest_pair_rules(
            olds[0], new_block,
            old_atoms_by_code=old_atoms_by_code,
            new_atoms_by_code=new_atoms_by_code,
        )

    combined_old = " ".join(
        block_body_text(ob, old_atoms_by_code) for ob in olds
    ).strip()
    new_text = block_body_text(new_block, new_atoms_by_code)
    primary = olds[0]
    change_type = guess_change_type(primary, new_block, combined_old, new_text)
    reuse_action = guess_reuse_action(
        change_type, primary, new_block, combined_old, new_text
    )
    teacher_note = draft_teacher_note(
        old_block=primary,
        new_block=new_block,
        old_text=combined_old,
        new_text=new_text,
        reuse_action=reuse_action,
        change_type=change_type,
    )
    old_ids = [str(ob.get("block_id") or "") for ob in olds if ob.get("block_id")]
    cw_merged = "、".join(
        p for p in (format_cw_range(ob.get("cw_pgs")) for ob in olds) if p
    )
    prefix = f"有序多锚定：{'+'.join(old_ids)}"
    if cw_merged:
        prefix = f"{prefix}（{cw_merged}）"
    teacher_note = f"{prefix}；{teacher_note}"

    judged = judge_block_pair(
        primary,
        new_block,
        old_text=combined_old,
        new_text=new_text,
        reuse_action=reuse_action,
        change_type=change_type,
        teacher_note=teacher_note,
    )
    points = list(judged.get("change_points") or [])
    for ob in olds[1:]:
        bid = ob.get("block_id") or ""
        name = (ob.get("block_name") or "").strip()
        cw = format_cw_range(ob.get("cw_pgs"))
        seg = f"辅匹配 {bid}"
        if name:
            seg += f"「{name}」"
        if cw:
            seg += f" {cw}"
        points.append(f"沿用{seg}素材，合并进同一新块")
    judged["change_points"] = points
    judged["anchor_group"] = {
        "new_block_id": new_block.get("block_id"),
        "old_block_ids": old_ids,
        "match_mode": "ordered_multi",
    }
    return judged | {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": "rules",
    }


def suggest_pair_rules(
    old_block: dict | None,
    new_block: dict | None,
    *,
    old_atoms_by_code: dict,
    new_atoms_by_code: dict,
) -> dict:
    old_text = block_body_text(old_block, old_atoms_by_code)
    new_text = block_body_text(new_block, new_atoms_by_code)
    change_type = guess_change_type(old_block, new_block, old_text, new_text)
    reuse_action = guess_reuse_action(
        change_type, old_block, new_block, old_text, new_text
    )
    teacher_note = draft_teacher_note(
        old_block=old_block,
        new_block=new_block,
        old_text=old_text,
        new_text=new_text,
        reuse_action=reuse_action,
        change_type=change_type,
    )
    return judge_block_pair(
        old_block,
        new_block,
        old_text=old_text,
        new_text=new_text,
        reuse_action=reuse_action,
        change_type=change_type,
        teacher_note=teacher_note,
    ) | {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": "rules",
    }


def suggest_pair(
    old_block: dict | None,
    new_block: dict | None,
    *,
    old_atoms_by_code: dict,
    new_atoms_by_code: dict,
    use_llm: bool = False,
) -> dict:
    old_text = block_body_text(old_block, old_atoms_by_code)
    new_text = block_body_text(new_block, new_atoms_by_code)
    if use_llm:
        from ....services.llm.compare_suggest import suggest_block_pair_with_llm

        try:
            llm_result = suggest_block_pair_with_llm(
                old_block,
                new_block,
                old_text=old_text,
                new_text=new_text,
            )
            judged = judge_block_pair(
                old_block,
                new_block,
                old_text=old_text,
                new_text=new_text,
                reuse_action=llm_result.get("reuse_action") or "optimize",
                change_type=llm_result.get("change_type") or "L009",
                teacher_note=llm_result.get("teacher_note") or "",
            )
            return judged | {
                k: v for k, v in llm_result.items()
                if k not in judged
            }
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            logger.warning("LLM suggest failed, fallback to rules: %s", err)
            result = suggest_pair_rules(
                old_block,
                new_block,
                old_atoms_by_code=old_atoms_by_code,
                new_atoms_by_code=new_atoms_by_code,
            )
            result["_llm_error"] = err
            return result
    return suggest_pair_rules(
        old_block,
        new_block,
        old_atoms_by_code=old_atoms_by_code,
        new_atoms_by_code=new_atoms_by_code,
    )


# 兼容旧名称
suggest_pair_rule = suggest_pair_rules


def _atoms_by_code(lesson_id: str) -> dict[str, TextbookAtom]:
    rows = TextbookAtom.query.filter_by(lesson_id=lesson_id).all()
    return {a.atom_code: a for a in rows}


def _block_dict(block: Block, side: str, atoms_by_code: dict[str, TextbookAtom]) -> dict:
    return _block_payload(block, side=side, atoms_by_code=atoms_by_code)


def run_auto_suggest(
    *,
    new_lesson_uid: str,
    match_id: str | None = None,
    overwrite: bool = False,
    lesson_match_id: str | None = None,
    use_llm: bool | None = None,
) -> dict:
    from ....services.llm.config import llm_enabled

    lesson_match, old_les, new_les = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    if lesson_match.compare_confirmed_at:
        raise ValueError("本课对比已确认，无法生成 AI 建议")

    effective_llm = use_llm if use_llm is not None else llm_enabled()

    old_atoms = _atoms_by_code(old_les.id)
    new_atoms = _atoms_by_code(new_les.id)
    old_blocks = {b.id: b for b in Block.query.filter_by(lesson_id=old_les.id).all()}
    new_blocks = {b.id: b for b in Block.query.filter_by(lesson_id=new_les.id).all()}

    q = BlockMatch.query.filter_by(lesson_match_id=lesson_match.id)
    if match_id:
        q = q.filter_by(id=match_id)
    matches = q.all()
    if not matches:
        raise ValueError("尚无配对，无法生成建议")

    updated = 0
    llm_used = 0
    llm_failed = 0
    llm_last_error: str | None = None

    matches_by_new: dict[str, list[BlockMatch]] = {}
    for bm in matches:
        if bm.new_block_id:
            matches_by_new.setdefault(bm.new_block_id, []).append(bm)

    suggestion_cache: dict[str, dict] = {}

    for bm in matches:
        meta = dict(bm.metadata_json or {})
        if meta.get("ai_suggestion") and not overwrite:
            continue
        new_block = new_blocks.get(bm.new_block_id or "")
        new_payload = _block_dict(new_block, "new", new_atoms) if new_block else None

        cache_key = str(bm.new_block_id or bm.id)
        if cache_key not in suggestion_cache:
            group = matches_by_new.get(bm.new_block_id or "", [bm]) if bm.new_block_id else [bm]
            old_payloads: list[dict] = []
            for member in group:
                ob = old_blocks.get(member.old_block_id or "")
                if ob:
                    old_payloads.append(_block_dict(ob, "old", old_atoms))
            old_payloads.sort(
                key=lambda p: int(p.get("sort_order") or 0),
            )
            if len(old_payloads) > 1 and new_payload:
                suggestion_cache[cache_key] = suggest_group_rules(
                    new_block=new_payload,
                    old_blocks=old_payloads,
                    old_atoms_by_code=old_atoms,
                    new_atoms_by_code=new_atoms,
                )
            else:
                old_block = old_blocks.get(bm.old_block_id or "")
                old_payload = _block_dict(old_block, "old", old_atoms) if old_block else None
                suggestion_cache[cache_key] = suggest_pair(
                    old_payload,
                    new_payload,
                    old_atoms_by_code=old_atoms,
                    new_atoms_by_code=new_atoms,
                    use_llm=effective_llm and len(group) == 1,
                )

        suggestion = dict(suggestion_cache[cache_key])
        llm_err = suggestion.pop("_llm_error", None)
        if llm_err:
            llm_last_error = llm_err
        if suggestion.get("source") in ("llm_api", "llm_bailian"):
            llm_used += 1
        elif effective_llm and suggestion.get("source") == "rules" and len(
            matches_by_new.get(bm.new_block_id or "", [bm])
        ) == 1:
            llm_failed += 1
        group = matches_by_new.get(bm.new_block_id or "", [bm]) if bm.new_block_id else [bm]
        if len(group) > 1:
            role = "primary" if bm.id == group[0].id else "secondary"
            suggestion = {**suggestion, "anchor_group_role": role}
        meta["ai_suggestion"] = suggestion
        bm.metadata_json = meta
        updated += 1

    db.session.commit()
    data = build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
    data["suggested_count"] = updated
    data["llm_used_count"] = llm_used
    data["llm_failed_count"] = llm_failed
    data["llm_enabled"] = effective_llm
    if effective_llm and updated and llm_used == 0:
        hint = llm_last_error or "未知错误"
        if "ModuleNotFoundError" in hint and "langchain" in hint.lower():
            hint = "缺少依赖，请在 textbook-handoff 环境执行 pip install -r requirements.txt"
        elif not llm_enabled():
            hint = ".env 中未读到 LLM_APP_ID/LLM_API_KEY 或 LLM_MODEL（改 .env 后无需重启，直接重试即可）"
        data["llm_warning"] = f"AI 模型未成功调用（{hint}），已回退规则引擎"
        data["llm_last_error"] = llm_last_error
    return data


def apply_ai_suggestion(
    *,
    new_lesson_uid: str,
    match_id: str,
    overwrite: bool = False,
    lesson_match_id: str | None = None,
) -> dict:
    from .matches import _normalize_change_type, _normalize_reuse_action

    lesson_match, _, _ = get_lesson_match_for_new_lesson(
        new_lesson_uid, lesson_match_id=lesson_match_id
    )
    if lesson_match.compare_confirmed_at:
        raise ValueError("本课对比已确认，无法采纳建议")

    bm = BlockMatch.query.filter_by(
        id=match_id, lesson_match_id=lesson_match.id
    ).first()
    if not bm:
        raise ValueError("未找到该配对")

    ai = (bm.metadata_json or {}).get("ai_suggestion") or {}
    if not ai:
        raise ValueError("该行尚无 AI 建议，请先生成")

    if overwrite or not (bm.reuse_action or "").strip():
        bm.reuse_action = _normalize_reuse_action(ai.get("reuse_action"))
    if overwrite or not (bm.change_type or "").strip():
        bm.change_type = _normalize_change_type(ai.get("change_type"))
    if overwrite or not (bm.teacher_note or "").strip():
        note = str(ai.get("teacher_note") or "").strip()
        bm.teacher_note = note or None

    bm.updated_at = datetime.utcnow()
    db.session.commit()
    return build_compare_workspace(
        new_lesson_uid=new_lesson_uid, lesson_match_id=lesson_match.id
    )
