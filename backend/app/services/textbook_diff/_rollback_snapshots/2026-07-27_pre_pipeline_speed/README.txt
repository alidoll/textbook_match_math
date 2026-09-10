# 回滚快照说明（2026-07-27 整册加速改动前）
#
# 目录文件：
#   page_text_extract.py   — 文字 OCR / 拼音校准
#   atom_compare.py        — 页 OCR、旧新串行、force_llm_refresh
#   lesson_pipeline.py     — 页并行 workers 默认 3
#   page_image_compare.py  — 图片比对（若有）
#
# 回滚示例（在仓库根目录）：
#   copy /Y backend\app\services\llm\_rollback_snapshots\2026-07-27_pre_pipeline_speed\page_text_extract.py backend\app\services\llm\page_text_extract.py
#   copy /Y backend\app\services\textbook_diff\_rollback_snapshots\2026-07-27_pre_pipeline_speed\atom_compare.py backend\app\services\textbook_diff\atom_compare.py
#   copy /Y backend\app\services\textbook_diff\_rollback_snapshots\2026-07-27_pre_pipeline_speed\lesson_pipeline.py backend\app\services\textbook_diff\lesson_pipeline.py
#
# 本次将改动：
#   1) 文字 OCR 默认吃本地缓存（去掉 force_llm_refresh=True）
#   2) 同一页对旧/新 OCR 并行
#   3) DIFF_PIPELINE_PAGE_WORKERS 默认 3 → 5
