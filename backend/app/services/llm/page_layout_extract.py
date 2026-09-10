"""教材页版面分析：Doubao 视觉模型返回插图/印章/页码 bbox。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import build_chat_openai, llm_enabled, llm_vision_model

logger = logging.getLogger(__name__)

def _cache_dir() -> Path:
    from ...repo_paths import app_root
    return app_root() / "data" / "base_data" / "cache" / "page_layout"

_CACHE_DIR = None  # 懒加载

# 提示词变更时递增，避免沿用漏检地图/资料袋插图的旧缓存
PAGE_LAYOUT_PROMPT_VERSION = 3

PAGE_LAYOUT_SYSTEM = """你是 K12 教材页面版面分析专家。根据附带的整页扫描图，识别插图区域与应排除的噪声区域。

规则：
1. illustration：框选照片、手绘、示意图、**地图/政区图/地形图/示意图**等画面区域。
   - 框的是整幅画面外轮廓（含图内色块边框）；**不要**把图外栏目说明、步骤标题（如「制模」「熔铸」）、正文段落、表格单元格包进插图框。
   - 【地图必出】含地名、省界、海岸线、图例、指北针的地图/示意图，即使图内有印刷汉字标签，也必须输出 illustration；图内标签不算「不得框选的正文」。
   - 【资料袋/阅读窗】侧栏色块、资料袋内嵌的配图、地图与正文照片同等对待，勿因旁边有说明文字而漏框。
