"""阶段 A ORM 模型（对齐 table.md）。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db

if TYPE_CHECKING:
    from .phase_b import Block, BlockMatch, ReuseReport


def _uuid() -> str:
    return str(uuid.uuid4())


class FileBlob(db.Model):
    __tablename__ = "file_blobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )


class Volume(db.Model):
    __tablename__ = "volumes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    volume_code: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    subject: Mapped[str] = mapped_column(String(32), nullable=False, default="科学")
    edition: Mapped[str] = mapped_column(String(32), nullable=False)
    grade: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    semester: Mapped[str] = mapped_column(String(8), nullable=False)
    book_type: Mapped[str] = mapped_column(String(8), nullable=False)  # old | new
    display_title: Mapped[str | None] = mapped_column(String(256))
    version_label: Mapped[str | None] = mapped_column(String(256))
    blob_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("file_blobs.id"))
    preview_blob_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("file_blobs.id"), nullable=True
    )
    parse_status: Mapped[str | None] = mapped_column(String(16), default="pending")
    parse_error: Mapped[str | None] = mapped_column(Text)
    parse_suggestions_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lessons: Mapped[list["Lesson"]] = relationship(back_populates="volume", cascade="all, delete-orphan")
    draft_pdfs: Mapped[list["VolumeDraftPdf"]] = relationship(
        back_populates="volume", cascade="all, delete-orphan"
    )


class VolumeDraftPdf(db.Model):
    """册次不完整修订版 PDF（可保留多份）。"""

    __tablename__ = "volume_draft_pdfs"
    __table_args__ = (UniqueConstraint("volume_id", "blob_id", name="uk_volume_draft_blob"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    volume_id: Mapped[str] = mapped_column(String(36), ForeignKey("volumes.id"), nullable=False, index=True)
    blob_id: Mapped[str] = mapped_column(String(36), ForeignKey("file_blobs.id"), nullable=False)
    upload_filename: Mapped[str | None] = mapped_column(String(512))
    label: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    volume: Mapped["Volume"] = relationship(back_populates="draft_pdfs")
    blob: Mapped["FileBlob"] = relationship()


class DiffLessonPair(db.Model):
    """教材对比册级粗分：新课时 ↔ 旧课时（可确认 / 改对）。"""

    __tablename__ = "diff_lesson_pairs"
    __table_args__ = (
        UniqueConstraint(
            "old_volume_id", "new_volume_id", "new_lesson_id", name="uk_diff_pairs_new"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    old_volume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("volumes.id"), nullable=False, index=True
    )
    new_volume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("volumes.id"), nullable=False, index=True
    )
    new_lesson_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lessons.id"), nullable=False, index=True
    )
    old_lesson_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lessons.id"), nullable=True
    )
    match_method: Mapped[str] = mapped_column(String(32), nullable=False, default="lesson_no")
    pair_status: Mapped[str] = mapped_column(String(24), nullable=False, default="suggested")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DiffPageAtomSnapshot(db.Model):
    """教材对比：单侧某一 PDF 页的 OCR 原子快照（可跨页调取）。"""

    __tablename__ = "diff_page_atom_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "volume_id",
            "page_1",
            "pdf_source",
            "preview_blob_id",
            name="uk_diff_page_atoms",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    volume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("volumes.id"), nullable=False, index=True
    )
    volume_code: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    page_1: Mapped[int] = mapped_column(Integer, nullable=False)
    pdf_source: Mapped[str] = mapped_column(String(8), nullable=False, default="full")
    preview_blob_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    atoms_json: Mapped[dict | list] = mapped_column(JSON, nullable=False)
    text_ocr_done: Mapped[bool] = mapped_column(nullable=False, default=False)
    image_ocr_done: Mapped[bool] = mapped_column(nullable=False, default=False)
    atoms_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class DiffPageCompare(db.Model):
    """教材对比：某一旧新页对的文字/图片比对结果。"""

    __tablename__ = "diff_page_compares"
    __table_args__ = (
        UniqueConstraint(
            "old_volume_id",
            "new_volume_id",
            "old_page",
            "new_page",
            "new_pdf_source",
            "preview_blob_id",
            name="uk_diff_page_cmp",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    old_volume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("volumes.id"), nullable=False, index=True
    )
    new_volume_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("volumes.id"), nullable=False, index=True
    )
    old_code: Mapped[str] = mapped_column(String(48), nullable=False)
    new_code: Mapped[str] = mapped_column(String(48), nullable=False)
    old_page: Mapped[int] = mapped_column(Integer, nullable=False)
    new_page: Mapped[int] = mapped_column(Integer, nullable=False)
    new_pdf_source: Mapped[str] = mapped_column(String(8), nullable=False, default="full")
    preview_blob_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    text_compare: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    image_compare: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    text_compare_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_compare_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Lesson(db.Model):
    __tablename__ = "lessons"
    __table_args__ = (
        UniqueConstraint("volume_id", "unit_no", "lesson_no", name="uk_lessons_volume_unit"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    volume_id: Mapped[str] = mapped_column(String(36), ForeignKey("volumes.id"), nullable=False, index=True)
    lesson_uid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    unit_no: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    unit_title: Mapped[str] = mapped_column(String(256), nullable=False)
    lesson_no: Mapped[str] = mapped_column(String(16), nullable=False)
    lesson_name: Mapped[str] = mapped_column(String(256), nullable=False)
    sort_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    page_range_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    body_text: Mapped[str | None] = mapped_column(Text)
    old_course_id: Mapped[str | None] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(SmallInteger)
    import_batch: Mapped[str | None] = mapped_column(String(32))
    slides_fetch_status: Mapped[str | None] = mapped_column(String(16), default="not_uploaded")
    blocks_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    volume: Mapped["Volume"] = relationship(back_populates="lessons")
    slides: Mapped[list["CoursewareSlide"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    textbook_pages: Mapped[list["LessonPage"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    textbook_atoms: Mapped[list["TextbookAtom"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    blocks: Mapped[list["Block"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan"
    )
    content_scans: Mapped[list["LessonContentScan"]] = relationship(
        back_populates="new_lesson",
        foreign_keys="LessonContentScan.new_lesson_id",
        cascade="all, delete-orphan",
    )


class LessonPage(db.Model):
    """教材按页 PNG（解析 PDF 时生成）。"""

    __tablename__ = "lesson_pages"
    __table_args__ = (
        UniqueConstraint("lesson_id", "page_index", name="uk_lesson_pages"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lesson_id: Mapped[str] = mapped_column(String(36), ForeignKey("lessons.id"), nullable=False, index=True)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_id: Mapped[str] = mapped_column(String(36), ForeignKey("file_blobs.id"), nullable=False)
    ocr_atoms_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    atom_lineage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lesson: Mapped["Lesson"] = relationship(back_populates="textbook_pages")


class CoursewareSlide(db.Model):
    __tablename__ = "courseware_slides"
    __table_args__ = (
        UniqueConstraint("lesson_id", "slide_index", name="uk_slides_lesson_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lesson_id: Mapped[str] = mapped_column(String(36), ForeignKey("lessons.id"), nullable=False, index=True)
    old_course_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slide_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    blob_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("file_blobs.id"))
    ocr_text: Mapped[str | None] = mapped_column(Text)
    fetch_status: Mapped[str | None] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lesson: Mapped["Lesson"] = relationship(back_populates="slides")


class MatchJob(db.Model):
    __tablename__ = "match_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    new_volume_id: Mapped[str] = mapped_column(String(36), ForeignKey("volumes.id"), nullable=False)
    status: Mapped[str | None] = mapped_column(String(16), default="pending")
    summary_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    matches: Mapped[list["LessonMatch"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class LessonMatch(db.Model):
    __tablename__ = "lesson_matches"
    __table_args__ = (
        UniqueConstraint("job_id", "new_lesson_id", "old_lesson_id", name="uk_lesson_matches_triple"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("match_jobs.id"), nullable=False, index=True)
    new_lesson_id: Mapped[str] = mapped_column(String(36), ForeignKey("lessons.id"), nullable=False, index=True)
    old_lesson_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("lessons.id"))
    match_tier: Mapped[str] = mapped_column(String(24), nullable=False)
    similarity_score: Mapped[float | None] = mapped_column(Float)
    old_lesson_hint: Mapped[str | None] = mapped_column(Text)
    subject: Mapped[str | None] = mapped_column(String(32))
    old_courseware_id: Mapped[str | None] = mapped_column(String(64))
    old_page_count: Mapped[int | None] = mapped_column(SmallInteger)
    match_rank: Mapped[int | None] = mapped_column("rank", SmallInteger, default=1)
    annotate_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    compare_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    compare_confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pair_review_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    pair_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    job: Mapped["MatchJob"] = relationship(back_populates="matches")
    block_matches: Mapped[list["BlockMatch"]] = relationship(
        back_populates="lesson_match", cascade="all, delete-orphan"
    )
    reuse_report: Mapped["ReuseReport | None"] = relationship(
        back_populates="lesson_match", uselist=False, cascade="all, delete-orphan"
    )
    match_signal: Mapped["LessonMatchSignal | None"] = relationship(
        back_populates="lesson_match", uselist=False, cascade="all, delete-orphan"
    )


class LessonMatchSignal(db.Model):
    """粗分 v2：lesson_matches 分项得分（一对扩展）。"""

    __tablename__ = "lesson_match_signals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lesson_match_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lesson_matches.id"), nullable=False, unique=True
    )
    title_score: Mapped[float | None] = mapped_column(Float)
    body_score: Mapped[float | None] = mapped_column(Float)
    unit_score: Mapped[float | None] = mapped_column(Float)
    context_score: Mapped[float | None] = mapped_column(Float)
    page_count_delta: Mapped[int | None] = mapped_column(SmallInteger)
    signal_source: Mapped[str] = mapped_column(String(16), nullable=False, default="rules")
    content_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    llm_reason: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lesson_match: Mapped["LessonMatch"] = relationship(back_populates="match_signal")


class LessonContentScan(db.Model):
    """建块前内容预扫描（OCR 写 lesson_pages.ocr_atoms_json，结论写本表）。"""

    __tablename__ = "lesson_content_scans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    new_lesson_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lessons.id"), nullable=False, index=True
    )
    match_job_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("match_jobs.id"))
    coarse_primary_match_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lesson_matches.id")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    ocr_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="missing_pages")
    ocr_pages_done: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    ocr_pages_total: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    suggested_pair_status: Mapped[str | None] = mapped_column(String(24))
    coarse_agreement: Mapped[str | None] = mapped_column(String(24))
    lesson_body_similarity: Mapped[float | None] = mapped_column(Float)
    recommended_old_lesson_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lessons.id")
    )
    cross_lesson_flag: Mapped[bool] = mapped_column(default=False, nullable=False)
    summary_text: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    new_lesson: Mapped["Lesson"] = relationship(
        back_populates="content_scans",
        foreign_keys=[new_lesson_id],
    )
    hits: Mapped[list["LessonContentScanHit"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan"
    )


class LessonContentScanHit(db.Model):
    """预扫描块级/课级命中预览。"""

    __tablename__ = "lesson_content_scan_hits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lesson_content_scans.id"), nullable=False, index=True
    )
    hit_level: Mapped[str] = mapped_column(String(8), nullable=False)
    old_lesson_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lessons.id"), nullable=False, index=True
    )
    old_block_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("blocks.id"))
    old_block_code: Mapped[str | None] = mapped_column(String(8))
    new_page_index: Mapped[int | None] = mapped_column(Integer)
    match_score: Mapped[float] = mapped_column(Float, nullable=False)
    rank_in_scan: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    is_primary_lesson: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_cross_lesson: Mapped[bool] = mapped_column(default=False, nullable=False)
    match_basis: Mapped[str | None] = mapped_column(String(32))
    excerpt: Mapped[str | None] = mapped_column(String(256))

    scan: Mapped["LessonContentScan"] = relationship(back_populates="hits")
    old_block: Mapped["Block | None"] = relationship(foreign_keys=[old_block_id])
