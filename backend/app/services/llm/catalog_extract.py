"""大模型视觉识别教材目录（扫描版 PDF 前几页）。"""
from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ...parsers.llm_toc_cache import (
    get_llm_catalog_rows,
    get_llm_toc_entries,
    put_llm_catalog_rows,
    put_llm_toc_entries,
)
from ...parsers.pdf_toc import TocEntry
from ...parsers.text_norm import norm_text, strip_lesson_seq
from .config import build_chat_openai, llm_enabled, llm_model, llm_vision_model

logger = logging.getLogger(__name__)

CATALOG_EXTRACT_SYSTEM = """你是教材目录结构化专家。根据用户附带的 PDF 目录页图片，提取正式教学目录。

图片说明：目录页已做预处理，尽量去除了红色印章/水印，正文与页码须完整保留；请只读文字，勿因残留印章干扰而漏行。

【总原则 · 所见即所得】
默认按目录印刷原文逐行提取，课号/标题不要改写成其它学科的格式（尤其不要默认改成化学「课题N」）。
用户若写明具体学科，再遵循该学科特例。

通用规则：
1. 只提取教学目录行；剔除前言、版权、CIP、后记、封面广告等非目录行。
2. 单元列写完整名称（如「第一单元 …」「第十二章 …」）；若有开篇「绪论」，单独作为 unit=「绪论」。
3. 课时列保留目录原文；一行一课，禁止把相邻多课粘进同一个 lesson。
4. pdf_page 填目录行右侧印刷页码（非 PDF 文件页序）；看不清则留空。
5. 按教材顺序输出；不要编造目录上不存在的课时；无绪论时不要输出绪论。
6. warnings 列出需人工复核的存疑点。

学科特例（仅当用户写明对应学科时生效）：
7. 语文：目录末「识字表/写字表/词语表」各保留一行（unit=附录）；快乐读书吧/口语交际/习作/语文园地必须分行。
8. 数学：unit 为章名；lesson 为「12.1 …」「读一读 …」「回顾与反思」「复习题」等原文，勿改成课题N。
9. 小学科学：lesson 保留「1 运动起来」或目录上的课名/专题研究等原文；不要改成「课题N」。
10. 化学：【所见即所得】lesson 必须与目录印刷一致：「课题1 …」「整理与提升」「复习与提高」「实验活动1 …」「跨学科实践活动1 …」等；
    禁止把「实验活动」「跨学科实践活动」改写成「课题N …」；禁止丢掉活动序号与标题。"""


CATALOG_YUWEN_USER_HINT = (
    "学科：语文。一行一课：快乐读书吧/数字课/口语交际/习作/语文园地必须分行，禁止粘成长串。"
    "务必提取目录末尾的「识字表」「写字表」「词语表」"
    "（unit=附录，填右侧印刷页码），勿并入语文园地。\n"
)

CATALOG_SHUXUE_USER_HINT = (
    "学科：数学。【所见即所得】按目录印刷原文逐行提取，不要改写课号/标题，不要套用化学「课题N」格式。\n"
    "unit 写章名原文（如「第十二章 分式和分式方程」）；"
    "lesson 写该行原文（如「12.1 分式」「读一读 分式方程的增根」「数学活动 …」"
    "「回顾与反思」「复习题」「主题探究（一）…」）。\n"
    "目录上出现的节与栏目全部保留，勿省略；一行一课，禁止粘连。\n"
)

CATALOG_KEXUE_USER_HINT = (
    "学科：小学科学。【按已调优的科学目录规则·所见即所得】"
    "按目录印刷顺序逐行提取，不要打乱课序，不要改写课号。"
    "unit 写「第N单元 …」原文；lesson 写目录原文（如「1 运动起来」「2 撬棍的学问」"
    "「专题研究 我的机器」），保留课序号与标题。"
    "「专题研究」「生涯链接」「能力增长」「评价表」等栏目按目录所在位置原样保留，"
    "不要把专题研究改成单元第1课；不要改成化学「课题N」；一行一课，禁止粘连。"
    "严禁用旧版课名或常识替换目录印刷标题"
    "（例如目录是「专题研究 厨房里的变化」就不得写成「自制酸碱指示剂」）。"
    "必须从第一单元提取到最后一单元，禁止只读最后一张图/对开右页。"
    "对开扫描若一页是目录、一页是课文，只提取目录页上的课时，不要把课文大标题当成整册目录。"
    "对开扫描的左右页都是目录时两侧都要提取；每课填写右侧印刷页码。\n"
)