2. 每个 illustration 必须填写 label：5~15 个汉字的简短主题描述（如「钱塘江潮涌位置示意图」「冰雪消融河流场景」「小学生做实验」），用于图文匹配；禁止留空。
3. stamp_noise：红色圆形/椭圆印章、红色水印（含部分去色残留）。
4. 【斜向淡色水印可忽略】「严禁外传」「违者必究」「(CHHT)」「CHHT」「内部资料」等斜向半透明水印**不要**当作 stamp_noise，也不要因水印挡图而漏出 illustration；仍框其下方插图/地图。
5. page_number：页脚或页眉单独的印刷页码数字。
6. 所有 bbox 为相对整页宽高的 0~1 小数，须满足 0≤x_start<x_end≤1、0≤y_start<y_end≤1。
7. 不要返回正文汉字段落、对话气泡文字区（由 OCR 处理）。
8. 按从上到下、从左到右排序；同一插图只输出一个紧框。
9. **2×2 工序/步骤示意图**（如制陶「制模/制范/熔铸/修整」四格）：须输出 **4 个独立** illustration，每格一块；禁止把整页四宫格并成一个大框。
10. 【几何附图·必出 illustration】三角形/圆/坐标系/角度标注图：整幅图形一个 illustration；边长/角度数字可由文字 OCR 另出，但图形本体必须框为 illustration。
11. 【过程示意图·必出 illustration】分式化简箭头流程、化学电子转移箭头示意、多步推导流程框：整段流程一个或多个 illustration；勿用正文大框覆盖箭头流程。"""


class LlmLayoutRegion(BaseModel):
    role: Literal["illustration", "stamp_noise", "page_number"]
    x_start: float = Field(ge=0.0, le=1.0)
    y_start: float = Field(ge=0.0, le=1.0)
    x_end: float = Field(ge=0.0, le=1.0)
    y_end: float = Field(ge=0.0, le=1.0)
    label: str = ""

    @field_validator("x_end")
    @classmethod
    def _x_end_after_start(cls, v: float, info) -> float:
        xs = info.data.get("x_start", 0.0)
        if v <= xs:
            return min(1.0, xs + 0.02)
        return v

    @field_validator("y_end")
    @classmethod
    def _y_end_after_start(cls, v: float, info) -> float:
        ys = info.data.get("y_start", 0.0)
        if v <= ys:
            return min(1.0, ys + 0.02)
        return v


class LlmPageLayout(BaseModel):
    regions: list[LlmLayoutRegion] = Field(default_factory=list)


def atom_layout_llm_enabled() -> bool:
    import os

    from dotenv import load_dotenv

    from ...repo_paths import app_root
    load_dotenv(app_root() / ".env", encoding="utf-8-sig", override=True)
    raw = os.getenv("ATOM_LAYOUT_LLM", "1").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return llm_enabled()


def _cache_path(image_path: Path) -> Path:
    data = image_path.read_bytes()
    key = hashlib.sha256(data).hexdigest()
    return _cache_dir() / f"{key}_v{PAGE_LAYOUT_PROMPT_VERSION}.json"


def _bgr_to_jpeg_data_url(bgr: Any, *, quality: int = 82) -> str:
    import cv2

    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("无法编码页面 JPEG")
    b64 = base64.standard_b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _image_to_data_url(image_path: Path, *, max_side: int = 1600) -> str:
    import cv2

    bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"无法读取 {image_path}")
    h, w = bgr.shape[:2]
    side = max(h, w)
    if side > max_side:
        scale = max_side / side
        bgr = cv2.resize(
            bgr,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )
    return _bgr_to_jpeg_data_url(bgr)


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    data = json.loads(raw)
    return LlmPageLayout.model_validate(data).model_dump()


def _invoke_layout_llm(image_url: str) -> LlmPageLayout:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(max_tokens=2048, model=llm_vision_model(), timeout=90.0)
    messages = [
        SystemMessage(content=PAGE_LAYOUT_SYSTEM),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        "请分析【待识别页】，输出 illustration / stamp_noise / page_number 区域的 bbox；"
                        "每个 illustration 的 label 必填（5~15 字主题描述）。"
                        "务必框出地图/示意图/资料袋内嵌配图；忽略斜向淡色水印，勿因图内地名标签而漏检。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        ),
    ]
    try:
        structured = llm.with_structured_output(LlmPageLayout)
        return structured.invoke(messages)
    except Exception as exc:
        logger.warning("LLM structured page layout failed, fallback JSON: %s", exc)
        msg = llm.invoke(messages)
        raw = getattr(msg, "content", str(msg))
        data = _parse_json_fallback(raw)
        return LlmPageLayout.model_validate(data)


def extract_page_layout(
    image_path: Path,
    *,
    force_refresh: bool = False,
    page_bgr: Any | None = None,
) -> LlmPageLayout | None:
    """调用视觉 LLM 分析页面版面；命中磁盘缓存则跳过请求。"""
    if not atom_layout_llm_enabled():
        return None
    if not image_path.is_file():
        return None

    cache_file = _cache_path(image_path)
    if not force_refresh and cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return LlmPageLayout.model_validate(data)
        except Exception:
            pass

    if page_bgr is not None:
        image_url = _bgr_to_jpeg_data_url(page_bgr)
    else:
        image_url = _image_to_data_url(image_path)

    layout = _invoke_layout_llm(image_url)
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            layout.model_dump_json(indent=0),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("page layout cache write failed: %s", exc)

    logger.info(
        "page layout %s: %d regions, model=%s",
        image_path.name,
        len(layout.regions),
        llm_vision_model(),
    )
    return layout


def layout_region_to_atom(region: LlmLayoutRegion, page_num: int, *, idx: int) -> dict[str, Any]:
    label = (region.label or "").strip()
    content = f"[插图] {label}"[:500] if label else "[插图]"
    return {
        "atom_id": f"A{page_num:03d}-llm-{idx:03d}",
        "atom_type": "image",
        "page": page_num,
        "x_start": round(region.x_start, 4),
        "y_start": round(region.y_start, 4),
        "x_end": round(region.x_end, 4),
        "y_end": round(region.y_end, 4),
        "content": content,
        "ocr_text": "",
        "parent_block_id": None,
        "bound_cw_pgs": [],
        "is_locked": False,
        # 供图片阶段过滤识别：勿被资料袋正文大框误杀
        "detect_source": "layout_llm",
    }


def layout_regions_to_image_atoms(
    layout: LlmPageLayout, page_num: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (illustration_atoms, exclude_regions_as_unit_dicts)。"""
    images: list[dict[str, Any]] = []
    excludes: list[dict[str, Any]] = []
    ill_idx = 0
    for region in layout.regions:
        unit = {
            "x_start": round(region.x_start, 4),
            "y_start": round(region.y_start, 4),
            "x_end": round(region.x_end, 4),
            "y_end": round(region.y_end, 4),
        }
        if region.role == "illustration":
            ill_idx += 1
            images.append(layout_region_to_atom(region, page_num, idx=ill_idx))
        elif region.role in ("stamp_noise", "page_number"):
            excludes.append(unit)
    return images, excludes


ILLUSTRATION_NAME_SYSTEM = """你是 K12 教材插图命名助手。根据裁剪出的插图画面，用 5~15 个汉字概括主题。
只输出主题短语本身：不要「插图」前缀、不要标点、不要解释、不要编号。"""


def illustration_theme_from_content(content: str) -> str:
    """从「[插图] 主题」取出主题；仅占位符时返回空串。"""
    text = (content or "").strip()
    for prefix in ("[插图]", "[image]", "[图片]"):
        if text.lower().startswith(prefix.lower()):
            rest = text[len(prefix) :].strip()
            if rest.startswith("]"):
                rest = rest[1:].strip()
            return rest[:80]
    if text in ("插图", "image", "图片"):
        return ""
    return text[:80] if text else ""


