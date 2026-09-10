from __future__ import annotations

from flask import current_app, redirect, render_template, request

from . import pages_bp


@pages_bp.get("/")
def home():
    return render_template("home.html")


@pages_bp.get("/courseware/")
def courseware_hub():
    """新链路：新课件薄仓（上传+OCR，供选题调用 KP）。"""
    return render_template(
        "hubs/courseware.html",
        shell=current_app.config.get("PRODUCT_SHELL", "classic"),
    )


@pages_bp.get("/selection/")
def selection_hub():
    """新链路：以新课为入口的选题齐套工作台。"""
    return render_template(
        "hubs/selection.html",
        shell=current_app.config.get("PRODUCT_SHELL", "classic"),
    )


@pages_bp.get("/xiaoke-workbench/")
def xiaoke_workbench_hub():
    """现网小科三入口打包：旧库 / 新库 / 新旧对比结果。"""
    return render_template("hubs/xiaoke_workbench.html")


@pages_bp.get("/textbook-library/")
def textbook_library_shelf():
    """教材库：化学 / 科学馆藏入馆口。"""
    return render_template("textbook_library/shelf.html")


@pages_bp.get("/textbook-library/slot")
def textbook_library_slot():
    """教材库：某一册次格内的多副本书架。"""
    return render_template("textbook_library/slot.html")


@pages_bp.get("/textbook-library/volumes/<volume_code>/intake")
def textbook_library_volume_intake(volume_code: str):
    return render_template(
        "textbook_library/volume_intake.html",
        volume_code=volume_code,
        ui_shell="textbook_library",
    )


@pages_bp.get("/textbook-library/lessons/<lesson_uid>/work")
def textbook_library_lesson_work(lesson_uid: str):
    from ..services.old_library.lessons import get_lesson_by_uid

    try:
        les = get_lesson_by_uid(lesson_uid, book_type="old")
        title = f"{les.lesson_no} {les.lesson_name}".strip() or lesson_uid
        volume_code = les.volume.volume_code if les.volume else ""
    except ValueError:
        title = lesson_uid
        volume_code = ""
    return render_template(
        "textbook_library/lesson_work.html",
        lesson_uid=lesson_uid,
        page_title=title,
        ui_shell="textbook_library",
        intake_return_url=(
            f"/textbook-library/volumes/{volume_code}/intake" if volume_code else "/textbook-library/"
        ),
    )


@pages_bp.get("/textbook-library/lessons/<lesson_uid>/annotate")
def textbook_library_lesson_annotate(lesson_uid: str):
    """旧 annotate 壳退役：302 → 单侧 work 页。"""
    return redirect(f"/textbook-library/lessons/{lesson_uid}/work", code=302)


@pages_bp.get("/textbook-library/intake")
def textbook_library_intake():
    code = (request.args.get("code") or "").strip()
    if code:
        return redirect(f"/textbook-library/volumes/{code}/intake", code=302)
    return redirect("/textbook-library/", code=302)
