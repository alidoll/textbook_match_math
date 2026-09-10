"""file_blobs 入库（按 content_hash 去重）。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..extensions import db
from ..models import FileBlob
from ..repo_paths import repo_root

# 超过此大小不写 MySQL LONGBLOB（整册 PDF 走 storage_path）
_DB_INLINE_MAX_BYTES = 16 * 1024 * 1024


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _rel_storage_path(abs_path: Path) -> str:
    return str(abs_path.resolve().relative_to(repo_root())).replace("\\", "/")


def read_blob_bytes(blob: FileBlob) -> bytes:
    if blob.content is not None:
        return bytes(blob.content)
    if blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            return path.read_bytes()
    raise FileNotFoundError(f"无法读取 blob {blob.id}：无 content 且磁盘文件不存在")


def blob_content_available(blob: FileBlob | None) -> bool:
    """库里有登记且（内联 content 或 storage_path 文件存在）才可直接 /api/file-blobs 读取。"""
    if not blob:
        return False
    if blob.content is not None:
        return True
    if blob.storage_path:
        return (repo_root() / blob.storage_path).is_file()
    return False


def blob_id_available(blob_id: str | None) -> bool:
    bid = (blob_id or "").strip()
    if not bid:
        return False
    return blob_content_available(FileBlob.query.get(bid))


def store_blob(
    *,
    content: bytes,
    mime_type: str,
    disk_path: Path | None = None,
    storage_path: Path | None = None,
    force_new: bool = False,
) -> tuple[FileBlob, bool]:
    """
    写入或复用已有 blob。
    - 提供 disk_path 且文件较大：仅记 storage_path，不把字节写入 MySQL
    - 小文件：写入 content 列
    - force_new=True：即使内容相同也新建（课时页图避免串课目录）
    """
    digest = sha256_hex(content)
    use_disk = disk_path is not None and disk_path.is_file()
    path_for_db = storage_path or disk_path
    rel_path = None
    if path_for_db is not None:
        try:
            rel_path = _rel_storage_path(path_for_db)
        except ValueError:
            rel_path = None

    # force_new：hash 混入路径，避开「同图不同课」串目录；同路径重跑则复用
    if force_new:
        mix = rel_path or (str(path_for_db) if path_for_db else digest)
        content_hash = sha256_hex(digest.encode("ascii") + b"|" + mix.encode("utf-8"))
    else:
        content_hash = digest

    existing = FileBlob.query.filter_by(content_hash=content_hash).first()
    if existing:
        if force_new and rel_path and existing.storage_path != rel_path:
            existing.storage_path = rel_path
        return existing, False

    if use_disk:
        inline = None
    elif len(content) >= _DB_INLINE_MAX_BYTES:
        raise ValueError(
            f"文件约 {len(content) // (1024 * 1024)}MB，超过 MySQL 内联上限；请先落盘再登记"
        )
    else:
        inline = content

    blob = FileBlob(
        content_hash=content_hash,
        content=inline,
        storage_path=rel_path if use_disk else None,
        mime_type=mime_type,
        size_bytes=len(content),
    )
    db.session.add(blob)
    db.session.flush()
    return blob, True


def store_blob_at_path(*, abs_path: Path, mime_type: str) -> tuple[FileBlob, bool]:
    """从已落盘文件登记 blob（整册 PDF 上传用）。"""
    data = abs_path.read_bytes()
    return store_blob(content=data, mime_type=mime_type, disk_path=abs_path)
