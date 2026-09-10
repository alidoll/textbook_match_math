"""课名主题簇：标题字面不像但仍属同一教学主题的新旧对照。

用于新库粗分与（按册开启的）教材对比粗分：在字符串相似度之外，
用教研确认的主题别名抬分 / 注入候选。
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...parsers.lesson_reuse_match import lesson_reuse_match_key

# 主题簇内旧课优先序：越靠前越优先（再结合年级邻近排序）
THEME_ALIAS_BOOST = 0.28
THEME_ALIAS_LES_FLOOR = 0.55

# 默认供新库 course_match 使用
DEFAULT_THEME_PACKS: tuple[str, ...] = ("moon",)


@dataclass(frozen=True)
class LessonThemeCluster:
    theme_id: str
    """新课标题归一化后，命中任一子串则归属本簇。"""
    new_needles: tuple[str, ...]
    """旧课标题归一化后，命中任一子串则归属本簇。"""
    old_needles: tuple[str, ...]
    """旧课优先对照名（归一化前可读名，匹配时再 normalize）。"""
    preferred_old_titles: tuple[str, ...]


# 湘科月亮/地月主题：六上新课把旧「地月系」等拆成三课
_MOON_CLUSTERS: tuple[LessonThemeCluster, ...] = (
    LessonThemeCluster(
        theme_id="moon_phase",
        new_needles=("月有阴晴圆缺", "阴晴圆缺", "月相"),
        old_needles=("在地球上看月球", "变化的月亮", "月相"),
        preferred_old_titles=("在地球上看月球", "变化的月亮"),
    ),
    LessonThemeCluster(
        theme_id="earth_moon_system",
        new_needles=("月球——地球卫星", "月球地球卫星", "地球卫星", "地月系"),
        old_needles=("地月系", "月球——地球卫星", "月球地球卫星", "地球卫星"),
        preferred_old_titles=("地月系",),
    ),
    LessonThemeCluster(
        theme_id="moon_explore",
        new_needles=("人类探月史", "探月史"),
        old_needles=("探索月球秘密", "探索月球的秘密"),
        preferred_old_titles=("探索月球的秘密",),
    ),
)

# 苏教五上：教材对比粗分专用（勿并入默认新库 pack，避免牵动其它册）
_SUJI_5S_CLUSTERS: tuple[LessonThemeCluster, ...] = (
    LessonThemeCluster(
        theme_id="earth_surface",
        new_needles=("地表与流水",),
        old_needles=("地球的表面", "地球表面"),
        preferred_old_titles=("地球的表面",),
    ),
)

_THEME_PACKS: dict[str, tuple[LessonThemeCluster, ...]] = {
    "moon": _MOON_CLUSTERS,
    "suji_5s": _SUJI_5S_CLUSTERS,
}


def _norm(title: str | None) -> str:
    return lesson_reuse_match_key(title or "")


def _needle_hit(key: str, needle: str) -> bool:
    nk = _norm(needle)
    return bool(nk) and nk in key


def _resolve_clusters(
    packs: Sequence[str] | None = None,
) -> tuple[LessonThemeCluster, ...]:
    use = tuple(packs) if packs is not None else DEFAULT_THEME_PACKS
    out: list[LessonThemeCluster] = []
    seen: set[str] = set()
    for pack in use:
        for c in _THEME_PACKS.get(pack, ()):
            if c.theme_id in seen:
                continue
            seen.add(c.theme_id)
            out.append(c)
    return tuple(out)


def theme_clusters_for_new_title(
    lesson_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> list[LessonThemeCluster]:
    key = _norm(lesson_title)
    if not key:
        return []
    return [
        c
        for c in _resolve_clusters(packs)
        if any(_needle_hit(key, n) for n in c.new_needles)
    ]


def theme_clusters_for_old_title(
    lesson_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> list[LessonThemeCluster]:
    key = _norm(lesson_title)
    if not key:
        return []
    return [
        c
        for c in _resolve_clusters(packs)
        if any(_needle_hit(key, n) for n in c.old_needles)
    ]


def shared_theme_ids(
    new_title: str | None,
    old_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> list[str]:
    new_ids = {c.theme_id for c in theme_clusters_for_new_title(new_title, packs=packs)}
    old_ids = {c.theme_id for c in theme_clusters_for_old_title(old_title, packs=packs)}
    return sorted(new_ids & old_ids)


def is_preferred_old_for_new(
    new_title: str | None,
    old_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> bool:
    """旧课是否落在新课所属主题簇的 preferred 列表。"""
    old_key = _norm(old_title)
    if not old_key:
        return False
    for cluster in theme_clusters_for_new_title(new_title, packs=packs):
        for pref in cluster.preferred_old_titles:
            pk = _norm(pref)
            if not pk:
                continue
            if pk == old_key or pk in old_key or old_key in pk:
                return True
    return False


def preferred_old_title_needles(
    new_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for cluster in theme_clusters_for_new_title(new_title, packs=packs):
        for pref in cluster.preferred_old_titles:
            nk = _norm(pref)
            if nk and nk not in seen:
                seen.add(nk)
                out.append(pref)
    return out


def theme_alias_boost(
    new_title: str | None,
    old_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> float:
    """同主题簇抬分；命中 preferred 旧课额外略抬。"""
    preferred = is_preferred_old_for_new(new_title, old_title, packs=packs)
    shared = shared_theme_ids(new_title, old_title, packs=packs)
    if not shared and not preferred:
        return 0.0
    boost = THEME_ALIAS_BOOST
    if preferred:
        boost += 0.06
    return boost


def qualifies_theme_alias_core(
    new_title: str | None,
    old_title: str | None,
    *,
    grade_gap: int,
    packs: Sequence[str] | None = None,
) -> bool:
    """主题别名对：允许作为 high_similarity 核心对照（仍限年级跨度）。"""
    if grade_gap > 4:
        return False
    if is_preferred_old_for_new(new_title, old_title, packs=packs):
        return True
    return bool(shared_theme_ids(new_title, old_title, packs=packs))


# 苏教五上：明确禁止的错配（新课子串 → 禁止旧课子串）
_SUJI_5S_BLOCKED: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("潜望镜", "万花筒"), ("光的反射", "光源")),
    (("地壳的组成", "地壳组成"), ("地表雕刻师",)),
)


def is_blocked_theme_pair(
    new_title: str | None,
    old_title: str | None,
    *,
    packs: Sequence[str] | None = None,
) -> bool:
    """教研确认的禁止配对（仅对开启的 pack 生效）。"""
    use = tuple(packs) if packs is not None else DEFAULT_THEME_PACKS
    if "suji_5s" not in use:
        return False
    nk = _norm(new_title)
    ok = _norm(old_title)
    if not nk or not ok:
        return False
    for new_needles, old_needles in _SUJI_5S_BLOCKED:
        if not any(_needle_hit(nk, n) for n in new_needles):
            continue
        if any(_needle_hit(ok, o) for o in old_needles):
            return True
    return False