CATALOG_HUAXUE_USER_HINT = (
    "学科：化学。【所见即所得】按目录印刷原文逐行提取，一字不改课号/栏目名/标题；"
    "正确示例：「课题1 我们周围的空气」「实验活动1 氧气的实验室制取与性质」"
    "「跨学科实践活动1 微型空气质量…」；"
    "错误示例（禁止）：「课题6 实验活动」「课题7 跨学科实践活动」。"
    "整理与提升、复习与提高、绪论等凡目录有的一律保留；一行一课，禁止粘连。\n"
)

CATALOG_DEFAULT_AS_SEEN_HINT = (
    "【默认·所见即所得】未指定专用学科规则时：按目录印刷原文逐行提取，"
    "不要套用化学「课题N」或其它学科格式改写课号/标题；一行一课，禁止粘连。\n"
)


class LlmCatalogLesson(BaseModel):
    lesson: str = Field(
        description="目录行原文：'1 课时名'、'课题 1 …'、'整理与提升'、'复习与提高'、"
        "'实验活动1 …'、'跨学科实践活动1 …'、'单元小结'，"
        "或语文附录表 '识字表'/'写字表'/'词语表'",
    )
    pdf_page: int | None = Field(
        default=None,
        description="目录行右侧印刷页码（非 PDF 文件页序）",
    )

    @field_validator("lesson")
    @classmethod
    def _strip_lesson(cls, v: str) -> str:
        return re.sub(r"\s+", " ", (v or "").strip())


class LlmCatalogUnit(BaseModel):
    unit: str = Field(description="单元全称")
    lessons: list[LlmCatalogLesson] = Field(default_factory=list)

    @field_validator("unit")
    @classmethod
    def _strip_unit(cls, v: str) -> str:
        return re.sub(r"\s+", " ", (v or "").strip())


class LlmCatalogExtract(BaseModel):
    units: list[LlmCatalogUnit] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _grade_label(grade: int | None) -> str:
    if grade is None:
        return "（未知）"
    return f"{grade}年级"


def _semester_label(semester: str | None) -> str:
    s = (semester or "").strip()
    if "下" in s:
        return "下学期"
    if s:
        return "上学期"
    return "（未知）"


def render_pdf_toc_images(
    pdf_path: Path,
    *,
    max_pages: int = 4,
    page_start: int = 0,
    page_end: int | None = None,
    zoom: float = 1.2,
    max_side: int = 1400,
    jpeg_quality: int = 82,
) -> list[str]:
    """渲染目录区页面为 data:image/jpeg;base64,...（内存去印章 + 压缩，避免巨型 PNG）。"""
    import fitz

    from ...parsers.pdf_catalog_step1 import filter_toc_like_bgrs
    from ...parsers.pdf_stamp_remove import prepare_catalog_page_bgrs

    doc = fitz.open(str(pdf_path))
    regions = []
    try:
        last = page_end if page_end is not None else page_start + max_pages - 1
        last = min(last, len(doc) - 1)
        for i in range(max(0, page_start), last + 1):
            regions.extend(
                prepare_catalog_page_bgrs(
                    doc.load_page(i),
                    zoom=zoom,
                    max_side=max_side,
                )
            )
    finally:
        doc.close()
    kept = filter_toc_like_bgrs(regions)
    if not kept:
        kept = regions
    logger.info(
        "目录视觉图 %s：渲染 %d 张，保留目录半页 %d 张",
        pdf_path.name,
        len(regions),
        len(kept),
    )
    urls: list[str] = []
    for idx, bgr in enumerate(kept):
        ok, buf = __import__("cv2").imencode(
            ".jpg",
            bgr,
            [__import__("cv2").IMWRITE_JPEG_QUALITY, jpeg_quality],
        )
        if not ok:
            raise ValueError(f"无法编码目录页 {idx + 1}")
        b64 = base64.standard_b64encode(buf.tobytes()).decode("ascii")
        urls.append(f"data:image/jpeg;base64,{b64}")
    return urls


def _lesson_match_key(lesson: str) -> str:
    return norm_text(strip_lesson_seq(lesson))


def science_toc_looks_complete(
    rows: list[dict[str, Any]],
    *,
    toc_n: int = 0,
) -> tuple[bool, str]:
    """小学科学整册目录：须含第一单元（或绪论）且有足够印刷页码。"""
    n = 0
    units = []
    pages = 0
    for row in rows or []:
        lesson = str(row.get("lesson") or "").strip()
        if lesson and "单元小结" not in lesson:
            n += 1
        units.append(str(row.get("unit") or ""))
        page = row.get("pdf_page")
        try:
            if page is not None and 1 <= int(page) <= 400:
                pages += 1
        except (TypeError, ValueError):
            pass
    if n < 3:
        return False, f"仅提取 {n} 课"
    unit_blob = " ".join(units)
    if (
        "第一单元" not in unit_blob
        and "第1单元" not in unit_blob
        and "绪论" not in unit_blob
    ):
        return False, "未包含第一单元，可能只识别到目录末页"
    if max(int(toc_n or 0), pages) < 3:
        return False, "目录行缺少印刷页码"
    return True, ""


