"""语文附录页识别（数学等非语文学科无附录，安全返回 False）。"""
from __future__ import annotations

from ...models import Volume


def volume_page_is_appendix_table(volume: Volume | None, page_1: int) -> bool:
    """判断某页是否为附录页（识字表/写字表/词语表等）。

    非语文学科无附录页，安全返回 False。
    """
    if volume is None:
        return False
    subject = (getattr(volume, "subject", "") or "").strip()
    if subject != "语文":
        return False
    # 语文附录页判断逻辑（如有需要可从源项目移植）
    return False
