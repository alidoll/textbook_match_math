"""公司 ai-service：旧侧教学区块 AI 预建建议（多模态：课件页 + 教材页截图）。"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import build_chat_openai, llm_block_seed_model, llm_enabled, llm_model

logger = logging.getLogger(__name__)

_FEW_SHOT_LESSON_UID = "湘科版-2-下-old-U1-L1"
_REF_EXAMPLE_PATH = (
    Path(__file__).resolve().parent / "references" / "block_seed_push_pull_example.json"
)
_BLOCK_SEED_EXAMPLES_DIR = (
    Path(__file__).resolve().parent / "references" / "block_seed_examples"
)

_BLOCK_SEED_VISION_EXAMPLES: list[tuple[str, str]] = [
    (
        "dialogue_bubble_with_character.png",
        "【建块范例 · 正例：气泡 + 旁侧小人同一区块】\n"
        "教材：橙色圆角气泡「你能让一块泥巴发生类似的变化吗？」（如 A001-009）"
        "+ 气泡旁/指向气泡的坐小车小人插图（image）。\n"
        "正确：气泡 text 与小人 image **同一 block** 的 atom_codes，对应同一课件页/教学环节。\n"
        "禁止：只绑气泡文字、把小人拆到另一区块；或气泡与小人在不同 block。",
    ),
    (
        "subtitle_bar_bottom.png",
        "【建块范例 · 正例：活动小标题条（页底/页中 subtitle_bar）仅条内合并】\n"
        "教材：浅蓝胶囊条「生活中混合物的分离」+ 左侧看书小人 icon（image）。\n"
        "（同类：页顶/页中「分离盐和芝麻」+ 左侧小人 icon。）\n"
        "正确：条内 icon + 条内汉字 **同一 block** 的 atom_codes。\n"
        "禁止：把小标题条与页内说明句、对话气泡、场景插图、其他活动条 merge 到同一 block；"
        "也禁止只绑条内文字、漏掉左侧 icon。",
    ),
]

BLOCK_SEED_SYSTEM = """你是一位小学科学课件教研专家，负责把一节课的「课件页 + 教材原子」切成教学区块。

**你将收到课件页截图与教材页截图。绑定 atom_codes 与 course_slide_indices 时，必须同时对照画面与 OCR 文字：**
- 插图、活动场景、标题条、实物照片等**视觉元素一致**才可归入同一区块
- 不要仅凭「同一教材页」或「同一教学环节」就把相邻内容绑在一起
- 同一教材页上相邻但教学目的不同的区域，须拆到不同区块，分别对应不同课件页
- **反例（禁止）：** 教材「用橡皮泥捏造型」+ 绿色橡皮泥车插图 → 应对应课件「改变橡皮泥的形状」页；**不能**与课件「奇妙的形变 / 弹簧海绵」页绑定

**教材原子绑定细则（常见易错，务必逐条检查）：**

**【铁律一】对话气泡 + 旁侧小人/卡通角色 — 必须同块**
- 气泡 text（对话文字框）与**紧挨其旁的小人插图 image**（气泡尾巴指向的配图角色）→ **同一区块**，atom_codes 必须同时包含两者
- **严禁**把气泡文字放在 B5 而小人插图放在 B1；哪怕小人 image 没有 OCR 文字，也必须跟随气泡进同一 block
- 检查方法：每输出一个 block 后，立即看该 block 的 atom_codes 中是否有 image 类型原子——如果有，它旁边的对话气泡 text 是否也在这个 block 里

**【铁律二】活动小标题条（subtitle_bar）— 仅条内合并**
- 左侧小人/看书 icon + 浅蓝胶囊条内汉字（如「生活中混合物的分离」「分离盐和芝麻」）→ **仅条内** atom_codes 同一 block
- **禁止**与说明句、气泡、场景大图、其他小标题或相邻正文绑到同一 block

**【铁律三】跨类型永远拆块**
- 活动小标题条 vs 其下方/旁边**独立**对话气泡 → 不同 block
- 说明句 vs 气泡 → 不同 block
- 不同 bubble_id 的左右气泡 → 不同 block
- 页顶说明段 vs 页底小标题条 → 不同 block

