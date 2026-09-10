from __future__ import annotations

from flask import render_template, request

from . import pages_textbook_diff_bp


def _view_back_meta(
    *,
    new_code: str,
    new_pdf_source: str,
    preview_blob_id: str,
    from_hub: str,
    subject: str,
    old_code: str,
) -> dict[str, str]:
    code = (new_code or "").strip()
    side = "old" if code.endswith("-DOLD") else "new"
    from_hub = (from_hub or "").strip().lower()
    subject = (subject or "").strip()
    old_code = (old_code or "").strip()

    def _workbook_back() -> dict[str, str]:
        q = f"old_code={old_code}&new_code={code}"
        if subject:
            q = f"subject={subject}&{q}"
        wb = f"/textbook-diff/workbook?{q}"
        return {
            "back_url": wb,
            "back_label": "返回本册建设",
            "hub_url": wb,
            "hub_label": "返回本册建设",
        }

    # 本册建设进入，或修订版页对比（非 intake）→ 回本册工作页，便于继续点其他对比
    if old_code and code and from_hub != "intake":
        if from_hub == "workbook" or new_pdf_source == "draft":
            return _workbook_back()

    if new_pdf_source == "draft" and code:
        q = f"code={code}&kind=draft"
        if preview_blob_id:
            q += f"&preview_blob_id={preview_blob_id}"
        back = f"/textbook-diff/{side}/intake/compare?{q}"
        return {
            "back_url": back,
            "back_label": "返回修订版对比",
            "hub_url": back,
            "hub_label": "返回修订版对比",
        }
    if code:
        back = f"/textbook-diff/{side}/intake?code={code}"
        return {
            "back_url": back,
            "back_label": "返回教材上传",
            "hub_url": back,
            "hub_label": "返回教材上传",
        }
    return {
        "back_url": "/textbook-diff/",
        "back_label": "教材对比首页",
        "hub_url": "/textbook-diff/",
        "hub_label": "教材对比首页",
    }


@pages_textbook_diff_bp.get("/view")
def diff_view_page():
    new_code = request.args.get("new_code", "")
    new_pdf_source = request.args.get("new_pdf_source", "full")
    preview_blob_id = request.args.get("preview_blob_id", "")
    back = _view_back_meta(
        new_code=new_code,
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        from_hub=request.args.get("from", ""),
        subject=request.args.get("subject", ""),
        old_code=request.args.get("old_code", ""),
    )
    return render_template(
        "textbook_diff/view.html",
        old_code=request.args.get("old_code", ""),
        new_code=new_code,
        mode=request.args.get("mode", "page"),
        old_page=request.args.get("old_page", ""),
        new_page=request.args.get("new_page", ""),
        new_lesson_uid=request.args.get("new_lesson_uid", ""),
        page_index=request.args.get("page_index", "1"),
        new_pdf_source=new_pdf_source,
        preview_blob_id=preview_blob_id,
        from_hub=request.args.get("from", ""),
        subject=request.args.get("subject", ""),
        back_url=back["back_url"],
        back_label=back["back_label"],
        hub_url=back["hub_url"],
        hub_label=back["hub_label"],
    )