def is_unnamed_illustration_atom(atom: dict[str, Any]) -> bool:
    if str(atom.get("atom_type") or "") not in ("image", "figure"):
        return False
    raw = str(atom.get("content") or atom.get("ocr_text") or "").strip()
    return not bool(illustration_theme_from_content(raw))


def _sanitize_illustration_theme(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text).strip()
    text = text.splitlines()[0].strip() if text else ""
    for prefix in ("插图：", "插图:", "主题：", "主题:", "标签：", "标签:", "图：", "图:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    text = re.sub(r"^\[?(?:插图|image|图片)\]?\s*", "", text, flags=re.I).strip()
    text = re.sub(r"[。！？；;,.，、]+$", "", text).strip()
    # 过长则截到约 15 字边界
    if len(text) > 20:
        text = text[:15].rstrip()
    if len(text) < 2:
        return ""
    return text[:80]


def _crop_atom_jpeg_data_url(page_bgr: Any, atom: dict[str, Any], *, pad: float = 0.01) -> str:
    import cv2

    h, w = page_bgr.shape[:2]
    xs = max(0.0, float(atom.get("x_start") or 0.0) - pad)
    ys = max(0.0, float(atom.get("y_start") or 0.0) - pad)
    xe = min(1.0, float(atom.get("x_end") or 1.0) + pad)
    ye = min(1.0, float(atom.get("y_end") or 1.0) + pad)
    x0, y0 = int(xs * w), int(ys * h)
    x1, y1 = max(x0 + 2, int(xe * w)), max(y0 + 2, int(ye * h))
    crop = page_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        raise ValueError("空裁剪区")
    # 过小裁剪放大，便于视觉模型辨认
    ch, cw = crop.shape[:2]
    if max(ch, cw) < 96:
        scale = 96 / max(ch, cw)
        crop = cv2.resize(
            crop,
            (max(2, int(cw * scale)), max(2, int(ch * scale))),
            interpolation=cv2.INTER_CUBIC,
        )
    return _bgr_to_jpeg_data_url(crop, quality=85)


def _invoke_illustration_name_llm(image_url: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(max_tokens=64, model=llm_vision_model(), timeout=45.0)
    messages = [
        SystemMessage(content=ILLUSTRATION_NAME_SYSTEM),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": "请为这张教材插图写出 5~15 字中文主题。",
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        ),
    ]
    msg = llm.invoke(messages)
    raw = getattr(msg, "content", str(msg))
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(block))
        raw = "".join(parts)
    return _sanitize_illustration_theme(str(raw))


def ensure_illustration_atom_labels(
    atoms: list[dict[str, Any]],
    *,
    page_bgr: Any | None,
    image_path: Path | None = None,
    require: bool | None = None,
) -> list[dict[str, Any]]:
    """版面 LLM 开启时：凡仍无主题的插图原子，裁剪后强制视觉命名。

    require=True（默认随 ATOM_LAYOUT_LLM）时，若仍有未命名则抛错，禁止偷懒落成「[插图]」。
    """
    if require is None:
        require = atom_layout_llm_enabled()
    if not require:
        return atoms

    if page_bgr is None and image_path is not None and image_path.is_file():
        try:
            import cv2

            page_bgr = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        except Exception:
            page_bgr = None
    if page_bgr is None:
        unnamed_n = sum(1 for a in atoms if is_unnamed_illustration_atom(a))
        if unnamed_n:
            raise RuntimeError(
                f"插图命名失败：无法读取页图，仍有 {unnamed_n} 个未命名插图"
            )
        return atoms

    for atom in atoms:
        if not is_unnamed_illustration_atom(atom):
            continue
        aid = str(atom.get("atom_id") or "?")
        label = ""
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                url = _crop_atom_jpeg_data_url(page_bgr, atom)
                label = _invoke_illustration_name_llm(url)
                if label:
                    break
            except Exception as exc:
                last_err = exc
                logger.warning(
                    "illustration name attempt %s failed atom=%s: %s",
                    attempt + 1,
                    aid,
                    exc,
                )
        if not label:
            detail = f"：{last_err}" if last_err else ""
            raise RuntimeError(
                f"插图命名未完成：原子 {aid} 仍无主题描述{detail}"
            )
        atom["content"] = f"[插图] {label}"[:500]
        logger.info("illustration named atom=%s label=%s", aid, label)
    return atoms