def llm_result_to_rows_and_toc(
    result: LlmCatalogExtract,
) -> tuple[list[dict[str, Any]], list[TocEntry]]:
    rows: list[dict[str, Any]] = []
    toc: list[TocEntry] = []
    for unit in result.units:
        unit_title = (unit.unit or "").strip()
        if not unit_title:
            continue
        for les in unit.lessons:
            lesson_label = (les.lesson or "").strip()
            if not lesson_label:
                continue
            row: dict[str, Any] = {"unit": unit_title, "lesson": lesson_label}
            if les.pdf_page is not None and 1 <= les.pdf_page <= 400:
                row["pdf_page"] = int(les.pdf_page)
            rows.append(row)
            if les.pdf_page is not None and 1 <= les.pdf_page <= 400:
                mk = _lesson_match_key(lesson_label)
                if mk or "小结" in lesson_label:
                    toc.append(
                        TocEntry(
                            unit_norm=norm_text(unit_title)[:48],
                            title_raw=lesson_label,
                            page_1=int(les.pdf_page),
                            match_key=mk or norm_text("单元小结"),
                        )
                    )
    return rows, toc


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
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(getattr(item, "text", item) or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _parse_json_fallback(text: Any) -> dict[str, Any]:
    raw = _message_content_to_text(text)
    m = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
    raw = (m.group(1) if m else raw) or ""
    # 结构化失败常见原因：字符串里夹了 ASCII 控制符
    raw = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw)
    raw = raw.strip()
    errors: list[Exception] = []

    def _try_load(blob: str) -> dict[str, Any]:
        start = blob.find("{")
        if start < 0:
            raise ValueError("响应中无 JSON 对象")
        data, _end = json.JSONDecoder().raw_decode(blob[start:])
        return LlmCatalogExtract.model_validate(data).model_dump()

    for candidate in (raw,):
        try:
            return _try_load(candidate)
        except Exception as exc:
            errors.append(exc)
    # 兼容旧逻辑：贪婪取最外层大括号后再解析
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        try:
            return _try_load(m2.group(0))
        except Exception as exc:
            errors.append(exc)
        try:
            data = json.loads(m2.group(0))
            return LlmCatalogExtract.model_validate(data).model_dump()
        except Exception as exc:
            errors.append(exc)
    detail = errors[-1] if errors else ValueError("无法解析目录 JSON")
    raise ValueError(f"目录 JSON 解析失败：{detail}") from detail


