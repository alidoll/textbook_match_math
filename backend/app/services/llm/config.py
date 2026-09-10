"""LLM 配置（公司 ai-service OpenAI 兼容模式）。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

DEFAULT_LLM_BASE_URL = "http://ai-service.tal.com/openai-compatible/v1"


def reload_llm_env() -> None:
    """每次调用前重载 .env，避免改密钥后必须手动重启 Flask。"""
    from ...repo_paths import app_root
    load_dotenv(app_root() / ".env", encoding="utf-8-sig", override=True)


def llm_app_id() -> str:
    reload_llm_env()
    return (
        os.getenv("LLM_APP_ID", "").strip()
        or os.getenv("TAL_MLOPS_APP_ID", "").strip()
    )


def llm_api_key_secret() -> str:
    reload_llm_env()
    return (
        os.getenv("LLM_API_KEY", "").strip()
        or os.getenv("TAL_MLOPS_APP_KEY", "").strip()
        or os.getenv("DASHSCOPE_API_KEY", "").strip()
        or os.getenv("BAILIAN_API_KEY", "").strip()
    )


def llm_api_credential() -> str:
    """Bearer 令牌：AppID:APIKey（见公司 curl 示例）。"""
    secret = llm_api_key_secret()
    app_id = llm_app_id()
    if not secret:
        return ""
    if app_id and ":" not in secret:
        return f"{app_id}:{secret}"
    return secret


def llm_api_key() -> str:
    return llm_api_credential()


def dashscope_api_key() -> str:
    return llm_api_credential()


def llm_model() -> str:
    reload_llm_env()
    return os.getenv("LLM_MODEL", "").strip()


def llm_vision_model() -> str:
    """视觉任务（目录识别、原子整理等）；默认同 LLM_MODEL。"""
    reload_llm_env()
    return os.getenv("LLM_VISION_MODEL", "").strip() or llm_model()


def llm_atom_curate_model() -> str:
    """教材原子整理 Agent；默认同 LLM_VISION_MODEL。"""
    reload_llm_env()
    return os.getenv("LLM_ATOM_CURATE_MODEL", "").strip() or llm_vision_model()


def atom_curate_heuristics_mode() -> str:
    """原子整理规则强度：minimal=大模型为主（默认）；full=规则补并/撤销（湘科版调优）。"""
    reload_llm_env()
    raw = os.getenv("ATOM_CURATE_HEURISTICS", "minimal").strip().lower()
    if raw in ("full", "1", "true", "yes", "on"):
        return "full"
    return "minimal"


def llm_block_seed_model() -> str:
    """旧侧 AI 建块（须对照课件/教材截图）；默认同 LLM_VISION_MODEL。"""
    reload_llm_env()
    return os.getenv("LLM_BLOCK_SEED_MODEL", "").strip() or llm_vision_model()


def llm_new_block_mirror_model() -> str:
    """新库对照旧块建块（豆包视觉）；默认同 LLM_BLOCK_SEED_MODEL。"""
    reload_llm_env()
    return (
        os.getenv("NEW_BLOCK_MIRROR_LLM_MODEL", "").strip()
        or llm_block_seed_model()
    )


def new_block_mirror_llm_enabled() -> bool:
    """新库 old_mirror 建块是否走豆包视觉分配。"""
    reload_llm_env()
    if os.getenv("NEW_BLOCK_MIRROR_LLM", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return False
    if os.getenv("LLM_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return False
    return bool(llm_api_credential()) and bool(llm_new_block_mirror_model())


def llm_timeout() -> float:
    """通用 LLM 请求超时（秒）。"""
    reload_llm_env()
    try:
        return float(os.getenv("LLM_TIMEOUT", "120"))
    except ValueError:
        return 120.0


def llm_atom_curate_timeout() -> float:
    """教材原子整理视觉 LLM 超时（秒）；默认同 LLM_TIMEOUT。"""
    reload_llm_env()
    raw = os.getenv("LLM_ATOM_CURATE_TIMEOUT", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return llm_timeout()


def new_block_mirror_llm_timeout() -> float:
    reload_llm_env()
    try:
        return float(os.getenv("NEW_BLOCK_MIRROR_LLM_TIMEOUT", "90"))
    except ValueError:
        return 90.0


def dual_track_textbook_llm_enabled() -> bool:
    """双轨实验 ① 教材轨是否走豆包按页分配。"""
    reload_llm_env()
    if os.getenv("DUAL_TRACK_LLM", "1").strip().lower() in ("0", "false", "no"):
        return False
    return new_block_mirror_llm_enabled()


def llm_course_match_model() -> str:
    """粗分（新课↔旧课）语义判定；默认同 LLM_MODEL。"""
    reload_llm_env()
    return os.getenv("LLM_COURSE_MATCH_MODEL", "").strip() or llm_model()


def llm_course_match_mode() -> str:
    """粗分策略：batch=整册目录一次对照；hybrid=逐课+15候选；rules=纯规则。"""
    reload_llm_env()
    raw = os.getenv("LLM_COURSE_MATCH_MODE", "batch").strip().lower()
    if raw in ("hybrid", "per_lesson", "per-lesson"):
        return "hybrid"
    if raw in ("rules", "rule", "0"):
        return "rules"
    return "batch"


def llm_course_match_batch_timeout() -> float:
    """整册 batch 单次 LLM 超时（秒）。"""
    reload_llm_env()
    try:
        return float(os.getenv("LLM_COURSE_MATCH_BATCH_TIMEOUT", "120"))
    except ValueError:
        return 120.0


def llm_course_match_enabled() -> bool:
    """粗分是否启用大模型（batch / hybrid）。"""
    reload_llm_env()
    if llm_course_match_mode() == "rules":
        return False
    if os.getenv("LLM_COURSE_MATCH_ENABLED", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return False
    if os.getenv("LLM_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return False
    return bool(llm_api_credential()) and bool(llm_course_match_model())


def llm_course_match_timeout() -> float:
    """粗分单次 LLM 请求超时（秒）。"""
    reload_llm_env()
    try:
        return float(os.getenv("LLM_COURSE_MATCH_TIMEOUT", "45"))
    except ValueError:
        return 45.0


def llm_prescan_model() -> str:
    """课时预判断（annotate ⑤）；默认同 LLM_COURSE_MATCH_MODEL。"""
    reload_llm_env()
    return os.getenv("LLM_PRESCAN_MODEL", "").strip() or llm_course_match_model()


def llm_prescan_enabled() -> bool:
    reload_llm_env()
    if os.getenv("LLM_PRESCAN_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return False
    if os.getenv("LLM_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return False
    return bool(llm_api_credential()) and bool(llm_prescan_model())


def llm_prescan_timeout() -> float:
    reload_llm_env()
    try:
        return float(os.getenv("LLM_PRESCAN_TIMEOUT", "60"))
    except ValueError:
        return 60.0


def llm_course_match_workers() -> int:
    """粗分并行 LLM 请求数。"""
    reload_llm_env()
    try:
        n = int(os.getenv("LLM_COURSE_MATCH_WORKERS", "4"))
    except ValueError:
        n = 4
    return max(1, min(n, 8))


def llm_base_url() -> str:
    reload_llm_env()
    return os.getenv("LLM_BASE_URL", "").strip() or DEFAULT_LLM_BASE_URL


def _uses_api_key_header() -> bool:
    """仅当显式 LLM_AUTH_STYLE=api-key 时使用 api-key 头；公司默认 Bearer。"""
    reload_llm_env()
    return os.getenv("LLM_AUTH_STYLE", "").strip().lower() == "api-key"


def llm_auth_headers() -> dict[str, str]:
    credential = llm_api_credential()
    if not credential:
        return {}
    if _uses_api_key_header():
        return {"api-key": credential}
    return {"Authorization": f"Bearer {credential}"}


def llm_default_headers() -> dict[str, str]:
    return llm_auth_headers()


def llm_enabled() -> bool:
    reload_llm_env()
    if os.getenv("LLM_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        return False
    return bool(llm_api_credential()) and bool(llm_model())


def llm_temperature() -> float:
    try:
        return float(os.getenv("LLM_TEMPERATURE", "0.2"))
    except ValueError:
        return 0.2


def llm_reasoning_mode() -> str | None:
    """豆包等模型的 reasoning.mode，如 enabled；未设则不传。"""
    reload_llm_env()
    raw = os.getenv("LLM_REASONING_MODE", "").strip().lower()
    if not raw or raw in ("0", "false", "no", "off", "disabled"):
        return None
    return raw


def llm_reasoning_effort() -> str | None:
    """reasoning.effort，如 low / medium / high。"""
    reload_llm_env()
    raw = os.getenv("LLM_REASONING_EFFORT", "").strip().lower()
    return raw or None


def llm_extra_request_body() -> dict[str, Any]:
    """
    网关扩展字段（透传 chat/completions body）。
    豆包 doubao-seed-2.0-pro 示例：reasoning + stream_options。
    """
    reload_llm_env()
    extra: dict[str, Any] = {}
    mode = llm_reasoning_mode()
    if mode:
        reasoning: dict[str, str] = {"mode": mode}
        effort = llm_reasoning_effort()
        if effort:
            reasoning["effort"] = effort
        extra["reasoning"] = reasoning
    if os.getenv("LLM_STREAM_INCLUDE_USAGE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        extra["stream_options"] = {"include_usage": True}
    return extra


def build_chat_openai(
    *,
    max_tokens: int = 768,
    model: str | None = None,
    include_reasoning: bool = True,
    timeout: float | None = None,
) -> ChatOpenAI:
    """
    创建 LangChain ChatOpenAI 客户端。
    鉴权：默认 Bearer AppID:Key；LLM_AUTH_STYLE=api-key 时用 api-key 头（见公司 curl）。
    """
    from langchain_openai import ChatOpenAI

    credential = llm_api_credential()
    kwargs: dict[str, Any] = {
        "model": model or llm_model(),
        "base_url": llm_base_url(),
        "temperature": llm_temperature(),
        "max_tokens": max_tokens,
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    if include_reasoning:
        extra_body = llm_extra_request_body()
        if extra_body:
            kwargs["extra_body"] = extra_body
    if _uses_api_key_header():
        kwargs["api_key"] = "unused"
        kwargs["default_headers"] = {"api-key": credential}
    else:
        # OpenAI SDK 会发 Authorization: Bearer {api_key}
        kwargs["api_key"] = credential
    return ChatOpenAI(**kwargs)


def lib_semantic_block_enabled() -> bool:
    """教材库非科学语义建块是否启用。"""
    reload_llm_env()
    raw = os.getenv("LIB_SEMANTIC_BLOCK", "1").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return True


def lib_semantic_cross_page_enabled() -> bool:
    """教材库语义建块课级跨页桥接是否启用。"""
    reload_llm_env()
    raw = os.getenv("LIB_SEMANTIC_CROSS_PAGE", "1").strip().lower()
    return raw not in ("0", "false", "no")


def lib_figure_part_role_llm_enabled() -> bool:
    """插图散件绑定前是否用 LLM 标注 text_role。"""
    reload_llm_env()
    raw = os.getenv("LIB_FIGURE_PART_ROLE_LLM", "1").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return True


def lib_table_grid_llm_enabled() -> bool:
    """教材库表格结构化是否启用 vision LLM。"""
    reload_llm_env()
    raw = os.getenv("LIB_TABLE_GRID_LLM", "1").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    return True


def lib_page_ocr_workers() -> int:
    """整课/整册 OCR 页级并行度（同课多页同时跑；1=串行）。"""
    reload_llm_env()
    try:
        n = int(os.getenv("LIB_PAGE_OCR_WORKERS", "3").strip() or "3")
    except ValueError:
        n = 3
    return max(1, min(n, 8))


def lib_semantic_block_workers() -> int:
    """语义建块页级 suggest 并行度（1=串行）。"""
    reload_llm_env()
    try:
        n = int(os.getenv("LIB_SEMANTIC_BLOCK_WORKERS", "3").strip() or "3")
    except ValueError:
        n = 3
    return max(1, min(n, 8))


def lib_ocr_audit_image_parallel() -> bool:
    """文字主 OCR 后：公式/拼音校对与图片 OCR 并行（B）。"""
    reload_llm_env()
    raw = os.getenv("LIB_OCR_AUDIT_IMAGE_PARALLEL", "1").strip().lower()
    return raw not in ("0", "false", "no")


def lib_volume_ocr_pipeline_blocks() -> bool:
    """整册：本课建块时预跑下一课 OCR（D）。"""
    reload_llm_env()
    raw = os.getenv("LIB_VOLUME_OCR_PIPELINE_BLOCKS", "1").strip().lower()
    return raw not in ("0", "false", "no")


def lib_semantic_block_fallback() -> str:
    """LLM 不可用时降级策略：error / figure / column。"""
    reload_llm_env()
    raw = (os.getenv("LIB_SEMANTIC_BLOCK_FALLBACK") or "error").strip().lower()
    if raw in ("figure", "column", "error"):
        return raw
    return "error"
