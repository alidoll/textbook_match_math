"""建块流程：对照分析六步 → 建块 → 锚定 → 锁定。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc

from ....extensions import db
from ....models import (
    Block,
    Lesson,
    LessonContentScan,
    LessonContentScanHit,
    LessonMatch,
    LessonPage,
    TextbookAtom,
    Volume,
)
from ....parsers.lesson_reuse_match import body_text_similarity_score
from ...course_match.engine import build_candidate, score_components_for_pair
from ...lesson_lookup import get_lesson_by_uid
from ...old_library.annotate.atoms import extract_atoms_for_page
from .pair_review import get_primary_lesson_match

STRUCTURAL_LESSON_NO_RADIUS = 2
LESSON_SCORE_MIN = 0.12
BLOCK_SCORE_MIN = 0.15
BLOCK_NAME_PREFILTER_MIN = 0.10
BLOCK_DEEP_TOP_K = 40
SWAP_SCORE_DELTA = 0.10
CONFIRM_BODY_MIN = 0.38
HEAVY_CHANGE_BODY_MAX = 0.22


def _lesson_no_int(lesson_no: str | None) -> int:
    no = str(lesson_no or "").strip()
    return int(no) if no.isdigit() else 9999


def structural_neighbor_lessons(*, primary_old: Lesson) -> list[Lesson]:
    """主参照课 + 同单元 + 同册课序 ±2。"""
    vol = Volume.query.get(primary_old.volume_id)
    if not vol:
        return [primary_old]

    rows = (
        Lesson.query.filter_by(volume_id=vol.id)
        .order_by(Lesson.unit_no, Lesson.lesson_no)
        .all()
    )
    p_no = _lesson_no_int(primary_old.lesson_no)
    p_unit = primary_old.unit_no
    out: list[Lesson] = []
    seen: set[str] = set()
    for les in rows:
        if les.id in seen:
            continue
        same_unit = les.unit_no == p_unit
        no_gap = abs(_lesson_no_int(les.lesson_no) - p_no) <= STRUCTURAL_LESSON_NO_RADIUS
        if les.id == primary_old.id or same_unit or no_gap:
            out.append(les)
            seen.add(les.id)
    return out or [primary_old]


def lesson_ocr_status(*, lesson_id: str) -> dict:
    """本课 OCR 覆盖（不依赖 scan 记录）。"""
    les = Lesson.query.get(lesson_id)
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    done = sum(1 for lp in pages if lp.ocr_atoms_json)
    body = (les.body_text or "").strip() if les else ""
    return {
        "ocr_pages_done": done,
        "ocr_pages_total": len(pages),
        "ocr_complete": bool(pages) and done >= len(pages),
        "has_body_text": bool(body),
        "body_text_len": len(body),
    }


def lesson_ocr_phase_status(*, lesson_id: str) -> dict:
    """文字 / 图片 OCR 分阶段进度（供对照分析六段条）。"""
    base = lesson_ocr_status(lesson_id=lesson_id)
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    total = len(pages)
    text_pages = 0
    image_pages = 0
    lesson_has_images = False

    for lp in pages:
        pi = int(lp.page_index)
        types = {
            (row[0] or "text")
            for row in db.session.query(TextbookAtom.atom_type)
            .filter_by(lesson_id=lesson_id, page_index=pi)
            .all()
        }
        has_text = bool(types & {"text", "title"}) or bool(lp.ocr_atoms_json)
        has_image = "image" in types
        if lp.ocr_atoms_json:
            for item in lp.ocr_atoms_json or []:
                if not isinstance(item, dict):
                    continue
                at = (item.get("atom_type") or item.get("type") or "").strip().lower()
                if at == "image":
                    lesson_has_images = True
                    break
        if has_text:
            text_pages += 1
        if has_image:
            image_pages += 1
            lesson_has_images = True

    text_complete = total > 0 and text_pages >= total
    if base["ocr_complete"]:
        image_complete = True
    elif not lesson_has_images and text_complete:
        image_complete = True
    else:
        image_complete = total > 0 and image_pages >= total and text_complete

    return {
        **base,
        "text_pages_done": text_pages,
        "image_pages_done": image_pages,
        "text_ocr_complete": text_complete,
        "image_ocr_complete": image_complete,
        "lesson_has_images": lesson_has_images,
    }


def resolve_prescan_neighbor_lessons(
    *,
    new_les: Lesson,
    primary_match: LessonMatch | None,
    primary_old: Lesson | None,
) -> list[Lesson]:
    """粗分 rank1–4 + 结构邻居（主参照课 ± 同单元 ± 课序）。"""
    neighbor_lessons: list[Lesson] = []
    if primary_match:
        coarse_rows = (
            LessonMatch.query.filter_by(
                new_lesson_id=new_les.id,
                job_id=primary_match.job_id,
            )
            .filter(LessonMatch.old_lesson_id.isnot(None))
            .order_by(LessonMatch.match_rank)
            .limit(4)
            .all()
        )
        for m in coarse_rows:
            old = Lesson.query.get(m.old_lesson_id) if m.old_lesson_id else None
            if old:
                neighbor_lessons.extend(structural_neighbor_lessons(primary_old=old))
    elif primary_old:
        neighbor_lessons = structural_neighbor_lessons(primary_old=primary_old)

    seen: set[str] = set()
    unique: list[Lesson] = []
    for les in neighbor_lessons:
        if les.id not in seen:
            unique.append(les)
            seen.add(les.id)
    return unique


def _page_text_from_ocr(lp: LessonPage) -> str:
    parts: list[str] = []
    for item in lp.ocr_atoms_json or []:
        if not isinstance(item, dict):
            continue
        t = (item.get("content") or item.get("ocr_text") or "").strip()
        if t and not t.startswith("["):
            parts.append(t)
    return " ".join(parts)


def _page_text_from_atoms(lesson_id: str, page_index: int) -> str:
    rows = TextbookAtom.query.filter_by(
        lesson_id=lesson_id, page_index=page_index
    ).all()
    parts: list[str] = []
    for a in rows:
        t = (a.content or a.ocr_text or "").strip()
        if t and not t.startswith("["):
            parts.append(t)
    return " ".join(parts)


def refresh_lesson_body_text(*, lesson_id: str) -> str:
    """从 AI 整理原子优先，其次 OCR 快照，汇总 lessons.body_text。"""
    les = Lesson.query.get(lesson_id)
    if not les:
        return ""
    page_indices = [
        int(row[0])
        for row in (
            db.session.query(LessonPage.page_index)
            .filter_by(lesson_id=lesson_id)
            .order_by(LessonPage.page_index)
            .all()
        )
    ]
    chunks: list[str] = []
    for pi in page_indices:
        text = _page_text_from_atoms(lesson_id, pi)
        if not text:
            lp = LessonPage.query.filter_by(lesson_id=lesson_id, page_index=pi).first()
            if lp:
                text = _page_text_from_ocr(lp)
        if text:
            chunks.append(text)
    body = "\n".join(chunks).strip()
    if body:
        les.body_text = body[:80000]
        db.session.flush()
    return body


def lesson_atoms_status(*, lesson_id: str) -> dict:
    """本课原子与 AI 整理覆盖（OCR 提取 ≠ 整理完成）。"""
    from ...old_library.annotate.ai_curate_atoms import page_ai_curated

    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    page_indices = [int(p.page_index) for p in pages]
    with_atoms = 0
    pages_curated = 0
    atom_total = 0
    curate_meta_count = 0
    for pi, lp in zip(page_indices, pages):
        n = TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=pi).count()
        atom_total += n
        if n > 0:
            with_atoms += 1
        if page_ai_curated(lesson_id=lesson_id, page_index=pi, lp=lp):
            pages_curated += 1
        curate_meta_count += (
            TextbookAtom.query.filter_by(lesson_id=lesson_id, page_index=pi)
            .filter(TextbookAtom.metadata_json.isnot(None))
            .count()
        )
    pages_total = len(page_indices)
    atoms_ready = atom_total > 0
    curate_ready = pages_total > 0 and pages_curated >= pages_total
    return {
        "pages_with_atoms": with_atoms,
        "pages_total": pages_total,
        "pages_curated": pages_curated,
        "atom_count": atom_total,
        "curate_meta_count": curate_meta_count,
        "atoms_ready": atoms_ready,
        "curate_ready": curate_ready,
    }


def ocr_lesson_pages(
    *,
    lesson_uid: str,
    ocr_scope: str = "missing_pages",
    ocr_phase: str = "all",
) -> tuple[int, int, list[str]]:
    """OCR 写入 lesson_pages.ocr_atoms_json；返回 (done, total, warnings)。"""
    les = get_lesson_by_uid(lesson_uid, book_type="new")
    page_indices = [
        int(row[0])
        for row in (
            db.session.query(LessonPage.page_index)
            .filter_by(lesson_id=les.id)
            .order_by(LessonPage.page_index)
            .all()
        )
    ]
    if not page_indices:
        raise ValueError("本课尚无教材页，请先解析 PDF")

    phase = (ocr_phase or "all").strip().lower()
    if phase not in ("all", "text", "images"):
        phase = "all"

    warnings: list[str] = []
    done = 0
    for pi in page_indices:
        if ocr_scope != "lesson_all":
            if phase == "images":
                has_image = (
                    db.session.query(TextbookAtom.id)
                    .filter_by(lesson_id=les.id, page_index=pi, atom_type="image")
                    .first()
                )
                if has_image:
                    continue
            else:
                has_ocr = (
                    db.session.query(LessonPage.ocr_atoms_json)
                    .filter_by(lesson_id=les.id, page_index=pi)
                    .scalar()
                )
                if has_ocr and phase != "text":
                    continue
                if phase == "text" and has_ocr:
                    types = {
                        (row[0] or "text")
                        for row in db.session.query(TextbookAtom.atom_type)
                        .filter_by(lesson_id=les.id, page_index=pi)
                        .all()
                    }
                    if types & {"text", "title"}:
                        continue
        try:
            extract_atoms_for_page(
                lesson_uid=lesson_uid,
                page_index=pi,
                replace_page=True,
                use_ocr=True,
                fill_gaps=True,
                book_type="new",
                ocr_phase=phase,
            )
            done += 1
        except (ValueError, RuntimeError) as exc:
            warnings.append(f"第 {pi} 页 OCR：{exc}")

    return done, len(page_indices), warnings


def _block_corpus_text(row: dict) -> str:
    name = (row.get("block_name") or "").strip()
    excerpt = (row.get("excerpt") or "").strip()
    return f"{name} {excerpt}".strip()


def _score_lessons(
    *,
    new_body: str,
    neighbor_lessons: list[Lesson],
    primary_old_id: str | None,
) -> list[dict]:
    from .suggest_anchors import _atom_excerpt, _text_similarity

    scored: list[dict] = []
    for old_les in neighbor_lessons:
        old_body = (old_les.body_text or "").strip()
        if not old_body:
            blocks = Block.query.filter_by(lesson_id=old_les.id).all()
            parts = [_atom_excerpt(old_les.id, b.atom_codes or []) for b in blocks[:12]]
            old_body = " ".join(p for p in parts if p)
        sim = body_text_similarity_score(new_body, old_body)
        score = float(sim) if sim is not None else _text_similarity(new_body, old_body)
        if score < LESSON_SCORE_MIN:
            continue
        scored.append(
            {
                "old_lesson_id": old_les.id,
                "old_lesson_uid": old_les.lesson_uid,
                "lesson_no": old_les.lesson_no,
                "lesson_name": old_les.lesson_name,
                "unit_title": old_les.unit_title,
                "score": round(score, 4),
                "is_primary": old_les.id == primary_old_id,
            }
        )
    scored.sort(key=lambda x: (-x["score"], x["lesson_name"] or ""))
    return scored


def _score_blocks(
    *,
    new_body: str,
    page_texts: list[tuple[int, str]],
    corpus: list[dict],
    primary_old_id: str | None,
    limit: int = 24,
) -> list[dict]:
    """块名粗筛 → Top-K 再比正文/摘要（两阶段）。"""
    from .suggest_anchors import _text_similarity

    if not corpus:
        return []

    name_ranked: list[tuple[float, dict]] = []
    for row in corpus:
        block_name = (row.get("block_name") or "").strip()
        if not block_name:
            continue
        name_score = _text_similarity(new_body, block_name)
        if name_score < BLOCK_NAME_PREFILTER_MIN:
            continue
        name_ranked.append((name_score, row))
    name_ranked.sort(key=lambda x: (-x[0], x[1].get("block_code") or ""))
    deep_pool = [row for _, row in name_ranked[:BLOCK_DEEP_TOP_K]]
    if not deep_pool:
        deep_pool = corpus[:BLOCK_DEEP_TOP_K]

    hits: list[dict] = []
    for row in deep_pool:
        cand = _block_corpus_text(row)
        if not cand:
            continue
        page_idx = None
        page_score = _text_similarity(new_body, cand)
        for pi, pt in page_texts:
            s = _text_similarity(pt, cand)
            if s > page_score:
                page_score = s
                page_idx = pi
        name_score = _text_similarity(new_body, row.get("block_name") or "")
        score = max(page_score, name_score * 0.85)
        if score < BLOCK_SCORE_MIN:
            continue
        hits.append(
            {
                "hit_level": "block",
                "old_lesson_id": row.get("lesson_id"),
                "old_block_id": row.get("block_id"),
                "old_block_code": row.get("block_code"),
                "new_page_index": page_idx,
                "match_score": round(float(score), 4),
                "is_primary_lesson": row.get("lesson_id") == primary_old_id,
                "is_cross_lesson": row.get("lesson_id") != primary_old_id,
                "match_basis": "block_name" if name_score >= page_score else "atom_text",
                "excerpt": (row.get("block_name") or "")[:256],
                "lesson_name": row.get("lesson_name"),
                "lesson_no": row.get("lesson_no"),
            }
        )
    hits.sort(key=lambda x: (-x["match_score"], x.get("old_block_code") or ""))
    for i, h in enumerate(hits[:limit], start=1):
        h["rank_in_scan"] = i
    return hits[:limit]


def _collect_page_texts(*, lesson_id: str) -> list[tuple[int, str]]:
    pages = (
        LessonPage.query.filter_by(lesson_id=lesson_id)
        .order_by(LessonPage.page_index)
        .all()
    )
    out: list[tuple[int, str]] = []
    for lp in pages:
        pi = int(lp.page_index)
        pt = _page_text_from_ocr(lp) or _page_text_from_atoms(lesson_id, pi)
        if pt:
            out.append((pi, pt))
    return out


def _run_prescan_match_core(
    *,
    new_les: Lesson,
    new_vol: Volume,
    primary_match: LessonMatch | None,
    primary_old: Lesson | None,
    scan: LessonContentScan,
    warnings: list[str],
) -> None:
    new_body = (new_les.body_text or "").strip()
    if not new_body:
        new_body = refresh_lesson_body_text(lesson_id=new_les.id)
    atom_stat = lesson_atoms_status(lesson_id=new_les.id)
    if not atom_stat["curate_ready"]:
        raise ValueError("请先 AI 整理本课（OCR + 整理原子），再运行对照预判断")
    if not new_body.strip():
        raise ValueError("整理原子后仍无可用正文，请检查教材页")

    page_texts = _collect_page_texts(lesson_id=new_les.id)
    unique_neighbors = resolve_prescan_neighbor_lessons(
        new_les=new_les,
        primary_match=primary_match,
        primary_old=primary_old,
    )
    primary_old_id = primary_old.id if primary_old else None

    lesson_scores = _score_lessons(
        new_body=new_body,
        neighbor_lessons=unique_neighbors,
        primary_old_id=primary_old_id,
    )

    neighbor_ids = [les.id for les in unique_neighbors]
    from .suggest_anchors import collect_blocks_for_lessons

    name_corpus = collect_blocks_for_lessons(
        lesson_ids=neighbor_ids,
        edition=new_vol.edition,
        subject=new_vol.subject,
        include_excerpt=False,
    )
    block_corpus = collect_blocks_for_lessons(
        lesson_ids=neighbor_ids,
        edition=new_vol.edition,
        subject=new_vol.subject,
        include_excerpt=True,
    )
    block_hits = _score_blocks(
        new_body=new_body,
        page_texts=page_texts,
        corpus=block_corpus,
        primary_old_id=primary_old_id,
    )

    title_components: dict | None = None
    if primary_old and primary_match:
        new_c = build_candidate(
            lesson_id=new_les.id,
            edition=new_vol.edition,
            grade=new_vol.grade,
            semester=new_vol.semester,
            unit_title=new_les.unit_title or "",
            lesson_no=new_les.lesson_no or "",
            lesson_name=new_les.lesson_name or "",
            old_course_id=new_les.old_course_id,
            page_count=new_les.page_count,
            body_text=new_body,
        )
        old_vol = Volume.query.get(primary_old.volume_id)
        old_c = (
            build_candidate(
                lesson_id=primary_old.id,
                edition=old_vol.edition if old_vol else new_vol.edition,
                grade=old_vol.grade if old_vol else new_vol.grade,
                semester=old_vol.semester if old_vol else new_vol.semester,
                unit_title=primary_old.unit_title or "",
                lesson_no=primary_old.lesson_no or "",
                lesson_name=primary_old.lesson_name or "",
                old_course_id=primary_old.old_course_id,
                page_count=primary_old.page_count,
                body_text=primary_old.body_text,
            )
            if old_vol
            else None
        )
        if new_c and old_c:
            title_components = score_components_for_pair(new_c, old_c)

    suggested, agreement, recommended_id, cross_flag, summary = _infer_prescan_verdict(
        primary_old_id=primary_old_id,
        lesson_scores=lesson_scores,
        block_hits=block_hits,
        title_components=title_components,
    )

    llm_prescan = _maybe_apply_llm_prescan(
        new_les=new_les,
        primary_old=primary_old,
        primary_match=primary_match,
        lesson_scores=lesson_scores,
        block_hits=block_hits,
        rule_summary=summary,
        warnings=warnings,
    )
    if llm_prescan:
        suggested = llm_prescan.get("pair_status") or suggested
        agreement = (
            "consistent"
            if llm_prescan.get("pair_status") == "confirmed"
            else "suggest_swap"
            if llm_prescan.get("pair_status") == "swap_primary"
            else "review_needed"
        )
        summary = llm_prescan.get("reason") or summary
        if llm_prescan.get("pair_status") == "swap_primary":
            idx = llm_prescan.get("recommended_lesson_index")
            cand_rows = _prescan_candidate_rows(primary_match, new_les.id)
            if idx is not None and 0 <= int(idx) < len(cand_rows):
                recommended_id = cand_rows[int(idx)].get("lesson_id")

    primary_body_sim = next(
        (x["score"] for x in lesson_scores if x.get("is_primary")),
        lesson_scores[0]["score"] if lesson_scores else None,
    )

    ocr_stat = lesson_ocr_status(lesson_id=new_les.id)
    scan.status = "done"
    scan.suggested_pair_status = suggested
    scan.coarse_agreement = agreement
    scan.lesson_body_similarity = primary_body_sim
    scan.recommended_old_lesson_id = recommended_id
    scan.cross_lesson_flag = cross_flag
    scan.summary_text = summary
    scan.ocr_pages_done = ocr_stat["ocr_pages_done"]
    scan.ocr_pages_total = ocr_stat["ocr_pages_total"]
    scan.result_json = {
        "prescan_step": "match_done",
        "lesson_scores": lesson_scores[:8],
        "structural_neighbor_count": len(unique_neighbors),
        "block_catalog_count": len(name_corpus),
        "block_name_samples": [
            {
                "lesson_no": r.get("lesson_no"),
                "lesson_name": r.get("lesson_name"),
                "block_name": r.get("block_name"),
            }
            for r in name_corpus[:16]
            if (r.get("block_name") or "").strip()
        ],
        "block_deep_pool": min(len(name_corpus), BLOCK_DEEP_TOP_K),
        "warnings": warnings,
        "title_components": title_components,
    }
    if llm_prescan:
        scan.result_json["llm_prescan"] = llm_prescan
    scan.finished_at = datetime.utcnow()

    LessonContentScanHit.query.filter_by(scan_id=scan.id).delete()
    for bh in block_hits:
        db.session.add(
            LessonContentScanHit(
                scan_id=scan.id,
                hit_level=bh["hit_level"],
                old_lesson_id=bh["old_lesson_id"],
                old_block_id=bh.get("old_block_id"),
                old_block_code=bh.get("old_block_code"),
                new_page_index=bh.get("new_page_index"),
                match_score=bh["match_score"],
                rank_in_scan=bh["rank_in_scan"],
                is_primary_lesson=bool(bh.get("is_primary_lesson")),
                is_cross_lesson=bool(bh.get("is_cross_lesson")),
                match_basis=bh.get("match_basis"),
                excerpt=bh.get("excerpt"),
            )
        )


def _prescan_candidate_rows(
    primary_match: LessonMatch | None,
    new_lesson_id: str,
) -> list[dict]:
    if not primary_match:
        return []
    from .pair_review import list_primary_candidates

    rows = list_primary_candidates(new_lesson_id=new_lesson_id, job_id=primary_match.job_id)
    for row in rows:
        old = Lesson.query.get(row.get("lesson_id")) if row.get("lesson_id") else None
        if old:
            row["body_text"] = old.body_text or ""
    return rows


def _maybe_apply_llm_prescan(
    *,
    new_les: Lesson,
    primary_old: Lesson | None,
    primary_match: LessonMatch | None,
    lesson_scores: list[dict],
    block_hits: list[dict],
    rule_summary: str,
    warnings: list[str],
) -> dict | None:
    import logging

    from ...llm.config import llm_prescan_enabled

    if not llm_prescan_enabled():
        return None
    log = logging.getLogger(__name__)
    try:
        from ...llm.prescan_suggest import suggest_lesson_prescan_with_llm

        candidates = _prescan_candidate_rows(primary_match, new_les.id)
        primary_dict = None
        if primary_old:
            primary_dict = {
                "lesson_no": primary_old.lesson_no,
                "lesson_name": primary_old.lesson_name,
                "unit_title": primary_old.unit_title,
            }
        decision = suggest_lesson_prescan_with_llm(
            new_lesson={
                "lesson_no": new_les.lesson_no,
                "lesson_name": new_les.lesson_name,
                "unit_title": new_les.unit_title,
                "body_text": new_les.body_text or "",
            },
            primary_old=primary_dict,
            candidates=candidates,
            rule_summary=rule_summary,
            rule_lesson_scores=lesson_scores,
            rule_block_hits=block_hits,
        )
        payload = decision.model_dump()
        payload["block_hints"] = enrich_prescan_block_hints(
            new_les=new_les,
            block_hits=block_hits,
            hints=payload.get("block_hints") or [],
        )
        return payload
    except Exception as exc:
        warnings.append(f"AI 预判断：{exc}")
        log.warning("LLM prescan failed for %s: %s", new_les.lesson_uid, exc)
        return None


def enrich_prescan_block_hints(
    *,
    new_les: Lesson,
    block_hits: list[dict],
    hints: list[dict],
) -> list[dict]:
    """补全 LLM 未覆盖的新课栏目（如第 4 页反思评价）。"""
    from ..block_pipeline_prepare import ensure_page_prepare, get_page_prepare

    out: list[dict] = list(hints or [])
    covered_text = " ".join(
        f"{h.get('old_block_label', '')} {h.get('new_page_hint', '')}" for h in out
    )

    pages = (
        LessonPage.query.filter_by(lesson_id=new_les.id)
        .order_by(LessonPage.page_index)
        .all()
    )
    for page in pages:
        pi = int(page.page_index)
        ensure_page_prepare(
            lesson_uid=new_les.lesson_uid,
            lesson_id=new_les.id,
            page_index=pi,
            lesson_name=new_les.lesson_name or "",
        )
        ctx = get_page_prepare(new_les.lesson_uid, pi) or {}
        for sec in ctx.get("sections") or []:
            name = str(sec.get("section_name") or "").strip()
            if not name or name in ("正文区",):
                continue
            if name in covered_text and f"第{pi}页" in covered_text:
                continue
            if any(name in str(h.get("new_page_hint") or "") for h in out):
                continue
            old_label = ""
            if name == "反思评价":
                old_label = "B13 旧课小结→新课反思评价"
            elif name == "拓展迁移" or "试一试" in name:
                old_label = "B14 旧尾页→新课拓展动手"
            elif name == "问题情境":
                old_label = "B02 问题情境"
            elif name == "科学探究":
                old_label = "B05–B08 科学探究"
            out.append(
                {
                    "old_block_label": old_label or name,
                    "new_page_hint": f"第{pi}页·{name}",
                    "confidence": "medium",
                }
            )
            covered_text += f" {name} 第{pi}页"

    hit_codes = {str(h.get("old_block_code") or "") for h in block_hits}
    for hit in block_hits[:12]:
        code = str(hit.get("old_block_code") or "").strip()
        if not code or code in covered_text:
            continue
        label = f"{code} {hit.get('block_name') or ''}".strip()
        if any(code in str(h.get("old_block_label") or "") for h in out):
            continue
        out.append(
            {
                "old_block_label": label[:80],
                "new_page_hint": hit.get("lesson_name") or "旧块命中",
                "confidence": "low",
            }
        )
        hit_codes.add(code)

    return out


def _prescan_workspace_response(*, lesson_uid: str, new_lesson_id: str) -> dict:
    from .workspace import build_new_annotate_workspace

    out = build_new_annotate_workspace(lesson_uid=lesson_uid)
    out["prescan_ocr"] = lesson_ocr_status(lesson_id=new_lesson_id)
    out["prescan"] = latest_content_scan_payload(new_lesson_id=new_lesson_id)
    out["ok"] = True
    return out


def _infer_prescan_verdict(
    *,
    primary_old_id: str | None,
    lesson_scores: list[dict],
    block_hits: list[dict],
    title_components: dict | None,
) -> tuple[str | None, str | None, str | None, bool, str]:
    """返回 suggested_pair_status, coarse_agreement, recommended_old_id, cross_flag, summary."""
    if not lesson_scores:
        return (
            "no_old" if not primary_old_id else None,
            "review_needed",
            None,
            False,
            "未能从正文匹配到旧课，请人工确认 Step 0。",
        )

    best = lesson_scores[0]
    primary_score = next(
        (x["score"] for x in lesson_scores if x.get("is_primary")),
        None,
    )
    best_id = best["old_lesson_id"]
    cross_ids = {
        h["old_lesson_id"]
        for h in block_hits
        if h.get("match_score", 0) >= BLOCK_SCORE_MIN
    }
    cross_flag = len(cross_ids) >= 2

    page_delta = (title_components or {}).get("page_count_delta")
    body_primary = primary_score if primary_score is not None else 0.0

    suggested: str | None = None
    agreement: str | None = "consistent"
    recommended_id: str | None = None
    summary_parts: list[str] = []

    if primary_old_id and best_id != primary_old_id:
        if best["score"] - body_primary >= SWAP_SCORE_DELTA:
            suggested = "swap_primary"
            agreement = "suggest_swap"
            recommended_id = best_id
            summary_parts.append(
                f"正文更贴近「{best.get('lesson_no')} {best.get('lesson_name')}」，"
                "建议更换主参照或人工复核。"
            )
        else:
            agreement = "review_needed"
            summary_parts.append("粗分主课与正文次优课接近，请翻页对照后确认 Step 0。")
    elif body_primary >= CONFIRM_BODY_MIN:
        suggested = "confirmed"
        summary_parts.append("正文与粗分主课较为一致，可优先考虑「确认对照」。")
    elif body_primary <= HEAVY_CHANGE_BODY_MAX:
        suggested = "heavy_change"
        agreement = "review_needed"
        summary_parts.append("正文与粗分主课重叠较低，宜选「同课改动大」或换主参照。")
    else:
        agreement = "review_needed"
        summary_parts.append("正文与主课部分相关，请对照左栏后确认 Step 0。")

    if page_delta is not None and abs(int(page_delta)) >= 2:
        summary_parts.append(f"页数差 {page_delta:+d}，内容可能重组。")
    if cross_flag:
        summary_parts.append("检测到块级信号来自多个旧课，建块时可用「跨课找旧块」。")

    return (
        suggested,
        agreement,
        recommended_id,
        cross_flag,
        " ".join(summary_parts),
    )


def latest_content_scan_payload(*, new_lesson_id: str) -> dict | None:
    row = (
        LessonContentScan.query.filter_by(new_lesson_id=new_lesson_id)
        .order_by(desc(LessonContentScan.created_at))
        .first()
    )
    if not row:
        return None
    hits = (
        LessonContentScanHit.query.filter_by(scan_id=row.id)
        .order_by(LessonContentScanHit.rank_in_scan)
        .limit(24)
        .all()
    )
    suggested = row.suggested_pair_status
    return {
        "scan_id": row.id,
        "status": row.status,
        "ocr_scope": row.ocr_scope,
        "ocr_pages_done": row.ocr_pages_done,
        "ocr_pages_total": row.ocr_pages_total,
        "suggested_pair_status": suggested,
        "suggested_pair_status_label": _suggested_pair_label(suggested),
        "coarse_agreement": row.coarse_agreement,
        "coarse_agreement_label": _agreement_label(row.coarse_agreement),
        "lesson_body_similarity": row.lesson_body_similarity,
        "recommended_old_lesson_id": row.recommended_old_lesson_id,
        "cross_lesson_flag": bool(row.cross_lesson_flag),
        "summary_text": row.summary_text,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "prescan_step": (row.result_json or {}).get("prescan_step"),
        "result_json": row.result_json or {},
        "block_hits": [
            {
                "hit_level": h.hit_level,
                "old_lesson_id": h.old_lesson_id,
                "old_block_code": h.old_block_code,
                "new_page_index": h.new_page_index,
                "match_score": h.match_score,
                "rank_in_scan": h.rank_in_scan,
                "is_primary_lesson": bool(h.is_primary_lesson),
                "is_cross_lesson": bool(h.is_cross_lesson),
                "match_basis": h.match_basis,
                "excerpt": h.excerpt,
            }
            for h in hits
        ],
    }


def _agreement_label(code: str | None) -> str:
    return {
        "consistent": "与粗分一致",
        "review_needed": "建议复核",
        "suggest_swap": "建议换主课",
    }.get((code or "").strip(), "")


def _suggested_pair_label(code: str | None) -> str:
    return {
        "confirmed": "确认对照",
        "heavy_change": "同课改动大",
        "no_old": "无对应",
        "swap_primary": "换主课",
    }.get((code or "").strip(), "需人工复核")


def run_lesson_prescan_ocr(
    *,
    lesson_uid: str,
    ocr_scope: str = "missing_pages",
    page_index: int | None = None,
) -> dict:
    """Step ①：OCR 缺页并汇总 body_text（不写匹配结论）。"""
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    warnings: list[str] = []

    if page_index is not None:
        pi = int(page_index)
        try:
            extract_atoms_for_page(
                lesson_uid=lesson_uid,
                page_index=pi,
                replace_page=True,
                use_ocr=True,
                fill_gaps=True,
                book_type="new",
            )
            done, total = 1, 1
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"第 {pi} 页 OCR：{exc}") from exc
    else:
        done, total, ocr_warn = ocr_lesson_pages(
            lesson_uid=lesson_uid,
            ocr_scope=(ocr_scope or "missing_pages").strip() or "missing_pages",
        )
        warnings.extend(ocr_warn)

    body = refresh_lesson_body_text(lesson_id=new_les.id)
    db.session.commit()

    out = _prescan_workspace_response(lesson_uid=lesson_uid, new_lesson_id=new_les.id)
    out["ocr_run"] = {
        "pages_ocr_this_run": done,
        "pages_total": total,
        "warnings": warnings,
        "has_body_text": bool(body.strip()),
        "prescan_step": "ocr_done",
    }
    return out


def run_content_prescan_match(*, lesson_uid: str) -> dict:
    """Step ②–④：拉邻居旧块 → 块匹配 → Step 0 建议（需先有正文）。"""
    new_les = get_lesson_by_uid(lesson_uid, book_type="new")
    new_vol = Volume.query.get(new_les.volume_id)
    if not new_vol:
        raise ValueError("新教材册不存在")

    primary_match = get_primary_lesson_match(new_les.id)
    primary_old = (
        Lesson.query.get(primary_match.old_lesson_id)
        if primary_match and primary_match.old_lesson_id
        else None
    )

    scan = LessonContentScan(
        new_lesson_id=new_les.id,
        match_job_id=primary_match.job_id if primary_match else None,
        coarse_primary_match_id=primary_match.id if primary_match else None,
        status="running",
        ocr_scope="match_only",
    )
    db.session.add(scan)
    db.session.flush()

    warnings: list[str] = []
    try:
        _run_prescan_match_core(
            new_les=new_les,
            new_vol=new_vol,
            primary_match=primary_match,
            primary_old=primary_old,
            scan=scan,
            warnings=warnings,
        )
        db.session.commit()
    except Exception as exc:
        scan.status = "failed"
        scan.summary_text = str(exc)
        scan.result_json = {
            "prescan_step": "match_failed",
            "warnings": warnings,
            "error": str(exc),
        }
        scan.finished_at = datetime.utcnow()
        db.session.commit()
        raise

    return _prescan_workspace_response(lesson_uid=lesson_uid, new_lesson_id=new_les.id)


def run_content_prescan(
    *,
    lesson_uid: str,
    ocr_scope: str = "missing_pages",
    run_ocr: bool = False,
) -> dict:
    """兼容旧一键流程；默认仅匹配，OCR 请用 run_lesson_prescan_ocr。"""
    if run_ocr:
        run_lesson_prescan_ocr(lesson_uid=lesson_uid, ocr_scope=ocr_scope)
    return run_content_prescan_match(lesson_uid=lesson_uid)
