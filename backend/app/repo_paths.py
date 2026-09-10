"""仓库根目录与静态数据路径（一律相对路径，便于上云部署）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """运行时数据根目录。

    打包后（PyInstaller frozen）：exe 上级目录的 TextbookMatch-Data/ 文件夹
    （与 exe 同级但独立，PyInstaller 重新构建 dist/ 时不会丢数据）。
    开发时：仓库根目录。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # exe 在 dist/TextbookMatch/ 下，数据放在 dist/TextbookMatch-Data/
        data_dir = exe_dir.parent / "TextbookMatch-Data"
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """只读资源根目录（Flask 模板 / 静态文件 / references）。

    打包后：PyInstaller 解压目录 sys._MEIPASS。
    开发时：同 app_root()（仓库根）。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_root()


# 全局基准库（维护者放置，用户不上传）
BASE_DATA_DIR = Path("data/base_data")
DEFAULT_BENCHMARK_OLD_XLSX = BASE_DATA_DIR / "全版本旧课标全册次目录清单.xlsx"
DEFAULT_BENCHMARK_NEW_XLSX = BASE_DATA_DIR / "全版本新课标已更新册次目录清单.xlsx"
DEFAULT_SELECTION_RESOURCE_XLSX = BASE_DATA_DIR / "小学科学同步学资源数据.xlsx"

# 旧教材 PDF 磁盘镜像（主存仍在 file_blobs）
OLD_TEXTBOOK_DIR = Path("data/old-textbook")
NEW_TEXTBOOK_DIR = Path("data/new-textbook")
LESSON_PAGES_DIR = Path("data/lesson-pages")


def repo_root() -> Path:
    return app_root()


def resolve_repo_path(raw: str | Path) -> Path:
    """将相对路径解析为基于 app_root 的绝对路径；已是绝对路径则原样 resolve。"""
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    return (app_root() / p).resolve()


def base_data_dir() -> Path:
    return resolve_repo_path(BASE_DATA_DIR)


def benchmark_old_xlsx_path() -> Path:
    rel = os.getenv("BENCHMARK_OLD_XLSX", str(DEFAULT_BENCHMARK_OLD_XLSX))
    return resolve_repo_path(rel)


def benchmark_new_xlsx_path() -> Path:
    rel = os.getenv("BENCHMARK_NEW_XLSX", str(DEFAULT_BENCHMARK_NEW_XLSX))
    return resolve_repo_path(rel)


def selection_resource_xlsx() -> Path:
    rel = os.getenv("SELECTION_RESOURCE_XLSX", str(DEFAULT_SELECTION_RESOURCE_XLSX))
    return resolve_repo_path(rel)


def old_textbook_dir() -> Path:
    rel = os.getenv("OLD_TEXTBOOK_DIR", str(OLD_TEXTBOOK_DIR))
    return resolve_repo_path(rel)


def old_textbook_mirror_path(edition_label: str, volume_code: str) -> Path:
    """如 data/old-textbook/湘科版/XK-2S-OLD.pdf"""
    safe_edition = (edition_label or "未知版本").strip()
    safe_code = (volume_code or "unknown").strip()
    return old_textbook_dir() / safe_edition / f"{safe_code}.pdf"


def new_textbook_dir() -> Path:
    rel = os.getenv("NEW_TEXTBOOK_DIR", str(NEW_TEXTBOOK_DIR))
    return resolve_repo_path(rel)


def new_textbook_mirror_path(edition_label: str, volume_code: str) -> Path:
    """如 data/new-textbook/湘科版/XK-2S-NEW.pdf"""
    safe_edition = (edition_label or "未知版本").strip()
    safe_code = (volume_code or "unknown").strip()
    return new_textbook_dir() / safe_edition / f"{safe_code}.pdf"


def new_textbook_draft_mirror_path(edition_label: str, volume_code: str, draft_key: str | None = None) -> Path:
    safe_edition = (edition_label or "未知版本").strip()
    safe_code = (volume_code or "unknown").strip()
    if draft_key:
        return new_textbook_dir() / safe_edition / f"{safe_code}.draft.{draft_key}.pdf"
    return new_textbook_dir() / safe_edition / f"{safe_code}.draft.pdf"


def old_textbook_draft_mirror_path(edition_label: str, volume_code: str, draft_key: str | None = None) -> Path:
    safe_edition = (edition_label or "未知版本").strip()
    safe_code = (volume_code or "unknown").strip()
    if draft_key:
        return old_textbook_dir() / safe_edition / f"{safe_code}.draft.{draft_key}.pdf"
    return old_textbook_dir() / safe_edition / f"{safe_code}.draft.pdf"


DIFF_TEXTBOOK_DIR = Path("data/diff-textbook")


def diff_textbook_dir() -> Path:
    rel = os.getenv("DIFF_TEXTBOOK_DIR", str(DIFF_TEXTBOOK_DIR))
    return resolve_repo_path(rel)


def diff_textbook_mirror_path(volume_code: str) -> Path:
    safe_code = (volume_code or "unknown").strip()
    return diff_textbook_dir() / f"{safe_code}.pdf"


def diff_textbook_draft_mirror_path(volume_code: str, draft_key: str | None = None) -> Path:
    safe_code = (volume_code or "unknown").strip()
    if draft_key:
        return diff_textbook_dir() / f"{safe_code}.draft.{draft_key}.pdf"
    return diff_textbook_dir() / f"{safe_code}.draft.pdf"


def textbook_mirror_path(
    *,
    book_type: str,
    edition_label: str,
    volume_code: str,
    draft: bool = False,
    draft_key: str | None = None,
) -> Path:
    if book_type in ("diff_new", "diff_old"):
        return (
            diff_textbook_draft_mirror_path(volume_code, draft_key)
            if draft
            else diff_textbook_mirror_path(volume_code)
        )
    if book_type == "new":
        return (
            new_textbook_draft_mirror_path(edition_label, volume_code, draft_key)
            if draft
            else new_textbook_mirror_path(edition_label, volume_code)
        )
    return (
        old_textbook_draft_mirror_path(edition_label, volume_code, draft_key)
        if draft
        else old_textbook_mirror_path(edition_label, volume_code)
    )


def lesson_pages_dir() -> Path:
    rel = os.getenv("LESSON_PAGES_DIR", str(LESSON_PAGES_DIR))
    return resolve_repo_path(rel)


def lesson_page_png_path(volume_code: str, lesson_uid: str, page_index: int) -> Path:
    """如 data/lesson-pages/XK-2S-OLD/湘科版-2-上-old-U1-L1/p001.png"""
    safe_code = (volume_code or "unknown").strip()
    safe_uid = (lesson_uid or "unknown").replace("\\", "_").replace("/", "_")
    return lesson_pages_dir() / safe_code / safe_uid / f"p{page_index:03d}.png"
