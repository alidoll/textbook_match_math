"""同一次 PDF 解析内复用大模型目录结果（含页码提示）；落盘避免重启丢失。"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..repo_paths import base_data_dir
from ..services.blobs import sha256_hex
from .pdf_toc import TocEntry

_log = logging.getLogger(__name__)

_cache: dict[str, list[TocEntry]] = {}
_path_content_hash: dict[str, str] = {}

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def normalize_content_hash(content_hash: str) -> str:
    h = content_hash.strip().lower()
    if not _SHA256_HEX.fullmatch(h):
        raise ValueError(f"content_hash 须为 64 位十六进制 SHA256，收到: {content_hash!r}")
    return h


def resolve_pdf_content_hash(pdf_path: Path, content_hash: str | None = None) -> str:
    """与 file_blobs.content_hash 对齐；同内容 PDF 复用同一缓存键。"""
    if content_hash:
        return normalize_content_hash(content_hash)
    resolved = pdf_path.resolve()
    path_key = str(resolved)
    cached = _path_content_hash.get(path_key)
    if cached:
        return cached
    digest = sha256_hex(resolved.read_bytes())
    _path_content_hash[path_key] = digest
    return digest


def _cache_key(pdf_path: Path, *, content_hash: str | None = None) -> str:
    return resolve_pdf_content_hash(pdf_path, content_hash)


def _disk_file(cache_key: str) -> Path:
    basename = cache_key[:32]
    return base_data_dir() / "cache" / "llm_toc" / f"{basename}.json"


def _save_disk(cache_key: str, entries: list[TocEntry]) -> None:
    path = _disk_file(cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "unit_norm": e.unit_norm,
            "title_raw": e.title_raw,
            "page_1": e.page_1,
            "match_key": e.match_key,
        }
        for e in entries
    ]
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _load_disk(cache_key: str) -> list[TocEntry] | None:
    path = _disk_file(cache_key)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [
            TocEntry(
                unit_norm=str(x["unit_norm"]),
                title_raw=str(x["title_raw"]),
                page_1=int(x["page_1"]),
                match_key=str(x["match_key"]),
            )
            for x in raw
        ]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _log.warning("读取 LLM 目录缓存失败 %s: %s", path.name, exc)
        return None


def _unlink_disk_artifacts(cache_key: str) -> None:
    base = _disk_file(cache_key)
    for path in (
        base,
        base.with_suffix(".catalog.json"),
        base.with_suffix(".offset.json"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def put_llm_toc_entries(
    pdf_path: Path,
    entries: list[TocEntry],
    *,
    content_hash: str | None = None,
) -> None:
    if not entries:
        return
    key = _cache_key(pdf_path, content_hash=content_hash)
    _cache[key] = list(entries)
    try:
        _save_disk(key, entries)
    except OSError as exc:
        _log.warning("写入 LLM 目录缓存失败: %s", exc)


def get_llm_toc_entries(
    pdf_path: Path,
    *,
    content_hash: str | None = None,
) -> list[TocEntry] | None:
    key = _cache_key(pdf_path, content_hash=content_hash)
    if key in _cache:
        return _cache[key]
    loaded = _load_disk(key)
    if loaded:
        _cache[key] = loaded
    return loaded


def get_llm_catalog_rows(
    pdf_path: Path,
    *,
    content_hash: str | None = None,
) -> list[dict[str, Any]] | None:
    """磁盘缓存的 LLM 目录行（含单元/课时名，与 toc 同源）。"""
    path = _disk_file(_cache_key(pdf_path, content_hash=content_hash)).with_suffix(
        ".catalog.json"
    )
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list) and raw:
            return raw
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return None


def put_llm_catalog_rows(
    pdf_path: Path,
    rows: list[dict[str, Any]],
    *,
    content_hash: str | None = None,
) -> None:
    if not rows:
        return
    path = _disk_file(_cache_key(pdf_path, content_hash=content_hash)).with_suffix(
        ".catalog.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def clear_llm_toc_cache(
    pdf_path: Path | None = None,
    *,
    content_hash: str | None = None,
) -> None:
    if pdf_path is None:
        _cache.clear()
        _path_content_hash.clear()
        return
    key = _cache_key(pdf_path, content_hash=content_hash)
    _cache.pop(key, None)
    _unlink_disk_artifacts(key)