def _invoke_vision_llm(
    *,
    user_text: str,
    image_urls: list[str],
    max_attempts: int = 3,
) -> LlmCatalogExtract:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(
        max_tokens=4096,
        model=llm_vision_model(),
        include_reasoning=False,
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    messages = [
        SystemMessage(content=CATALOG_EXTRACT_SYSTEM),
        HumanMessage(content=content),
    ]
    last_exc: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            structured = llm.with_structured_output(LlmCatalogExtract)
            return structured.invoke(messages)
        except Exception as exc:
            logger.warning(
                "LLM structured catalog failed (attempt %d/%d), fallback JSON: %s",
                attempt,
                max_attempts,
                exc,
            )
            try:
                msg = llm.invoke(messages)
                data = _parse_json_fallback(getattr(msg, "content", msg))
                return LlmCatalogExtract.model_validate(data)
            except Exception as parse_exc:
                last_exc = parse_exc
                logger.warning(
                    "LLM catalog JSON fallback failed (attempt %d/%d): %s",
                    attempt,
                    max_attempts,
                    parse_exc,
                )
    assert last_exc is not None
    raise last_exc


def extract_catalog_with_llm_vision(
    pdf_path: Path,
    *,
    edition: str | None = None,
    grade: int | None = None,
    semester: str | None = None,
    subject: str | None = None,
    max_pages: int = 4,
    page_start: int = 0,
    page_end: int | None = None,
    force_refresh: bool = False,
    content_hash: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """
    用大模型识别目录页图片。
    返回 (catalog_rows, source_tag)；同时写入 llm_toc_cache 供页码匹配复用。
    """
    if not llm_enabled():
        raise RuntimeError("LLM 未启用或未配置 LLM_APP_ID/LLM_API_KEY / LLM_MODEL")

    _subj = (subject or "").strip()
    is_yuwen = _subj in ("语文", "Test")
    is_shuxue = _subj == "数学"
    is_kexue = _subj in ("科学", "小科")
    is_huaxue = (subject or "").strip() == "化学"

    if not force_refresh:
        cached_rows = get_llm_catalog_rows(pdf_path, content_hash=content_hash)
        cached_toc = get_llm_toc_entries(pdf_path, content_hash=content_hash)
    else:
        cached_rows = None
        cached_toc = None
    if cached_rows and cached_toc:
        cache_ok = len(cached_rows) >= 3
        if is_kexue:
            cache_ok, _why = science_toc_looks_complete(
                cached_rows, toc_n=len(cached_toc)
            )
        if cache_ok:
            if is_yuwen:
                from ..textbook_diff.yuwen_appendix import ensure_yuwen_appendix_tables

                cached_rows, cached_toc, changed = ensure_yuwen_appendix_tables(
                    pdf_path, cached_rows, cached_toc
                )
                if changed:
                    put_llm_catalog_rows(pdf_path, cached_rows, content_hash=content_hash)
                    put_llm_toc_entries(pdf_path, cached_toc, content_hash=content_hash)
            logger.info(
                "LLM catalog cache hit %s: %d rows, %d page hints",
                pdf_path.name,
                len(cached_rows),
                len(cached_toc),
            )
            return cached_rows, "llm_vision_cache"
        logger.warning(
            "LLM catalog cache 行数不足（%d），重新识别 %s",
            len(cached_rows),
            pdf_path.name,
        )

    image_urls = render_pdf_toc_images(
        pdf_path,
        max_pages=max_pages,
        page_start=page_start,
        page_end=page_end,
    )
    if not image_urls:
        raise ValueError("PDF 无可用页面")

    meta = []
    if edition:
        meta.append(f"版本：{edition}")
    meta.append(f"年级：{_grade_label(grade)}")
    meta.append(f"学期：{_semester_label(semester)}")
    subject_hint = CATALOG_DEFAULT_AS_SEEN_HINT
    if is_yuwen:
        meta.append(
            "学科：语文"
            if (subject or "").strip() == "语文"
            else "学科：语文（Test 册按语文目录规则）"
        )
        subject_hint = CATALOG_YUWEN_USER_HINT
    elif is_shuxue:
        meta.append("学科：数学")
        subject_hint = CATALOG_SHUXUE_USER_HINT
    elif is_kexue:
        meta.append("学科：小学科学")
        subject_hint = CATALOG_KEXUE_USER_HINT
    elif is_huaxue:
        meta.append("学科：化学")
        subject_hint = CATALOG_HUAXUE_USER_HINT
    elif (subject or "").strip():
        meta.append(f"学科：{(subject or '').strip()}")

    user_text = (
        subject_hint
        + "请从附带的 PDF 前几页（含目录）提取全部单元与课时。\n"
        + "【一行一课】目录每一印刷行对应一个 lesson，禁止粘连。\n"
        + "页码只填目录行右侧印刷数字，不要换算 PDF 页序。\n"
        + " · ".join(meta)
        + f"\n共 {len(image_urls)} 张目录区页面图片"
        + ("（对开已拆成左右页，请全部提取）。" if len(image_urls) else "。")
    )

    last_empty: Exception | None = None
    rows: list[dict[str, Any]] = []
    toc_entries: list[TocEntry] = []
    for attempt in range(1, 4):
        result = _invoke_vision_llm(user_text=user_text, image_urls=image_urls)
        rows, toc_entries = llm_result_to_rows_and_toc(result)
        from ...parsers.catalog_lesson_line import expand_glued_catalog_rows

        rows = expand_glued_catalog_rows(rows)
        ok = len(rows) >= 3
        why = f"仅 {len(rows)} 行"
        if is_kexue:
            ok, why = science_toc_looks_complete(rows, toc_n=len(toc_entries))
        if ok:
            break
        last_empty = ValueError(f"大模型目录不完整（{why}）")
        logger.warning(
            "LLM catalog incomplete (attempt %d/3): %s for %s",
            attempt,
            why,
            pdf_path.name,
        )
    else:
        raise last_empty or ValueError("大模型目录行不足（0 行）")

    if is_yuwen:
        from ..textbook_diff.yuwen_appendix import ensure_yuwen_appendix_tables

        rows, toc_entries, _ = ensure_yuwen_appendix_tables(
            pdf_path, rows, toc_entries
        )

    put_llm_toc_entries(pdf_path, toc_entries, content_hash=content_hash)
    put_llm_catalog_rows(pdf_path, rows, content_hash=content_hash)
    logger.info(
        "LLM catalog %s: %d rows, %d page hints, model=%s",
        pdf_path.name,
        len(rows),
        len(toc_entries),
        llm_vision_model(),
    )
    return rows, "llm_vision"
