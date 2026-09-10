"""阶段 B ORM：教材原子、教学区块、码表。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..extensions import db

if TYPE_CHECKING:
    from .phase_a import Lesson, LessonMatch


def _uuid() -> str:
    return str(uuid.uuid4())


class DictionaryEntry(db.Model):
    __tablename__ = "dictionary_entries"
    __table_args__ = (UniqueConstraint("category", "code", name="uk_dict_category_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    sort_order: Mapped[int | None] = mapped_column(SmallInteger, default=0)


class TextbookAtom(db.Model):
    __tablename__ = "textbook_atoms"
    __table_args__ = (
        UniqueConstraint("lesson_id", "atom_code", name="uk_atoms_lesson_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lesson_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lessons.id"), nullable=False, index=True
    )
    atom_code: Mapped[str] = mapped_column(String(16), nullable=False)
    page_index: Mapped[int] = mapped_column(Integer, nullable=False)
    atom_type: Mapped[str] = mapped_column(String(16), nullable=False)
    bbox_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lesson: Mapped["Lesson"] = relationship(back_populates="textbook_atoms")


class Block(db.Model):
    __tablename__ = "blocks"
    __table_args__ = (
        UniqueConstraint("lesson_id", "block_code", name="uk_blocks_lesson_code"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lesson_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lessons.id"), nullable=False, index=True
    )
    block_code: Mapped[str] = mapped_column(String(8), nullable=False)
    block_name: Mapped[str] = mapped_column(String(64), nullable=False)
    textbook_page_start: Mapped[int | None] = mapped_column(Integer)
    textbook_page_end: Mapped[int | None] = mapped_column(Integer)
    atom_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    course_slide_indices: Mapped[list | None] = mapped_column(JSON)
    sort_order: Mapped[int | None] = mapped_column(SmallInteger, default=0)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lesson: Mapped["Lesson"] = relationship(back_populates="blocks")


class BlockMatch(db.Model):
    __tablename__ = "block_matches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    lesson_match_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lesson_matches.id"), nullable=False, index=True
    )
    old_block_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("blocks.id"))
    new_block_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("blocks.id"))
    match_type: Mapped[str | None] = mapped_column(String(8), default="1:1")
    reuse_action: Mapped[str | None] = mapped_column(String(32))
    change_type: Mapped[str | None] = mapped_column(String(16))
    teacher_note: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))

    lesson_match: Mapped["LessonMatch"] = relationship(back_populates="block_matches")
    old_block: Mapped["Block | None"] = relationship(foreign_keys=[old_block_id])
    new_block: Mapped["Block | None"] = relationship(foreign_keys=[new_block_id])


class ReuseReport(db.Model):
    __tablename__ = "reuse_reports"
    __table_args__ = (
        UniqueConstraint("match_id", name="uk_reuse_reports_match"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    match_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lesson_matches.id"), nullable=False, index=True
    )
    reuse_ratio: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    lesson_match: Mapped["LessonMatch"] = relationship(back_populates="reuse_report")