每个区块有两层命名（务必都填）：
1. **stage_ref**（环节参考）：必须从给定的「允许环节名称」列表中精确选取一项，不要自造。
2. **topic_name**（区块名称 / 复用摘要）：用 12～56 字（**最多 64 字含标点**）概括**本块绑定的课件页 + 教材原子**两侧要点，便于后续判断素材复用程度。
   - **必须同时看课件 OCR 与教材原子**（哪一侧有内容就写哪一侧；两侧都有则都写）
   - 推荐格式：`课件：…；教材：…`（两侧各用短句，勿写长段落）
   - 要具体、可辨认，如「课件：推拉门导入案例；教材：推推手游戏说明」
   - 不要只重复 stage_ref；不要只写教材而忽略课件，也不要只写课件而忽略已绑定的教材原子
   - 同一 stage_ref 可出现多次，但 topic_name 应区分不同活动/知识点

切分原则：
0. 先阅读【标准人工建块范例 · 推拉游戏】与【建块视觉范例】（气泡+小人同块、小标题条仅条内合并），再处理待建块课时；同一类活动（如推手 vs 拔河 vs 情境提问）须拆块，勿合并。
1. 连续、同一教学目的的课件页应合并为一块（不要一页一块）。
2. 常见结构：课件名称页 → 导入 → 知识点讲解 / 实验活动操作（可多块）→ 知识小结 → 课件尾页。
3. 实验/活动操作：含步骤、记录表、动手图示的连续页归一块；不同活动拆成多块并写清 topic_name。
4. 当【教材原子】列表非空时：每块必须绑定与该块**课件页画面 + 文字**都匹配的 atom_codes；每个 atom_code 只能分配给一个区块，并尽量覆盖全部所列原子。
5. 仅当提示「尚无原子」时，atom_codes 才可留空数组。
6. 每张课件页只能出现在一个区块；尽量覆盖全部课件页。

输出 JSON 对象，含 blocks 数组；每项字段：
- stage_ref（字符串，取自允许环节名称）
- topic_name（字符串，本块课件+教材复用摘要，推荐「课件：…；教材：…」）
- course_slide_indices（正整数数组）
- atom_codes（字符串数组，可为空）
- reason（简短中文，说明为何这样切分）

