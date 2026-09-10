"""判定规则阈值（M1 单一配置源，后续可用反馈数据调参）。"""

from __future__ import annotations

# 文字改动量边界（区块级，后续 M2 接 diff 引擎）
TEXT_CHANGE_DIRECT_REUSE_MAX = 0.05
TEXT_CHANGE_OPTIMIZE_MIN = 0.05
TEXT_CHANGE_OPTIMIZE_MAX = 0.30
TEXT_CHANGE_REFERENCE_MIN = 0.30
TEXT_CHANGE_REFERENCE_MAX = 0.70
TEXT_CHANGE_NEW_BUILD_MIN = 0.70

# 优化调整子类型：仅改文字 vs 需重配音
OPTIMIZE_TEXT_ONLY_MAX = 0.20

# 课时级：连续可复用组
MIN_CONTINUOUS_REUSABLE_LENGTH = 2
CONTINUOUS_REUSABLE_RATIO_VIDEO_CLIP = 1 / 3
CONTINUOUS_REUSABLE_RATIO_INSUFFICIENT = 2 / 3

# 课时级：动作占比
REFERENCE_RATIO_MATERIAL_RERECORD = 0.20
OPTIMIZE_RATIO_MATERIAL_RERECORD_MAX = 0.20

# 页面级
PAGE_MOSTLY_REUSE_MIN = 0.80
PAGE_PARTIAL_ADJUST_MIN = 0.50

# 质检
MATCH_SCORE_GAP_LOW_CONFIDENCE = 0.10

# 有序多锚定：同一新区块可匹配多个 sort_order 连续旧块（建块 + 对比共用）
# compare_qa 仅对 non_contiguous / duplicate_anchor / order_inversion 告警

# 有效区块：碎片判定
FRAGMENT_MAX_ATOMS = 1
FRAGMENT_MIN_TEXT_CHARS = 15

# 实验块关键词
EXPERIMENT_BLOCK_KINDS = frozenset({"activity"})
EXPERIMENT_NAME_KEYWORDS = ("实验", "探究", "演示", "操作")

# 完整闭环关键词（启发式，建块 metadata 优先）
CLOSURE_INTRO_KEYWORDS = ("导入", "情境", "问题", "引入", "激趣")
CLOSURE_ACTIVITY_KEYWORDS = ("实验", "探究", "活动", "观察", "操作")
CLOSURE_SUMMARY_KEYWORDS = ("总结", "小结", "归纳", "结论", "反思")
