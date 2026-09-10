"""用户在首页填写自己的 LLM API Key，写入 .env 供 reload_llm_env() 读取。"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from flask import jsonify, request

from ..repo_paths import app_root
from ..services.llm.config import llm_api_credential, llm_app_id, llm_api_key_secret
from . import api_bp

logger = logging.getLogger(__name__)

_ENV_FILE = app_root() / ".env"

# 需要写回 .env 的鉴权相关键（仅支持这些，避免被当任意配置写入工具）
_KEY_APP_ID = "TAL_MLOPS_APP_ID"
_KEY_APP_KEY = "TAL_MLOPS_APP_KEY"
_KEY_LLM_API_KEY = "LLM_API_KEY"
_KEY_LLM_APP_ID = "LLM_APP_ID"


def _read_env_lines() -> list[str]:
    if not _ENV_FILE.exists():
        return []
    return _ENV_FILE.read_text(encoding="utf-8-sig").splitlines()


def _upsert_env(lines: list[str], key: str, value: str) -> list[str]:
    """更新 .env 文本中某一行（仅非注释行）；不存在则追加。"""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    out: list[str] = []
    replaced = False
    for line in lines:
        if pattern.match(line) and not replaced:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    return out


@api_bp.get("/llm-key")
def llm_key_status():
    """返回当前是否已配置 API Key（不泄露明文，仅给掩码用于回显）。"""
    app_id = llm_app_id()
    secret = llm_api_key_secret()
    configured = bool(secret)
    return jsonify({
        "ok": True,
        "configured": configured,
        "app_id": app_id,
        # 仅返回末 4 位，方便用户确认是自己的 Key
        "key_mask": (f"…{secret[-4:]}" if len(secret) >= 8 else "…" + secret[-2:]) if secret else "",
    })


@api_bp.post("/llm-key")
def llm_key_save():
    """用户在首页提交 API Key，写入 .env。

    支持两种鉴权方式（与 .env 注释一致）：
      A) api-key 头：同时填 App ID + App Key（写入 TAL_MLOPS_APP_ID / TAL_MLOPS_APP_KEY）
      B) Bearer：填一整串 LLM_API_KEY（可含 AppID:Key）
    """
    data = request.get_json(silent=True) or {}
    app_id = (data.get("app_id") or "").strip()
    api_key = (data.get("api_key") or "").strip()

    if not api_key:
        return jsonify({"ok": False, "error": "API Key 不能为空"}), 400

    lines = _read_env_lines()

    if app_id:
        # 方式 A：api-key 头鉴权
        lines = _upsert_env(lines, _KEY_APP_ID, app_id)
        lines = _upsert_env(lines, _KEY_APP_KEY, api_key)
        # 清空可能残留的 Bearer 方式键，避免冲突
        lines = _upsert_env(lines, _KEY_LLM_API_KEY, "")
        lines = _upsert_env(lines, _KEY_LLM_APP_ID, "")
    else:
        # 方式 B：Bearer 鉴权（一整串）
        lines = _upsert_env(lines, _KEY_LLM_API_KEY, api_key)
        lines = _upsert_env(lines, _KEY_LLM_APP_ID, "")

    # 末尾保留换行
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    # 立即校验能否读回
    credential = llm_api_credential()
    if not credential:
        return jsonify({
            "ok": False,
            "configured": False,
            "error": "写入后仍读不到 credential，请检查 .env 格式",
        }), 400

    # 真正验证 Key 能连上 AI 服务（一次最轻量调用）
    verified, verify_error = _test_llm_connection()
    secret = llm_api_key_secret()
    return jsonify({
        "ok": verified,
        "configured": True,
        "verified": verified,
        "key_mask": (f"…{secret[-4:]}" if len(secret) >= 8 else "…" + secret[-2:]),
        "error": verify_error,
    })


def _test_llm_connection() -> tuple[bool, str | None]:
    """用刚写入的 Key 做一次最轻量 LLM 调用，验证连通性。

    返回 (是否成功, 失败原因)。
    """
    from langchain_core.messages import HumanMessage

    from ..services.llm.config import build_chat_openai, llm_model

    model = llm_model()
    if not model:
        return False, "LLM_MODEL 未配置，无法验证连通性"

    try:
        # max_tokens 不能太小，否则部分模型（尤其带 reasoning 的）会直接报错
        llm = build_chat_openai(max_tokens=32, include_reasoning=False, timeout=30)
        llm.invoke([HumanMessage(content="hi")])
        return True, None
    except Exception as exc:
        msg = str(exc)
        logger.warning("LLM 验证失败: %s", msg, exc_info=True)
        low = msg.lower()
        if "401" in low or "unauthorized" in low or "invalid_api_key" in low:
            return False, "API Key 无效或已过期（认证失败 401）"
        if "403" in low or "forbidden" in low:
            return False, "API Key 无权限访问该模型（403 Forbidden）"
        if "timeout" in low or "timed out" in low:
            return False, "连接超时，请确认网络可达 AI 服务（需公司网或 VPN）"
        if "connection" in low or "resolve" in low or "refused" in low:
            return False, "无法连接 AI 服务，请检查网络或 VPN"
        return False, f"验证失败：{msg[:200]}"