只输出一个 JSON 对象，不要用 markdown 代码块（不要 ```json）包裹。
字符串字段（尤其 reason、topic_name）内勿使用英文双引号 "，引用课文时用书名号或省略引号。"""

TEACHING_INTENT_ADDENDUM = """

**额外字段 teaching_intent（教学意图）**：每个区块再写 1～2 句（20～80 字），说明本块要达成的教学目标、学生应理解的核心知识点或技能点；须基于所绑课件页与教材原子内容，勿重复 topic_name 字面，勿写空泛套话。"""


def _block_seed_system(*, include_teaching_intent: bool = False) -> str:
    if include_teaching_intent:
        return BLOCK_SEED_SYSTEM + TEACHING_INTENT_ADDENDUM
    return BLOCK_SEED_SYSTEM


class AiSuggestedBlock(BaseModel):
    stage_ref: str = Field(default="", description="教学环节，须为允许名称之一")
    topic_name: str = Field(default="", description="区块名称：概括本块课件 OCR + 教材原子要点")
    teaching_intent: str = Field(
        default="",
        description="教学意图：本块要达成的教学目标或学生理解（20～80字）",
    )
    course_slide_indices: list[int] = Field(description="课件页序号列表")
    atom_codes: list[str] = Field(default_factory=list, description="教材原子编号")
    reason: str = Field(default="", description="切分理由")

    @model_validator(mode="before")
    @classmethod
    def _legacy_block_name(cls, data: Any) -> Any:
        if isinstance(data, dict):
            d = dict(data)
            if not str(d.get("stage_ref") or "").strip():
                d["stage_ref"] = d.get("block_name") or d.get("label") or ""
            if not str(d.get("topic_name") or "").strip():
                d["topic_name"] = d.get("knowledge_name") or d.get("block_topic") or ""
            return d
        return data

    @field_validator("course_slide_indices")
    @classmethod
    def _positive_slides(cls, v: list[int]) -> list[int]:
        out = sorted({int(x) for x in v if int(x) > 0})
        if not out:
            raise ValueError("course_slide_indices 不能为空")
        return out


class AiBlockSeedPlan(BaseModel):
    blocks: list[AiSuggestedBlock] = Field(min_length=1)


def load_push_pull_few_shot_example() -> dict[str, Any] | None:
    """人工建块标杆：推拉游戏（湘科版-2-下-old-U1-L1）。

    优先使用仓库内 reference JSON（与锁定人工区块同步），避免 DB 被 AI 重建后 few-shot 失真。
    仅在 JSON 缺失时回退读数据库。
    """
    if _REF_EXAMPLE_PATH.is_file():
        data = json.loads(_REF_EXAMPLE_PATH.read_text(encoding="utf-8"))
        return {**data, "source": "reference_file", "lesson_uid": _FEW_SHOT_LESSON_UID}

    try:
        from ...models import Block, CoursewareSlide, Lesson, TextbookAtom
        from ..old_library.annotate.block_stage import read_block_fields

        les = Lesson.query.filter_by(lesson_uid=_FEW_SHOT_LESSON_UID).first()
        if les:
            blocks = (
                Block.query.filter_by(lesson_id=les.id)
                .order_by(Block.sort_order, Block.block_code)
                .all()
            )
            manual = [
                b
                for b in blocks
                if (read_block_fields(b, book_type="old").get("block_name") or "").strip()
            ]
            if len(manual) >= 5:
                slide_rows = {
                    int(s.slide_index): (s.ocr_text or "")
                    for s in CoursewareSlide.query.filter_by(lesson_id=les.id).all()
                }
                out_blocks: list[dict[str, Any]] = []
                for b in manual:
                    fields = read_block_fields(b, book_type="old")
                    atom_codes = list(b.atom_codes or [])
                    excerpts: list[str] = []
                    if atom_codes:
                        rows = TextbookAtom.query.filter(
                            TextbookAtom.lesson_id == les.id,
                            TextbookAtom.atom_code.in_(atom_codes),
                        ).all()
                        for row in rows[:4]:
                            t = (row.content or row.ocr_text or "").strip()
                            if t and not t.startswith("["):
                                excerpts.append(re.sub(r"\s+", " ", t)[:50])
                    slide_excerpts: list[str] = []
                    for idx in b.course_slide_indices or []:
                        t = (slide_rows.get(int(idx)) or "").strip().replace("\n", " ")
                        if t and not t.startswith("["):
                            slide_excerpts.append(re.sub(r"\s+", " ", t)[:50])
                    out_blocks.append(
                        {
                            "block_code": b.block_code,
                            "stage_ref": fields["stage_ref"],
                            "topic_name": fields["block_name"],
                            "course_slide_indices": list(b.course_slide_indices or []),
                            "atom_codes": atom_codes,
                            "atom_excerpts": excerpts,
                            "slide_excerpts": slide_excerpts,
                        }
                    )
                return {
                    "lesson_uid": les.lesson_uid,
                    "lesson_name": les.lesson_name,
                    "unit_title": les.unit_title,
                    "source": "database",
                    "blocks": out_blocks,
                }
    except Exception as exc:
        logger.debug("load push-pull few-shot from db failed: %s", exc)

    return None


def format_few_shot_example_text(example: dict[str, Any]) -> str:
    uid = example.get("lesson_uid") or _FEW_SHOT_LESSON_UID
    lines = [
        "【标准人工建块范例 · 推拉游戏】",
        f"课时：{example.get('unit_title', '')} · {example.get('lesson_name', '推拉游戏')}（{uid}）",
        "以下为教研锁定的人工区块：请仿照切分粒度、课件页合并方式，以及 atom_codes 与 course_slide_indices 的对应关系。",
        "（stage_ref=环节参考，topic_name=区块名称；绑定须画面+文字一致）",
        "",
    ]
    for i, b in enumerate(example.get("blocks") or [], start=1):
        code = b.get("block_code") or f"B{i:02d}"
        slides = b.get("course_slide_indices") or []
        atoms = b.get("atom_codes") or []
        excerpts = b.get("atom_excerpts") or []
        slide_excerpts = b.get("slide_excerpts") or []
        hint = str(b.get("binding_hint") or "").strip()
        atom_hint = ""
        if excerpts:
            atom_hint = f" | 教材：{'；'.join(excerpts[:2])}"
        elif atoms:
            atom_hint = f" | atoms={atoms}"
        slide_hint = ""
        if slide_excerpts:
            slide_hint = f" | 课件 OCR：{'；'.join(slide_excerpts[:2])}"
        lines.append(
            f"{code} stage_ref={b.get('stage_ref')} | topic_name={b.get('topic_name')} "
            f"| 课件 P{slides}{slide_hint}{atom_hint}"
        )
        if hint:
            lines.append(f"    ↳ {hint}")
    return "\n".join(lines)


def build_block_seed_user_prompt(
    *,
    lesson_name: str,
    unit_title: str,
    allowed_names: list[str],
    slides: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    few_shot_example: dict[str, Any] | None = None,
    audit_feedback: str | None = None,
) -> str:
    names = "、".join(allowed_names)
    slide_lines = []
    for s in slides:
        idx = int(s["slide_index"])
        text = (s.get("ocr_text") or "").strip().replace("\n", " ")[:280]
        if not text:
            text = "（暂无 OCR 文字，请结合页序与常见结构推断）"
        slide_lines.append(f"  P{idx}: {text}")

    atom_lines = []
    for pg in atoms_by_page:
        page_index = pg.get("page_index")
        pdf_page = pg.get("pdf_page")
        head = f"教材课内 p{page_index}"
        if pdf_page:
            head += f"（PDF p{pdf_page}）"
        items = pg.get("atoms") or []
        if not items:
            atom_lines.append(f"  {head}: （无原子）")
            continue
        parts = []
        for a in items[:12]:
            code = a.get("atom_code", "")
            excerpt = (a.get("content") or a.get("ocr_text") or "")[:60].replace("\n", " ")
            parts.append(f"{code}:{excerpt or '…'}")
        atom_lines.append(f"  {head}: " + " | ".join(parts))

    few_shot = ""
    ex = few_shot_example or load_push_pull_few_shot_example()
    if ex:
        few_shot = format_few_shot_example_text(ex) + "\n\n"

    audit_section = ""
    feedback = (audit_feedback or "").strip()
    if feedback:
        audit_section = (
            "\n\n【审核修正意见 · 二次建块】\n"
            "以下为上一轮建块审核发现的问题与当前区块快照，请在本轮输出中逐项修正：\n"
            + feedback
            + "\n"
        )

    return (
        few_shot
        + f"【待建块课时】{unit_title} · {lesson_name}\n"
        f"允许环节名称 stage_ref：{names}\n\n"
        f"【课件共 {len(slides)} 页】\n"
        + "\n".join(slide_lines)
        + "\n\n【教材原子】\n"
        + ("\n".join(atom_lines) if atom_lines else "  （尚无原子，atom_codes 可留空）")
        + audit_section
        + "\n\n请仿照人工范例与视觉范例（气泡+旁侧小人须同块；小标题条仅条内 icon+汉字同块、不得与页内其他原子混绑），为【待建块课时】输出 blocks。"
        + "每个 topic_name 须概括本块课件页 OCR 与所绑教材原子（推荐「课件：…；教材：…」）。"
        + "绑定 atom_codes 时须对照随后附上的课件/教材截图，画面与文字都相像才可同块。"
        + "\n\n【输出前自检 · 必做】逐一核对每个 block 的 atom_codes："
        + "（1）有 image 原子的 block —— 它旁边的对话气泡 text 也在这个 block 里吗？如果不在，立即修正；"
        + "（2）有 subtitle_bar 原子的 block —— 是否混入了说明句/气泡/场景大图？如果是，拆到另一个 block；"
        + "（3）每个原子是否**只出现在一个 block**？如有重复，修正为唯一归属。"
    )


def _block_seed_example_image_data_url(filename: str) -> str | None:
    path = _BLOCK_SEED_EXAMPLES_DIR / filename
    if not path.is_file():
        logger.warning("block seed example image missing: %s", path)
        return None
    data = path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _append_block_seed_vision_examples(content: list[dict[str, Any]]) -> None:
    if not _BLOCK_SEED_VISION_EXAMPLES:
        return
    n = len(_BLOCK_SEED_VISION_EXAMPLES)
    content.append(
        {
            "type": "text",
            "text": (
                f"═══ 建块视觉范例（共 {n} 张，请牢记绑定规则后再处理下方课件/教材）═══"
            ),
        }
    )
    for filename, caption in _BLOCK_SEED_VISION_EXAMPLES:
        content.append({"type": "text", "text": caption})
        url = _block_seed_example_image_data_url(filename)
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})


def _blob_to_data_url(blob_id: str | None) -> str | None:
    if not blob_id:
        return None
    try:
        from ...models import FileBlob
        from ..blobs import read_blob_bytes

        blob = FileBlob.query.get(str(blob_id).strip())
        if not blob:
            return None
        data = read_blob_bytes(blob)
        mime = (blob.mime_type or "image/png").lower()
        if "jpeg" in mime or "jpg" in mime:
            prefix = "data:image/jpeg;base64,"
        else:
            prefix = "data:image/png;base64,"
        b64 = base64.standard_b64encode(data).decode("ascii")
        return prefix + b64
    except Exception as exc:
        logger.debug("block seed blob %s unreadable: %s", blob_id, exc)
        return None


def _vision_images_available(
    slides: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
) -> bool:
    if any(s.get("blob_id") for s in slides):
        return True
    return any(pg.get("blob_id") for pg in atoms_by_page)


def _build_block_seed_vision_content(
    user_text: str,
    slides: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    _append_block_seed_vision_examples(content)
    content.append(
        {
            "type": "text",
            "text": (
                "═══ 课件页截图（按 P 序号）═══\n"
                "请逐页对照画面，决定各 atom_code 应绑哪几页课件。"
            ),
        }
    )
    for s in sorted(slides, key=lambda x: int(x.get("slide_index") or 0)):
        idx = int(s["slide_index"])
        ocr = re.sub(r"\s+", " ", (s.get("ocr_text") or "").strip())[:140]
        labels = [
            str(x).strip()
            for x in (s.get("image_labels") or [])
            if str(x).strip()
        ]
        label_hint = f"；插图：{'、'.join(labels[:6])}" if labels else ""
        content.append(
            {
                "type": "text",
                "text": f"【课件 P{idx}】OCR：{ocr or '（暂无文字）'}{label_hint}",
            }
        )
        url = _blob_to_data_url(s.get("blob_id"))
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})

    if atoms_by_page:
        content.append(
            {
                "type": "text",
                "text": (
                    "═══ 教材课内页截图 ═══\n"
                    "同一页上不同区域可能对应不同课件页；按画面拆分 atom_codes，勿整页绑到同一课件。"
                ),
            }
        )
        for pg in sorted(atoms_by_page, key=lambda x: int(x.get("page_index") or 0)):
            pi = int(pg.get("page_index") or 0)
            pdf_page = pg.get("pdf_page")
            head = f"【教材 p{pi}】"
            if pdf_page:
                head += f"（PDF p{pdf_page}）"
            parts = []
            for a in pg.get("atoms") or []:
                code = a.get("atom_code", "")
                excerpt = re.sub(
                    r"\s+",
                    " ",
                    (a.get("content") or a.get("ocr_text") or "")[:40],
                )
                parts.append(f"{code}:{excerpt or '…'}")
            content.append(
                {
                    "type": "text",
                    "text": head + "\n原子：" + (" | ".join(parts) if parts else "（无）"),
                }
            )
            url = _blob_to_data_url(pg.get("blob_id"))
            if url:
                content.append({"type": "image_url", "image_url": {"url": url}})

    content.append(
        {
            "type": "text",
            "text": "请结合以上截图与 OCR，输出 blocks JSON。",
        }
    )
    return content


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item["text"]))
        return "\n".join(p for p in parts if p)
    return str(content)


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    for pattern in (
        r"^```(?:json)?\s*\n?(.*?)\n?```\s*$",
        r"^'''(?:json)?\s*\n?(.*?)\n?'''\s*$",
        r"^```(?:json)?\s*\n?(.*)$",
        r"^'''(?:json)?\s*\n?(.*)$",
    ):
        m = re.match(pattern, s, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return s


def _iter_exception_chain(exc: BaseException | None):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _looks_like_json_text(text: str) -> bool:
    s = (text or "").strip()
    return bool(
        s.startswith("{")
        or s.startswith("```")
        or s.startswith("'''")
        or "```json" in s
        or "'''json" in s
    )


def _repair_unescaped_quotes_in_json_field(raw: str, field_name: str) -> str:
    """LLM 常在 reason/topic_name 等字段内插入未转义的 ASCII 双引号，导致 json.loads 失败。"""
    key = f'"{field_name}"'
    result: list[str] = []
    i = 0
    while i < len(raw):
        idx = raw.find(key, i)
        if idx < 0:
            result.append(raw[i:])
            break
        result.append(raw[i:idx])
        j = idx + len(key)
        while j < len(raw) and raw[j] in " \t:":
            j += 1
        if j >= len(raw) or raw[j] != '"':
            result.append(raw[idx:j])
            i = j
            continue
        j += 1
        val_chars: list[str] = []
        while j < len(raw):
            c = raw[j]
            if c == "\\" and j + 1 < len(raw):
                val_chars.append(raw[j : j + 2])
                j += 2
                continue
            if c == '"':
                k = j + 1
                while k < len(raw) and raw[k] in " \t\r\n":
                    k += 1
                if k < len(raw) and raw[k] in ",}]":
                    break
                val_chars.append('\\"')
                j += 1
                continue
            val_chars.append(c)
            j += 1
        result.append(key)
        result.append(': "')
        result.extend(val_chars)
        result.append('"')
        i = j + 1
    return "".join(result)


def _repair_llm_json(raw: str) -> str:
    text = _strip_json_fence(raw)
    for field in ("reason", "topic_name", "stage_ref"):
        text = _repair_unescaped_quotes_in_json_field(text, field)
    return text


def _parse_json_fallback(text: Any) -> dict[str, Any]:
    raw = _strip_json_fence(_message_content_to_text(text))
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads(_repair_llm_json(raw))
    return AiBlockSeedPlan.model_validate(data).model_dump()


def _plan_from_llm_text(text: str) -> AiBlockSeedPlan:
    data = _parse_json_fallback(text)
    return AiBlockSeedPlan.model_validate(data)


def _candidates_from_structured_failure(exc: Exception) -> list[str]:
    candidates: list[str] = []
    seen_texts: set[str] = set()

    def _add(text: str) -> None:
        raw = text or ""
        if not raw.strip():
            return
        key = raw.strip()
        if key in seen_texts:
            return
        seen_texts.add(key)
        candidates.append(raw)

    try:
        from pydantic import ValidationError
    except ImportError:
        ValidationError = ()  # type: ignore[misc, assignment]

    try:
        from langchain_core.exceptions import OutputParserException
    except ImportError:
        OutputParserException = ()  # type: ignore[misc, assignment]

    for err in _iter_exception_chain(exc):
        if isinstance(err, ValidationError):
            for item in err.errors():
                inp = item.get("input")
                if isinstance(inp, str):
                    _add(inp)
        if OutputParserException and isinstance(err, OutputParserException):
            if err.llm_output:
                _add(err.llm_output)
        msg = str(err)
        if _looks_like_json_text(msg):
            _add(msg)
    return candidates


def _plan_from_any_result(result: Any) -> AiBlockSeedPlan:
    if isinstance(result, AiBlockSeedPlan):
        return result
    if isinstance(result, dict):
        parsed = result.get("parsed")
        if isinstance(parsed, AiBlockSeedPlan):
            return parsed
        raw_msg = result.get("raw")
        if raw_msg is not None:
            text = _message_content_to_text(getattr(raw_msg, "content", raw_msg))
            if text.strip():
                return _plan_from_llm_text(text)
        parsing_error = result.get("parsing_error")
        if parsing_error:
            for raw in _candidates_from_structured_failure(parsing_error):
                try:
                    return _plan_from_llm_text(raw)
                except Exception:
                    continue
    text = _message_content_to_text(getattr(result, "content", result))
    if text.strip():
        return _plan_from_llm_text(text)
    raise ValueError("LLM 未返回可解析的建块 JSON")


def _invoke_langchain(
    user_prompt: str,
    *,
    slides: list[dict[str, Any]] | None = None,
    atoms_by_page: list[dict[str, Any]] | None = None,
    include_teaching_intent: bool = False,
) -> AiBlockSeedPlan:
    slides = slides or []
    atoms_by_page = atoms_by_page or []
    use_vision = _vision_images_available(slides, atoms_by_page)
    system_prompt = _block_seed_system(include_teaching_intent=include_teaching_intent)

    if use_vision:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = llm_block_seed_model()
        llm = build_chat_openai(max_tokens=4096, model=model, include_reasoning=True)
        content = _build_block_seed_vision_content(user_prompt, slides, atoms_by_page)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=content),
        ]
        try:
            structured = llm.with_structured_output(AiBlockSeedPlan)
            return structured.invoke(messages)
        except Exception as exc:
            logger.warning("block seed vision structured output failed: %s", exc)
        try:
            msg = llm.invoke(messages)
        except Exception as exc:
            logger.exception("block seed vision LLM invoke failed")
            raise ValueError(f"AI 建块请求失败：{exc}") from exc
        raw = _message_content_to_text(getattr(msg, "content", msg))
        if not raw.strip():
            raise ValueError("AI 建块返回为空，请重试")
        try:
            return _plan_from_llm_text(raw)
        except Exception as exc:
            logger.warning(
                "block seed vision JSON parse failed: %s; head=%r",
                exc,
                raw[:500],
            )
            raise ValueError(f"AI 建块结果解析失败：{exc}") from exc

    from langchain_core.prompts import ChatPromptTemplate

    llm = build_chat_openai(max_tokens=4096, include_reasoning=False)
    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system_prompt),
            ("human", "{user_input}"),
        ]
    )
    try:
        structured = llm.with_structured_output(AiBlockSeedPlan)
        chain = prompt | structured
        return chain.invoke({"user_input": user_prompt})
    except Exception as exc:
        logger.warning("block seed structured output failed, fallback to raw JSON: %s", exc)

    chain = prompt | llm
    try:
        msg = chain.invoke({"user_input": user_prompt})
    except Exception as exc:
        logger.exception("block seed LLM invoke failed")
        raise ValueError(f"AI 建块请求失败：{exc}") from exc

    content = _message_content_to_text(getattr(msg, "content", msg))
    if not content.strip():
        extra = getattr(msg, "additional_kwargs", None) or {}
        content = _message_content_to_text(
            extra.get("content") or extra.get("reasoning_content") or ""
        )
    if not content.strip():
        raise ValueError("AI 建块返回为空，请重试")

    try:
        return _plan_from_llm_text(content)
    except Exception as exc:
        logger.warning(
            "block seed JSON parse failed: %s; content head=%r",
            exc,
            content[:500],
        )
        raise ValueError(f"AI 建块结果解析失败：{exc}") from exc


