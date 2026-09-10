"""file_blobs 文件下载。"""
from __future__ import annotations

from pathlib import Path

from flask import Response, send_file

from ..extensions import db
from ..models import FileBlob
from ..repo_paths import repo_root
from . import api_bp


@api_bp.get("/file-blobs/<blob_id>")
def serve_file_blob(blob_id: str):
    blob = db.session.get(FileBlob, blob_id)
    if not blob:
        return Response("未找到文件", status=404)

    if blob.storage_path:
        path = repo_root() / blob.storage_path
        if path.is_file():
            resp = send_file(path, mimetype=blob.mime_type, download_name=path.name)
            resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
            return resp

    if blob.content is not None:
        resp = Response(bytes(blob.content), mimetype=blob.mime_type)
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
        return resp

    return Response("文件内容不可用", status=404)