def suggest_blocks_plan_with_llm(
    *,
    lesson_name: str,
    unit_title: str,
    allowed_names: list[str],
    slides: list[dict[str, Any]],
    atoms_by_page: list[dict[str, Any]],
    include_teaching_intent: bool = False,
    audit_feedback: str | None = None,
) -> dict[str, Any]:
    if not llm_enabled():
        raise RuntimeError("LLM 未启用或未配置 LLM_APP_ID/LLM_API_KEY / LLM_MODEL")

    few_shot = load_push_pull_few_shot_example()
    user_prompt = build_block_seed_user_prompt(
        lesson_name=lesson_name,
        unit_title=unit_title,
        allowed_names=allowed_names,
        slides=slides,
        atoms_by_page=atoms_by_page,
        few_shot_example=few_shot,
        audit_feedback=audit_feedback,
    )
    use_vision = _vision_images_available(slides, atoms_by_page)
    plan = _invoke_langchain(
        user_prompt,
        slides=slides,
        atoms_by_page=atoms_by_page,
        include_teaching_intent=include_teaching_intent,
    )
    blocks_out = []
    for b in plan.blocks:
        d = b.model_dump()
        d["block_name"] = d.get("stage_ref") or ""  # 兼容旧字段名
        blocks_out.append(d)
    return {
        "blocks": blocks_out,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": "llm_vision" if use_vision else "llm_api",
        "model": llm_block_seed_model() if use_vision else llm_model(),
        "few_shot_source": (few_shot or {}).get("source"),
    }
