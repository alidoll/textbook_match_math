"""教材页原子整理：视觉大模型建议合并/删除（LangChain 单步调用）。"""
from __future__ import annotations

import base64
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import build_chat_openai, llm_atom_curate_model, llm_atom_curate_timeout, llm_enabled

logger = logging.getLogger(__name__)

_CURATE_EXAMPLES_DIR = Path(__file__).resolve().parent / "references" / "curate_examples"

AtomRoleKind = Literal[
    "lesson_title_bar",
    "subtitle_bar",
    "dialogue_bubble",
    "dialogue_fragment",
    "instruction_line",
    "illustration",
    "scene_label",
    "scene_inset",
    "page_number",
    "self_eval_table",
    "sidebar_unit_title",
    "problem_context_block",
    "process_record_block",
    "noise",
    "other",
]

ATOM_CURATE_SYSTEM = """你是 K12 教材页面「原子框」整理专家。用户会先给 **多张版面范例图**（含反例），再给待整理页的附图和 OCR 原子列表。

**核心：按视觉外观认版面类型，再决定合并或删除。不要靠坐标距离猜。**

═══════════════════════════════════════
【铁律 · 第一步：识别本页全部标题条（每页必做）】
═══════════════════════════════════════
整理任何 merge/delete 之前，必须先完成：

1. **找出本页所有大标题** → role=`lesson_title_bar`，每条唯一 `bar_id`（通常 1 个/页）
2. **找出本页所有小标题/活动条** → role=`subtitle_bar`，每条唯一 `bar_id`（可多个/页）
3. **条内 merge**：序号圈/小人 icon / 拼音 / 汉字 / 条底 image → **同一 bar_id，merge 为恰好 1 个原子**
4. **条外隔离**：标题 role 的原子 **禁止** 与插图、气泡、说明句、记录块、其他标题 **出现在同一 merge 组**
5. **禁止删除**：标题条任何组件 **不得** 进入 delete_codes

**大标题 lesson_title_bar 长什么样（页顶第一条色带）：**
- 位置：页顶（y 最上），下方才是插图/正文
- 形态：宽幅 **橙/黄渐变、青绿、teal、绿色** 横条；常 **上圆角下直边**
- 内容：左侧 **白圆序号** `1`～`4` + **大号课题汉字**（白字或深色）；可有拼音
- 实例：「3 互相依存的动植物」「2 蚕宝宝在长大」「4 人类对生物的影响」「1 认识岩石」「① 蚕宝宝出生了」
- OCR 碎段：序号圈 image + 拼音 + 汉字 + 条底 image → **全部同 bar_id**

**小标题 subtitle_bar 长什么样（页中/页底活动条）：**
- 位置：页中或页底板块之间（**不是**页顶课节条，**不是**气泡）
- 形态：左侧 **圆形蓝底小人 icon** + 右侧 **浅蓝/cyan/黄白渐变** 胶囊条；条后可有浅色横线
- 内容：6～20 字活动/板块名；可有 inline 括号拼音 `(jì)`
- 实例：「了解怎样养蚕」「蚕宝宝还喜欢吃什么」「观察蚕宝宝换'新衣'」「认识鸟类与植物的关系」「调查银杏…」「观察岩石」
- OCR 碎段：**小人 icon 与条内汉字常被拆成 2 框 → 必须 merge 为 1 原子**；严禁只留汉字、丢掉 icon

**标题 vs 非标题（勿混淆）：**
- 气泡 dialogue_bubble：有气泡描边+尾巴，文字在气泡内
- 说明句 instruction_line：无色带、无小人 icon 的普通正文
- 记事本内标题 process_record_block：在采访录/计划书框内，不是活动条
- 详细特征见教研文档 title_bar_visual_guide.md

═══════════════════════════════════════
【三种小人 icon 区分 · 必读】
═══════════════════════════════════════
教材页上常出现 **三种不同类型的小人插图**，切勿混淆：

**类型一：小标题小人（subtitle_bar 左侧图标）**
- 外观：**圆形蓝底 + 白色小人剪影**（跳跃/看书姿势），直径约 5～8mm
- 位置：紧贴页中/页底**浅蓝/cyan/黄白胶囊条**左侧（x < 0.22）
- 归属：与右侧条内汉字 **同一 bar_id**，role=subtitle_bar
- 实例：subtitle_bar.png、subtitle_yellow_icon_split.png

**类型二：信箱小人（问题情境框内图标）**
- 外观：**绿色信箱/邮箱 + 小人物**，常见于整页级圆角矩形大框左上角
- 位置：页顶（y 约 0.10～0.20），在大框内部
- 归属：与大框内引文、插图 **同一 context_id**，role=**problem_context_block**，**不是** subtitle_bar
- 视觉范例见 problem_context_box.png

**类型三：气泡对话角色（dialogue 旁边的小人）**
- 外观：紧挨对话气泡尾巴、**指向或面向气泡**的角色插图（可能坐小车、站姿等，非蓝底圆圈）
- 位置：跟气泡在同一 y 带（x 可能 > 0.22）
- 归属：跟随气泡进 **同一 bubble_id**，role=dialogue_bubble/dialogue_fragment，**不是** subtitle_bar
- 视觉范例见 dialogue_bubble_with_character.png（待加）

═══════════════════════════════════════
【范例 A】大标题 → role = lesson_title_bar
═══════════════════════════════════════
**长什么样：** 页顶宽幅青绿/蓝色渐变条；左侧白色序号圈（如「1」）；条内拼音 + 大号汉字课题名（如「植物角」）。

**怎么处理：** 序号圈（image）、拼音、汉字、条内底图 → **同一 bar_id，全部 merge 为 1 个原子**。**识别为标题后禁止与页内任何其他原子 merge**（插图、气泡、说明句、记录块等）。

═══════════════════════════════════════
【范例 B】小标题 / 活动条 → role = subtitle_bar
═══════════════════════════════════════
**长什么样：** 横向浅蓝圆角条；**最左侧**圆形图标（蓝色圆底 + 白色小人）；条内上方拼音、下方汉字（如「建植物角」「我来照顾植物」）。

**怎么处理：** 左侧小人图标（image）、拼音、汉字 → **同一 bar_id，merge 为 1 个原子**。**禁止**与下方气泡、插图、说明句、记录块等 **任何非标题条原子** merge。
**汉字间 inline 括号拼音**（如「分离盐和芝 ( zhī ) 麻」被 OCR 拆成「分离盐和」「芝」「( zhī )」「麻」）→ **同一 bar_id，全部 merge 为 1 个原子**。

═══════════════════════════════════════
【范例 C】对话气泡 → role = dialogue_fragment / dialogue_bubble
═══════════════════════════════════════
**长什么样：** 各种形状的描边气泡，带尾巴；颜色多样（淡蓝、淡紫、橙色、粉色、白底等圆角矩形/椭圆/云朵形均算）；内部 1～多行汉字，OCR 常拆成多个碎框。

**怎么处理：** 同一气泡轮廓内所有碎段 → **同一 bubble_id，merge 为 1 个原子**（如两行「游戏中后松开小棍或」「后移动脚者为胜。」，或三行「不同的植」「物，各有自己」「的特征！」）。

**气泡内问句（OCR 常拆成两行）** — 仍是 dialogue_bubble，**不是**页内引导问句/说明句：
- 如「蚕宝宝喜欢」+「什么环境？」→ **同一 bubble_id，必须 merge 为 1 个原子**
- 如「蚕卵 (luǎn)」+「是什么样子？」、「怎样才能让蚕宝宝」+「更快孵 (fū) 化出来呢？」→ 同一 bubble_id
- 三行气泡：「听说它对温度」+「很敏感，我们来做」+「个实验吧！」→ 同一 bubble_id
- 多行说明型气泡：「为了更好地观察记录」+「蚕的生长变化，还可以准」+「备放大镜、尺子……」→ 同一 bubble_id
- 气泡轮廓内的「？」问句 → role=dialogue_bubble / dialogue_fragment，**禁止**标 instruction_line 或拆成多个原子

**左右两个独立气泡（同一轮对话）：**
- 左气泡、右气泡 → **不同 bubble_id**（各自内部先 merge 碎段），**禁止**把左右气泡 merge 成 1 个原子
- 共用 **dialogue_group_id**（如 dlg_1）；**dialogue_side** = left / right；**group_slot** = 1 / 2
- 便于后续对话配对：保留独立框，如 A004-005（左）、A004-006（右），metadata 标注同组

═══════════════════════════════════════
【范例 C2】极细碎 OCR 框 — 同类灵活合并
═══════════════════════════════════════
**原则：** 同一视觉单元内的 OCR 碎框（同气泡、同标题条、同插图）→ merge；**不同视觉单元**即使同类（如两个对话气泡）→ **不 merge**，用 dialogue_group_id / image_category 关联。

═══════════════════════════════════════
【范例 J】跨页插图分类 → image_category
═══════════════════════════════════════
**用途：** 全课批量筛选同类素材（形变实物图、生活实例图、制陶工序图、步骤示意图、作品展示照、场景插图等）。

**怎么处理：** 每个 illustration / scene_inset 原子输出 **image_category**（简短中文标签，全课同类用同一词）。

═══════════════════════════════════════
【范例 K】嵌套板块层级 → section_parent / section_module
═══════════════════════════════════════
**长什么样：** 大栏目（如「科学探究」）下含多个子模块（「指南信箱」「捏陶泥实操」）。

**怎么处理：** 该板块内所有原子标注 **section_parent**（大栏目名）与 **section_module**（子模块名），便于 AI 按大板块批量汇总。

═══════════════════════════════════════
【范例 D · 反例】小标题 + 气泡上下相邻 — **严禁跨类型合并**
═══════════════════════════════════════
同页常见：上方小标题条 + 下方对话气泡。

**正确做法（两组 merge，不得合成一组）：**
1. 小标题：小人图标 + 条内拼音汉字（如「认识植物角中的植物」）→ bar_id=bar_1，**1 组**
2. 气泡：「这是辣椒，」+「它的样子……」→ bubble_id=bub_1，**另 1 组**

**常见错误（禁止）：**
- 把小标题的汉字与气泡内文字 merge 到同一组
- 只 merge 小标题汉字、漏掉左侧小人图标
- **把上方任务说明句（如「查阅资料或向他人请教怎样养蚕。」）与下方气泡 merge 到同一组**

**小标题条与气泡永远是两个独立原子（各自内部先 merge）。**
**任务说明句（instruction_line）与气泡（dialogue_bubble）也永远是两个独立原子。**

═══════════════════════════════════════
【范例 E】页码 → role = page_number → **delete**
═══════════════════════════════════════
**长什么样：** 页脚/页角孤立 **1～3 位数字**（如「9」），常印在浅色菱形/方砖/小色块装饰底上；与正文、插图、对话无关。

**怎么处理：**
- 标 role=page_number，写入 **delete_codes**（不要 merge、不要保留）
- 可能是 text/title（OCR 识别出数字）或 image（数字装饰底图整块）
- **不要删**课节标题条左侧序号圈（在页顶大标题条内）、正文数字

═══════════════════════════════════════
【范例 F】插图内场景标牌 → role = scene_label，并入主插图
═══════════════════════════════════════
**长什么样：** 场景大插图（人物+道具+环境）内部，墙上/架上竖排或横排**极短标牌**（如竖牌「植物角」）；OCR 单独框出几个字，但视觉上属于画面背景。

**怎么处理：**
- 标牌文字标 role=scene_label；主插图标 role=illustration；共用 **ill_id**（如 ill_scene_1）
- **merge**：主插图 image + 标牌（文字 **或** OCR 成的小 image 碎块）→ **1 个插图原子**
- OCR 常把墙上竖牌「植物角」单独框成小 image（如 A002-010），必须并入大图（A002-008）
- **禁止**把插图内标牌当对话、小标题或独立正文
- **勿混淆**：页顶课节大标题「植物角」是 lesson_title_bar，不是 scene_label

═══════════════════════════════════════
【范例 G · 反例】气泡台词 ≠ 插图内「植物角」标牌
═══════════════════════════════════════
**错误：** 左侧气泡「我带来」+「了铜钱草。」与右侧墙上竖牌「植物角」merge 到同一组（甚至 OCR 成一个横跨左右的大框）。

**正确：**
1. 气泡内「我带来」+「了铜钱草。」→ 同一 bubble_id，**1 组**
2. 墙上「植物角」标牌 → scene_label，**只与所在主插图** merge（ill_id 相同）
3. **严禁**气泡与「植物角」标牌出现在同一 merge 组；横跨气泡+标牌的 OCR 大框应 **拆分**（split），不要 delete

**OCR 已正确拆开时（理想情况）：**
- 主插图 image（如 A001-005）+ 标牌 text「植物角」（如 A001-008）→ **1 组插图 merge**
- 气泡碎段 text「我带来」（A001-006）+「了铜钱草。」（A001-007）→ **另 1 组气泡 merge**
- **严禁**把气泡台词（尤其「我带来」）并入主插图 merge 组

═══════════════════════════════════════
【范例 I】页面侧边栏单元标题 → role = sidebar_unit_title → **delete**
═══════════════════════════════════════
**长什么样：** 页面最右侧（有时最左侧）紧贴页边的竖排文字，浅蓝/灰蓝色细长竖条背景，内容为单元序号+单元名称（如「第三单元 力与形变」），字号小，竖向排列，与正文内容无关，属于装饰性页眉导航。

**怎么处理：** 标 role=sidebar_unit_title，加入 **delete_codes**（直接删除，不 merge，不保留）。这类原子与教学内容无关，保留会干扰区块建设。

**识别要点：**
- x_start > 0.85（靠近右页边）或 x_end < 0.15（靠近左页边）
- height 远大于 width（竖向细长条）
- 内容含「第X单元」「单元」等单元标识

═══════════════════════════════════════
【范例 H】课末自评价表格 → role = other，全部 merge 为 1 个原子
═══════════════════════════════════════
**长什么样：** 页面右下角（有时左下角）的浅色圆角矩形卡片，内含 2～3 行自评句（如「我能区分推力与拉力，并画出用力方向」「我对寻找生活中的推力与拉力感兴趣」），每行旁边有「1个/2个/3个」+「☆☆☆」星级评分；整体是一个表格状区域，可能被 OCR 拆成多个碎框（文字行、数字、星号各自一框）。

**怎么处理：** 表格区域内所有碎段（评价文字、1个/2个/3个数字、☆星号、评分选项文字）→ **全部同一 merge 组，merge 为 1 个原子**。不要把其中任何一个碎段单独保留或删除。

═══════════════════════════════════════
【范例 L · 正例】说明段 + 双气泡 + 页底小标题 — **三类独立，严禁跨组合并**
═══════════════════════════════════════
**长什么样（如「混合与分离」第 2 页）：**
- 页顶：连续 1～2 行说明句（无气泡描边），如「一些物体混合在一起后…」「可以使用一定的方法…」
- 页中：左右两个独立圆角气泡，各自 1～3 行 OCR 碎段（如左「利用能否被磁铁吸引…」；右「利用大小不」「同，可以用筛子」「分离它们。」）
- 页底：浅蓝活动小标题条 + 左侧小人图标 + 「生活中混合物的分离」

**怎么处理（三组各自 merge，不得合成一组）：**
1. 说明段两行 → **instruction_line**，可各自保留或同段 merge（**禁止**与气泡/小标题 merge）
2. 左气泡碎段 → **bubble_id=bub_l**；右气泡三行 → **bubble_id=bub_r**；**不同 bubble_id 不 merge**
3. 页底小人 + 「生活中混合物的分离」→ **subtitle_bar，bar_id=bar_bottom，1 组**

**常见错误（禁止）：**
- 把说明段、左气泡、右气泡、页底小标题 merge 到同一组
- 把右气泡三行碎段各自保留不 merge

═══════════════════════════════════════
【范例 M · 正例】问题情境大框 → role = problem_context_block
═══════════════════════════════════════
**长什么样：** 整页或半页级**单一圆角矩形大框**（常见浅黄/米色底 + 黄色描边），用于课首「问题情境」导入；框内可含：
- 左上角装饰图标（如绿色信箱 + 小人，可能 OCR 成 image）
- 左侧/中部多行说明文字（古籍引文、历史背景等，OCR 常拆成 3～5 个 text 碎框）
- 右侧带细边框的插图（线描古画/照片，OCR 成 image）
- **视觉上同属一个封闭大框**，与框外正文/活动条分离

**怎么处理：**
- 大框内**所有** OCR 原子（图标 image、各行 text、右侧插图 image）→ **同一 context_id**（如 ctx_1），role=**problem_context_block**
- **全部 merge 为 1 个原子**（问题情境模块整块保留，便于后续建块标「问题情境」）
- **section_module** 可填 `问题情境`

**常见错误（禁止）：**
- 只 merge 文字行、漏掉信箱图标或右侧插图
- 把框内文字与框外活动条/气泡 merge
- 把框内多行引文各自保留不 merge

**勿混淆：**
- 浅蓝**活动小标题条**（subtitle_bar）→ 仅条内 merge，不是问题情境大框
- **指南信箱**若在大栏目「科学探究」下且无整页黄框包裹 → 用 section_module 标注，不一定 merge 成 1 块（除非视觉上是同一封闭大框）

═══════════════════════════════════════
【范例 N · 正例】记录过程 / 计划书 → role = process_record_block
═══════════════════════════════════════
**长什么样：** 独立**记事本/计划表视觉块**（常见浅蓝底 + 顶部装订孔/波浪边装饰），整块是一个封闭模块；OCR 常拆成标题、日期、多行正文、编号列表、问答对等碎框。典型两类：

1. **采访录**（如「养蚕采访录」）：标题 + 「采访时间：…」+ 「采访对象：…」+ 「采访记录：」+ 多组「问：…」「答：…」
2. **计划书**（如「养蚕计划」）：标题 + 编号列表「1. … 2. … 3. …」（含框内虚线子区域）

**怎么处理：**
- **每一个**记事本/计划表封闭块内**全部** OCR 原子 → role=**process_record_block**，同一 **record_id**（如 rec_interview / rec_plan）
- **全部 merge 为 1 个原子**（记录过程/计划书模块整块保留）
- **section_module** 可填 `记录过程` 或 `计划书`
- 同页若有 **两块**独立记事本（如上方采访录 + 下方计划书）→ **不同 record_id**，各自 merge 为 1 原子，**禁止**跨块 merge

**常见错误（禁止）：**
- 只 merge 部分问答、漏标题/日期/编号行
- 把块内「问：」「答：」各自拆成独立原子不 merge
- 把采访录与计划书 merge 到同一组
- 把块内文字与块外气泡/活动条 merge

**勿混淆：**
- 块内「问：蚕宝宝吃什么？」是**采访录正文**，不是 dialogue_bubble（无独立气泡描边）
- 页内**对话气泡**（带尾巴/圆角描边）→ dialogue_bubble，不是 process_record_block

═══════════════════════════════════════
其他 role
═══════════════════════════════════════
| role | 含义 |
|------|------|
| instruction_line | 说明句、说一说、引导问句、总结句 |
| illustration | 场景插图、照片（可与 scene_label 同 ill_id 合并） |
| problem_context_block | **问题情境大框**内元素（引文+图标+插图），整框 merge 为 1 原子 |
| process_record_block | **记录过程/计划书**记事本块（采访录、计划表），整块 merge 为 1 原子 |
| self_eval_table | 课末自评价表格（评价句+星级评分），全部合并 |
| noise / other | 噪点或其他 |

## 输出要求

1. **atom_roles**：每个 atom_code 都要有 role；标题条 bar_id、气泡 bubble_id、插图 ill_id、自评 table_id、问题情境 context_id、**记录过程 record_id**；对话组 dialogue_group_id；插图 image_category；板块 section_parent / section_module。
2. **merge_groups**：大标题/小标题/气泡**内部**碎段合并；**scene_label 与 illustration 合并**；**问题情境/记录过程封闭块内全部 merge 为 1 组**。
   - **核心原则**：标注为 `lesson_title_bar` / `subtitle_bar` 的原子，**禁止**与任何非标题条原子出现在同一 merge 组；仅同一 `bar_id` 的标题条组件可 merge
   - **同一 bubble_id 内** merge；**不同 bubble_id 的左右气泡不 merge**（仅共用 dialogue_group_id）
   - **严禁**把课节大标题与「物体是怎么变形的？」引导问句 merge
   - **严禁**把引导问句与活动小标题条（如「改变橡皮泥的形状」）merge 到同一组
   - 小标题条只 merge 条内：左侧小人图标 + 拼音 + 条内汉字
3. **delete_codes**：**仅** page_number、sidebar_unit_title 和 noise 的 atom_code。
   - **严禁**删除活动说明、引导问句、说一说、总结句、插图、作品照、**实验步骤小照片**（培养皿/盐结晶/分离结果等）
   - 蔓延/粘连 OCR 大框：靠 merge 或拆分处理，**不要 delete**
   - **noise 识别**：image 原子如果看着**页面上空白无内容**（不是标题条组件、不是插图、不是页码装饰），或不属于任何板块的孤立空图块 → 标 role=`noise`，放入 delete_codes

4. **大标题 vs 小标题/活动条（严禁跨组合并）**
   - 页顶课节大标题（如「3 混合与分离」）→ **lesson_title_bar，单独 1 组**
   - 其下方浅蓝活动小标题（如「分离盐和芝麻」+ 左侧小人）→ **subtitle_bar，另 1 组**
   - 页中/页底小标题条（如「生活中混合物的分离」+ 左侧小人）→ **subtitle_bar，另 1 组**
   - **禁止**把大标题与小标题/活动条 merge 到同一组；OCR 若产出跨越两行的竖向大框，应拆分而非整框保留

5. **说明段 vs 气泡 vs 小标题（严禁跨组合并）**
   - 页顶说明句（如「一些物体混合在一起后…可以使用一定的方法…」）→ **instruction_line，单独**
   - 左右对话气泡（如「利用能否被磁铁吸引…」「利用大小不同，可以用筛子…」）→ **各 1 个 dialogue_bubble**，气泡内碎段 merge
   - **禁止**把说明段、两个气泡、小标题条 merge 到同一组

6. **问题情境大框（整框 merge 为 1 原子）**
   - 浅黄/米色圆角大框内：信箱图标 + 多行引文 + 右侧插图 → **problem_context_block，同一 context_id，1 组 merge**
   - **禁止**只 merge 文字、漏插图/图标；**禁止**与框外内容 merge

7. **记录过程 / 计划书（记事本块整框 merge 为 1 原子）**
   - 浅蓝记事本块：采访录（标题+时间+对象+问答）或计划书（标题+编号列表）→ **process_record_block，同一 record_id，1 组 merge**
   - 同页两块记事本 → **不同 record_id**；**禁止**把块内「问/答」当 dialogue_bubble

输出 JSON：
- atom_roles: [{ atom_code, role, bar_id?, bubble_id?, ill_id?, context_id?, record_id?, dialogue_group_id?, dialogue_side?, group_slot?, image_category?, section_parent?, section_module? }]
- merge_groups: [{ atom_codes, reason }]
- delete_codes: []
- warnings: []"""

# 发给视觉模型的 few-shot 范例（文案, 文件名）
_CURATE_VISION_EXAMPLES: list[tuple[str, str]] = [
    (
        "title_lesson_orange_num3.png",
        "【大标题变体 · 橙黄渐变宽条】\n"
        "页顶：白圆序号「3」+ 白字「互相依存的动植物」。"
        "序号圈(image)+汉字 → lesson_title_bar，同一 bar_id，merge 为 1 原子。"
        "禁止与下方任何插图/正文 merge。",
    ),
    (
        "title_lesson_teal_num2.png",
        "【大标题变体 · 青绿/teal 宽条】\n"
        "页顶：白圆「2」+「蚕宝宝在长大」。"
        "lesson_title_bar，1 组 merge = 1 原子；禁止与下方插图 merge。",
    ),
    (
        "title_lesson_green_num1.png",
        "【大标题变体 · 绿色宽条】\n"
        "页顶：绿条内白圆「1」+「认识岩石」。"
        "lesson_title_bar，条内全部 merge；禁止 delete 序号圈。",
    ),
    (
        "title_lesson_orange_num4.png",
        "【大标题变体 · 橙黄宽条 + 课节号 4】\n"
        "页顶：白圆序号「4」+ 白字「人类对生物的影响」。"
        "lesson_title_bar，同一 bar_id；禁止与下方插图/小标题 merge。",
    ),
    (
        "subtitle_yellow_icon_split.png",
        "【小标题 · icon+汉字被 OCR 拆成 2 框 → 必须 merge】\n"
        "左侧圆形小人 icon + 黄白胶囊「认识其他动物与植物的关系」。"
        "OCR 常拆成 2 个原子 → **必须同一 bar_id merge 为 1 原子**。"
        "禁止与上方说明句 merge；禁止 delete icon。",
    ),
    (
        "subtitle_cyan_silkworm_food.png",
        "【小标题 · cyan 胶囊 + 小人 icon】\n"
        "「蚕宝宝还喜欢吃什么」：icon + 条内汉字 → subtitle_bar，1 组 merge。"
        "不是气泡问句；禁止与上下插图/说明 merge。",
    ),
    (
        "subtitle_cyan_silkworm_molt.png",
        "【小标题 · cyan 胶囊「观察蚕宝宝换'新衣'」】\n"
        "小人 icon + 条内汉字（含引号）→ subtitle_bar，1 原子。"
        "条后浅色横线仅装饰，仍与 icon+汉字同 bar_id；禁止与气泡 merge。",
    ),
    (
        "subtitle_yellow_survey_ginkgo.png",
        "【小标题 · 黄白胶囊 + 调查类长句】\n"
        "「调查银杏退出濒危植物名单的原因」：icon + 条内长汉字 → subtitle_bar，1 组。"
        "OCR 常拆 icon/汉字 为 2 框，必须 merge；禁止与上方说明句 merge。",
    ),
    (
        "subtitle_green_observe_rocks.png",
        "【小标题 · 浅绿胶囊】\n"
        "「观察岩石」：小人 icon + 绿条汉字 → subtitle_bar，1 原子。"
        "与页顶大标题「认识岩石」是 **不同 bar_id**，不得跨条 merge。",
    ),
    (
        "lesson_title_bar.png",
        "【范例 A · 大标题 lesson_title_bar】\n"
        "页顶青绿条：序号圈「1」+ 拼音 + 「植物角」。"
        "图中所有框（序号圈 image、拼音、汉字、条底）必须同一 bar_id，merge 为 1 个原子。",
    ),
    (
        "subtitle_bar.png",
        "【范例 B · 小标题/活动条 subtitle_bar】\n"
        "浅蓝横条：左侧圆形小人 + 拼音 + 「建植物角」。"
        "小人图标、拼音、汉字必须同一 bar_id，merge 为 1 个原子。",
    ),
    (
        "dialogue_bubble.png",
        "【范例 C1 · 对话气泡 dialogue（淡蓝椭圆）】\n"
        "椭圆气泡内三行碎段「不同的植」「物，各有自己」「的特征！」"
        "属于同一气泡，必须同一 bubble_id，merge 为 1 个原子。",
    ),
    (
        "dialogue_bubble_rounded.png",
        "【范例 C2 · 对话气泡 dialogue（圆角矩形，多色）】\n"
        "上图：白底圆角矩形气泡，两行「游戏中后松开小棍或」「后移动脚者为胜。」→ 同一 bubble_id，merge 为 1 个原子。\n"
        "下图：淡蓝圆角矩形气泡，两行「推和拉，你的感」「觉有什么不同？」→ 同一 bubble_id，merge 为 1 个原子。\n"
        "要点：气泡不一定是椭圆，圆角矩形、橙色、粉色、白底带边框均属于气泡，多行文字必须合并。",
    ),
    (
        "dialogue_bubble_question.png",
        "【范例 C3 · 气泡内问句（OCR 两行碎段）】\n"
        "淡蓝圆角气泡内：「蚕宝宝喜欢」+「什么环境？」→ **同一 bubble_id，merge 为 1 个原子**。\n"
        "这是 dialogue_bubble（气泡台词），**不是** instruction_line / 页内引导问句。\n"
        "严禁：只保留「什么环境？」、漏并「蚕宝宝喜欢」；严禁两行各自独立不 merge。",
    ),
    (
        "dialogue_bubble_multiline_silkworm.png",
        "【范例 C4 · 多行气泡碎段（养蚕课常见）→ 同一 bubble_id merge 为 1 原子】\n"
        "左：橙/蓝圆角气泡内 2～3 行 OCR 碎段，必须全部 merge，例如：\n"
        "·「为了更好地观察记录」+「蚕的生长变化，还可以准」+「备放大镜、尺子……」\n"
        "·「蚕卵 (luǎn)」+「是什么样子？」\n"
        "·「听说它对温度」+「很敏感，我们来做」+「个实验吧！」\n"
        "·「怎样才能让蚕宝宝」+「更快孵 (fū) 化出来呢？」\n"
        "严禁：把含「观察/记录/实验」的行标为 instruction_line 或 activity 说明而拆开；"
        "块内「问/答」式采访录才用 process_record_block，**有气泡描边的仍是 dialogue_bubble**。",
    ),
    (
        "subtitle_not_with_bubble.png",
        "【范例 D · 反例：小标题与气泡必须分开】\n"
        "上图：上方小标题「认识植物角中的植物」+ 下方气泡「这是辣椒，」「它的样子……」。\n"
        "正确：① 小人+小标题文字 → 1 组 merge；② 气泡两行 → 另 1 组 merge。\n"
        "严禁：把小标题文字与气泡文字 merge 到同一组；严禁漏并左侧小人。",
    ),
    (
        "instruction_not_with_bubble.png",
        "【范例 D2 · 反例：任务说明句 ≠ 下方气泡】\n"
        "上方说明句「查阅资料或向他人请教怎样养蚕。」→ instruction_line，**单独 1 个原子**。\n"
        "下方气泡「蚕宝宝喜欢」+「什么环境？」→ dialogue_bubble，**另 1 组 merge**。\n"
        "严禁：把说明句与气泡 merge 到同一组；说明句不是 bubble 内台词。",
    ),
    (
        "page_number.png",
        "【范例 E · 页码 page_number → 删除】\n"
        "页脚/页角：浅色菱形或色块上的孤立数字「9」。"
        "标 role=page_number，加入 delete_codes，不要 merge。\n"
        "（勿与页顶课节序号圈「1 植物角」混淆。）",
    ),
    (
        "scene_label_in_illustration.png",
        "【范例 F · 插图内标牌 scene_label → 并入主插图】\n"
        "大图：孩子与植物架场景（插图 A002-008）。墙上竖牌「植物角」"
        "可能被 OCR 成文字，也可能被框成**小 image**（A002-010）。\n"
        "正确：illustration + scene_label/inset 同一 ill_id，merge 为 1 个插图原子。\n"
        "严禁：把标牌单独保留为独立原子。",
    ),
    (
        "dialogue_not_scene_label.png",
        "【范例 G · 反例：气泡 ≠ 墙上「植物角」标牌】\n"
        "左：气泡「我带来」「了铜钱草。」必须同一 bubble 合并。\n"
        "右：墙上竖牌「植物角」属于插图内 scene_label，只并入主插图。\n"
        "严禁：气泡文字与「植物角」标牌 merge 到同一组。",
    ),
    (
        "sidebar_unit_title.png",
        "【范例 I · 侧边栏单元标题 sidebar_unit_title → 删除】\n"
        "页面最右侧竖排细长条：浅蓝色背景，竖向文字「第三单元 力与形变」。\n"
        "正确：标 role=sidebar_unit_title，加入 delete_codes，直接删除。\n"
        "识别要点：x_start > 0.85 或 x_end < 0.15，height 远大于 width，含「单元」字样。",
    ),
    (
        "self_eval_table.png",
        "【范例 H · 课末自评价表格 self_eval_table】\n"
        "页面右下角浅色圆角卡片：左列评价句（「我能区分推力与拉力，并画出用力方向」"
        "「我对寻找生活中的推力与拉力感兴趣」），右列「1个/2个/3个」+「☆☆☆」星级。\n"
        "正确：表格内所有碎段（评价文字、数字、星号）→ 全部同一 merge 组，合并为 1 个原子。\n"
        "严禁：把数字「1个」「2个」「3个」或星号单独保留，或与表格外的内容合并。",
    ),
    (
        "mixing_separation_page2.png",
        "【范例 L · 正例：说明段 + 双气泡 + 页底小标题（混合与分离 p2）】\n"
        "页顶说明句（instruction_line）：「一些物体混合在一起后…」「可以使用一定的方法…」→ 单独，不与气泡 merge。\n"
        "左气泡「利用能否被磁铁吸引…」→ bubble_id=bub_l，内部 merge。\n"
        "右气泡三行「利用大小不」「同，可以用筛子」「分离它们。」→ bubble_id=bub_r，**必须 merge 为 1 个原子**。\n"
        "页底浅蓝条 + 小人 + 「生活中混合物的分离」→ subtitle_bar，bar_id=bar_bottom，1 组。\n"
        "严禁：说明段、两气泡、页底小标题跨类型 merge 到同一组。",
    ),
    (
        "problem_context_box.png",
        "【范例 M · 问题情境大框 problem_context_block → 整框 merge 为 1 原子】\n"
        "浅黄圆角大框（黄边）：左上绿色信箱图标 + 左侧多行《夏小正》养蚕引文（OCR 多段 text）"
        "+ 右侧线描古画插图（image）。\n"
        "正确：框内**全部**原子同一 context_id=ctx_1，role=problem_context_block，"
        "**merge 为 1 个原子**；section_module=问题情境。\n"
        "严禁：只 merge 引文、漏信箱或插图；严禁与框外活动条/正文 merge。",
    ),
    (
        "process_record_interview.png",
        "【范例 N1 · 记录过程/采访录 process_record_block → 整块 merge 为 1 原子】\n"
        "浅蓝记事本块「养蚕采访录」：标题 + 采访时间 + 采访对象 + 采访记录 + 多组问/答"
        "（OCR 常拆成 8～12 个 text 碎框）。\n"
        "正确：块内**全部**原子同一 record_id=rec_interview，role=process_record_block，"
        "**merge 为 1 个原子**；section_module=记录过程。\n"
        "块内「问：」「答：」是采访正文，**不是** dialogue_bubble。严禁漏并标题/日期/问答行。",
    ),
    (
        "process_record_plan.png",
        "【范例 N2 · 计划书 process_record_block → 整块 merge 为 1 原子】\n"
        "浅蓝记事本块「养蚕计划」：标题 + 编号列表 1～5（含框内虚线区域若干行）。\n"
        "正确：块内**全部**原子同一 record_id=rec_plan，role=process_record_block，"
        "**merge 为 1 个原子**；section_module=计划书。\n"
        "若同页还有采访录 → 用**不同** record_id，两块各自 1 原子，禁止跨块 merge。",
    ),
    (
        "信箱小人.png",
        "【三种小人区分 · 类型二：信箱小人 → role=problem_context_block，不是 subtitle_bar】\n"
        "左上角绿色信箱/邮箱 + 小人物组合图标，出现在整页浅黄圆角大框内（问题情境框）。\n"
        "正确：与大框内引文、右侧插图 → role=**problem_context_block**，同一 context_id。\n"
        "严禁：标为 subtitle_bar；严禁与大框外活动条/标题 merge。\n"
        "对照：此图标在封闭大框内部，不在浅蓝胶囊条左侧。",
    ),
    (
        "小标题旁边小人.png",
        "【三种小人区分 · 类型一：小标题小人 → role=subtitle_bar】\n"
        "左侧**圆形蓝底 + 白色小人剪影**（跳跃/看书姿势），紧贴浅蓝/cyan/黄白胶囊条左侧。\n"
        "正确：与右侧条内汉字 → role=**subtitle_bar**，同一 bar_id，merge 为 1 原子。\n"
        "对照：此图标在图标色带左侧，**不是**大框内信箱、**不是**气泡旁角色。",
    ),
    (
        "气泡文本框旁边小人.png",
        "【三种小人区分 · 类型三：气泡旁角色 → role=dialogue_bubble/dialogue_fragment，不是 subtitle_bar】\n"
        "紧挨对话气泡尾巴的角色插图（坐小车、站姿等），没有蓝底圆圈背景。\n"
        "正确：跟随气泡文字 → role=**dialogue_bubble** 或 **dialogue_fragment**，同一 bubble_id。\n"
        "严禁：标为 subtitle_bar / lesson_title_bar / instruction_line。\n"
        "对照：此角色插图跟气泡在同一区域，**不是**胶囊条左侧的圆形蓝底小人。",
    ),
]


class AtomRoleEntry(BaseModel):
    atom_code: str
    role: AtomRoleKind
    bar_id: str = Field(default="", description="同一标题条/活动条共用")
    bubble_id: str = Field(default="", description="同一对话气泡内 OCR 碎段共用（仅组内 merge）")
    ill_id: str = Field(default="", description="同一主插图+场景标牌共用")
    table_id: str = Field(default="", description="同一自评价表格共用")
    context_id: str = Field(
        default="",
        description="同一问题情境大框内元素共用（整框 merge 为 1 原子）",
    )
    record_id: str = Field(
        default="",
        description="同一记录过程/计划书记事本块内元素共用（整块 merge 为 1 原子）",
    )
    dialogue_group_id: str = Field(
        default="",
        description="同一轮对话左右气泡共用（不 merge，仅关联）",
    )
    dialogue_side: str = Field(
        default="",
        description="对话气泡方位：left / right / center",
    )
    group_slot: int = Field(
        default=0,
        description="同组子序号，如 1=左气泡 2=右气泡",
    )
    image_category: str = Field(
        default="",
        description="跨页插图分类标签，如形变实物图/生活实例图/制陶工序图",
    )
    section_parent: str = Field(
        default="",
        description="所属大栏目，如科学探究",
    )
    section_module: str = Field(
        default="",
        description="大栏目下子模块，如指南信箱/捏陶泥实操",
    )
    group_label: str = Field(
        default="",
        description="可选备注，如左气泡/右气泡",
    )

    @field_validator("atom_code")
    @classmethod
    def _strip_code(cls, v: str) -> str:
        return str(v).strip()


class AtomMergeGroup(BaseModel):
    atom_codes: list[str] = Field(min_length=2, description="要合并的原子编号，至少 2 个")
    reason: str = Field(default="", description="合并理由")

    @field_validator("atom_codes")
    @classmethod
    def _strip_codes(cls, v: list[str]) -> list[str]:
        out = [str(c).strip() for c in v if str(c).strip()]
        if len(out) < 2:
            raise ValueError("merge_groups 每组至少 2 个 atom_code")
        return out


class AtomCuratePlan(BaseModel):
    atom_roles: list[AtomRoleEntry] = Field(default_factory=list)
    merge_groups: list[AtomMergeGroup] = Field(default_factory=list)
    delete_codes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


BAR_ROLES = frozenset({"lesson_title_bar", "subtitle_bar"})
DIALOGUE_ROLES = frozenset({"dialogue_bubble", "dialogue_fragment"})
ILLUSTRATION_ROLES = frozenset({"illustration", "scene_label", "scene_inset"})
TABLE_ROLES = frozenset({"self_eval_table"})
CONTEXT_ROLES = frozenset({"problem_context_block"})
RECORD_ROLES = frozenset({"process_record_block"})


def fill_missing_cluster_ids_for_roles(
    atom_roles: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """LLM 标了 role 但未填 cluster id 时，按视觉亲和补 bubble_id / context_id。"""
    from .atom_curate_heuristics import _dialogue_affinity_clusters

    out = [dict(r) for r in atom_roles]
    by_code = {str(a["atom_code"]): a for a in atoms}

    ctx_missing = [
        r
        for r in out
        if str(r.get("role") or "").strip() in CONTEXT_ROLES
        and not str(r.get("context_id") or "").strip()
    ]
    if len(ctx_missing) >= 2:
        cid = "ctx_auto_1"
        for r in ctx_missing:
            r["context_id"] = cid

    rec_missing = [
        r
        for r in out
        if str(r.get("role") or "").strip() in RECORD_ROLES
        and not str(r.get("record_id") or "").strip()
        and str(r.get("atom_code") or "").strip() in by_code
    ]
    from .atom_curate_heuristics import (
        _cluster_process_record_atoms,
        atom_conflicts_with_record_context_cluster,
    )

    rec_missing = [
        r
        for r in rec_missing
        if not atom_conflicts_with_record_context_cluster(
            by_code[str(r["atom_code"])],
            atoms,
            trusted_role="process_record_block",
        )
    ]
    if len(rec_missing) >= 2:
        rec_atoms = [
            by_code[str(r["atom_code"])]
            for r in rec_missing
            if str(r.get("atom_code") or "").strip() in by_code
        ]
        for i, cluster in enumerate(_cluster_process_record_atoms(rec_atoms), 1):
            if len(cluster) < 2:
                continue
            rid = f"rec_auto_{i}"
            codes = {str(a["atom_code"]) for a in cluster}
            for r in rec_missing:
                if str(r.get("atom_code") or "").strip() in codes:
                    r["record_id"] = rid

    dlg_missing = [
        r
        for r in out
        if str(r.get("role") or "").strip() in DIALOGUE_ROLES
        and not str(r.get("bubble_id") or "").strip()
    ]
    if len(dlg_missing) >= 2:
        dlg_atoms = [
            by_code[str(r["atom_code"])]
            for r in dlg_missing
            if str(r.get("atom_code") or "").strip() in by_code
        ]
        for i, cluster in enumerate(_dialogue_affinity_clusters(dlg_atoms), 1):
            bid = f"bub_auto_{i}"
            codes = {str(a["atom_code"]) for a in cluster}
            for r in dlg_missing:
                if str(r.get("atom_code") or "").strip() in codes:
                    r["bubble_id"] = bid

    return out


def derive_merge_groups_from_roles(
    atom_roles: list[dict[str, Any]],
    *,
    known_codes: set[str] | None = None,
    atoms: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """由大模型角色标注推导合并组（不依赖坐标）。"""
    from .atom_curate_heuristics import (
        atom_conflicts_with_record_context_cluster,
        _filter_dialogue_bubble_merge_codes,
        _split_merge_group_by_dialogue_affinity,
    )

    by_code = {str(a["atom_code"]): a for a in (atoms or [])}
    bar_buckets: dict[str, list[str]] = defaultdict(list)
    bubble_buckets: dict[str, list[str]] = defaultdict(list)
    ill_buckets: dict[str, list[str]] = defaultdict(list)
    table_buckets: dict[str, list[str]] = defaultdict(list)
    context_buckets: dict[str, list[str]] = defaultdict(list)
    record_buckets: dict[str, list[str]] = defaultdict(list)
    for raw in atom_roles:
        code = str(raw.get("atom_code") or "").strip()
        if not code or (known_codes is not None and code not in known_codes):
            continue
        role = str(raw.get("role") or "").strip()
        bar_id = str(raw.get("bar_id") or "").strip()
        bubble_id = str(raw.get("bubble_id") or "").strip()
        ill_id = str(raw.get("ill_id") or "").strip()
        table_id = str(raw.get("table_id") or "").strip()
        context_id = str(raw.get("context_id") or "").strip()
        record_id = str(raw.get("record_id") or "").strip()
        if bar_id and role in BAR_ROLES:
            bar_buckets[bar_id].append(code)
        if role in BAR_ROLES:
            continue
        if bubble_id and role in DIALOGUE_ROLES:
            bubble_buckets[bubble_id].append(code)
        if ill_id and role in ILLUSTRATION_ROLES:
            ill_buckets[ill_id].append(code)
        if table_id and role in TABLE_ROLES:
            table_buckets[table_id].append(code)
        if context_id and role in CONTEXT_ROLES:
            if (
                atoms
                and code in by_code
                and atom_conflicts_with_record_context_cluster(
                    by_code[code], atoms, trusted_role="problem_context_block"
                )
            ):
                continue
            context_buckets[context_id].append(code)
        if record_id and role in RECORD_ROLES:
            if (
                atoms
                and code in by_code
                and atom_conflicts_with_record_context_cluster(
                    by_code[code], atoms, trusted_role="process_record_block"
                )
            ):
                continue
            record_buckets[record_id].append(code)
    groups: list[dict[str, Any]] = []
    for codes in bar_buckets.values():
        if len(codes) >= 2:
            groups.append(
                {
                    "atom_codes": sorted(set(codes)),
                    "reason": "视觉标题条/活动条：图标+拼音+汉字",
                }
            )
    for codes in bubble_buckets.values():
        uniq = sorted(set(codes))
        if len(uniq) < 2:
            continue
        filtered = (
            _filter_dialogue_bubble_merge_codes(uniq, atoms)
            if atoms
            else uniq
        )
        if len(filtered) < 2:
            continue
        if atoms:
            subgroups = _split_merge_group_by_dialogue_affinity(filtered, atoms)
            for sub in subgroups:
                if len(sub) >= 2:
                    groups.append(
                        {
                            "atom_codes": sorted(set(sub)),
                            "reason": "同一对话气泡内 OCR 碎段",
                        }
                    )
        else:
            groups.append(
                {
                    "atom_codes": filtered,
                    "reason": "同一对话气泡内 OCR 碎段",
                }
            )
    for codes in ill_buckets.values():
        if len(codes) >= 2:
            groups.append(
                {
                    "atom_codes": sorted(set(codes)),
                    "reason": "场景插图：标牌文字属于画面背景，并入主插图",
                }
            )
    for codes in table_buckets.values():
        if len(codes) >= 2:
            groups.append(
                {
                    "atom_codes": sorted(set(codes)),
                    "reason": "课末自评价表格：评价句+星级评分合并为1个原子",
                }
            )
    for codes in context_buckets.values():
        if len(codes) >= 2:
            groups.append(
                {
                    "atom_codes": sorted(set(codes)),
                    "reason": "问题情境大框：框内元素整框合并",
                }
            )
    for codes in record_buckets.values():
        if len(codes) >= 2:
            groups.append(
                {
                    "atom_codes": sorted(set(codes)),
                    "reason": "记录过程/计划书：记事本块内元素整块合并",
                }
            )
    return groups


def derive_delete_codes_from_roles(
    atom_roles: list[dict[str, Any]],
    *,
    known_codes: set[str] | None = None,
) -> list[str]:
    """由大模型 role=page_number 推导删除项。"""
    out: list[str] = []
    for raw in atom_roles:
        code = str(raw.get("atom_code") or "").strip()
        if not code or (known_codes is not None and code not in known_codes):
            continue
        if str(raw.get("role") or "").strip() == "page_number":
            out.append(code)
    return sorted(set(out))


def supplement_merge_groups_from_atom_roles(
    plan: dict[str, Any],
    atoms: list[dict[str, Any]],
    atom_roles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """由 atom_roles 的 cluster id 补全 LLM 漏填的 merge_groups（不依赖坐标阈值规则）。"""
    roles = atom_roles if atom_roles is not None else (plan.get("atom_roles") or [])
    if not roles:
        return plan
    known = {str(a["atom_code"]) for a in atoms}
    filled = fill_missing_cluster_ids_for_roles(roles, atoms)
    derived = derive_merge_groups_from_roles(filled, known_codes=known, atoms=atoms)
    merge_groups = union_merge_groups(plan.get("merge_groups") or [], derived)
    warnings = list(plan.get("warnings") or [])
    if derived and len(merge_groups) > len(plan.get("merge_groups") or []):
        warnings.append(
            f"由 atom_roles 补全 {len(merge_groups) - len(plan.get('merge_groups') or [])} 组合并"
        )
    return {**plan, "merge_groups": merge_groups, "atom_roles": filled, "warnings": warnings}


def union_merge_groups(
    primary: list[dict[str, Any]],
    supplemental: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并多来源 merge_groups；primary 优先，不拆 primary 已有组。"""
    out: list[dict[str, Any]] = list(primary)
    used: set[str] = set()
    for g in out:
        used.update(g.get("atom_codes") or [])
    for g in supplemental:
        codes = [str(c).strip() for c in (g.get("atom_codes") or []) if str(c).strip()]
        codes = [c for c in codes if c not in used]
        if len(codes) < 2:
            continue
        if used.intersection(codes):
            continue
        out.append({**g, "atom_codes": codes})
        used.update(codes)
    return out


def build_atom_curate_user_prompt(
    *,
    lesson_name: str,
    unit_title: str,
    page_index: int,
    pdf_page: int | None,
    atoms: list[dict[str, Any]],
) -> str:
    head = f"课时：{unit_title} · {lesson_name}\n教材课内第 {page_index} 页"
    if pdf_page:
        head += f"（PDF 物理页 {pdf_page}）"
    lines = [
        head,
        f"共 {len(atoms)} 个粗原子（请结合附图判断每个框属于哪种视觉单元）：",
        "",
    ]
    for a in atoms:
        code = a.get("atom_code", "")
        typ = a.get("atom_type", "text")
        b = a.get("bbox") or {}
        ys = float(b.get("y_start", 0))
        ye = float(b.get("y_end", 0))
        xs = float(b.get("x_start", 0))
        xe = float(b.get("x_end", 0))
        text = (a.get("content") or a.get("ocr_text") or "").replace("\n", " ")[:80]
        lines.append(
            f"  {code} [{typ}] y={ys:.2f}-{ye:.2f} x={xs:.2f}-{xe:.2f} | {text or '…'}"
        )
    lines.append(
        "\n【第一步 · 必做】先扫描本页全部大标题 lesson_title_bar 与小标题 subtitle_bar："
        "每条分配 bar_id，条内（序号圈/小人 icon/拼音/汉字/条底 image）merge 为恰好 1 原子；"
        "标题 role 禁止与任何非标题原子同组，禁止 delete。\n"
        "再对照范例：大标题变体（橙/青绿/绿宽条）、小标题变体（黄/cyan/绿胶囊 + 小人 icon）、"
        "A/B（经典标题条）、C1～C4（气泡）、D/D2（小标题≠气泡、说明句≠气泡）、"
        "E（页码→删除）、F/G（插图标牌）、H（自评表）、I（侧边栏→删除）、"
        "L/M/N1/N2（说明+气泡+页底小标题 / 问题情境 / 采访录·计划书）、"
        "J/K（image_category / section_*）处理本页："
        "① atom_roles；② merge_groups（标题条隔离、左右气泡不跨 bubble）；③ delete_codes：page_number 等。"
    )
    return "\n".join(lines)


def _example_image_data_url(filename: str) -> str | None:
    """加载内置版面范例图（小标题条 / 对话气泡）。"""
    path = _CURATE_EXAMPLES_DIR / filename
    if not path.is_file():
        logger.warning("curate example image missing: %s", path)
        return None
    data = path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _build_vision_message_content(user_text: str, page_image_url: str) -> list[dict[str, Any]]:
    """版面范例图 + 待整理页图。"""
    n = len(_CURATE_VISION_EXAMPLES)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"以下 {n} 张为版面范例（含反例、页码、标牌、说明段+双气泡等），请牢记规则后再处理【待整理页】。",
        },
    ]
    for filename, caption in _CURATE_VISION_EXAMPLES:
        content.append({"type": "text", "text": caption})
        url = _example_image_data_url(filename)
        if url:
            content.append({"type": "image_url", "image_url": {"url": url}})
    content.append({"type": "text", "text": "【待整理页】\n" + user_text})
    content.append({"type": "image_url", "image_url": {"url": page_image_url}})
    return content


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    data = json.loads(raw)
    return AtomCuratePlan.model_validate(data).model_dump()


def _invoke_vision_llm(user_text: str, image_url: str) -> AtomCuratePlan:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(
        max_tokens=4096,
        model=llm_atom_curate_model(),
        timeout=llm_atom_curate_timeout(),
    )
    content = _build_vision_message_content(user_text, image_url)
    messages = [
        SystemMessage(content=ATOM_CURATE_SYSTEM),
        HumanMessage(content=content),
    ]
    try:
        structured = llm.with_structured_output(AtomCuratePlan)
        return structured.invoke(messages)
    except Exception as exc:
        logger.warning("atom curate structured output failed: %s", exc)
        msg = llm.invoke(messages)
        raw = getattr(msg, "content", str(msg))
        data = _parse_json_fallback(raw)
        return AtomCuratePlan.model_validate(data)


def suggest_atom_curate_plan_with_llm(
    *,
    lesson_name: str,
    unit_title: str,
    page_index: int,
    pdf_page: int | None,
    atoms: list[dict[str, Any]],
    image_url: str,
) -> dict[str, Any]:
    if not llm_enabled():
        raise RuntimeError("LLM 未启用或未配置凭证 / LLM_MODEL")
    if not atoms:
        raise ValueError("本页尚无原子，请先 OCR 提取")
    if not image_url:
        raise ValueError("本页教材图不可用")

    known_codes = {str(a["atom_code"]) for a in atoms}
    user_prompt = build_atom_curate_user_prompt(
        lesson_name=lesson_name,
        unit_title=unit_title,
        page_index=page_index,
        pdf_page=pdf_page,
        atoms=atoms,
    )
    plan = _invoke_vision_llm(user_prompt, image_url)
    atom_roles = [r.model_dump() for r in plan.atom_roles]
    atom_roles = fill_missing_cluster_ids_for_roles(atom_roles, atoms)
    explicit_merges = [g.model_dump() for g in plan.merge_groups]
    derived_merges = derive_merge_groups_from_roles(
        atom_roles, known_codes=known_codes, atoms=atoms
    )
    merge_groups = union_merge_groups(explicit_merges, derived_merges)
    if derived_merges and len(merge_groups) > len(explicit_merges):
        plan.warnings.append(
            f"由 atom_roles 补全 {len(merge_groups) - len(explicit_merges)} 组合并"
        )

    derived_deletes = derive_delete_codes_from_roles(
        atom_roles, known_codes=known_codes
    )
    delete_codes = sorted(
        set(str(c).strip() for c in plan.delete_codes if str(c).strip())
        | set(derived_deletes)
    )
    if derived_deletes and len(delete_codes) > len(plan.delete_codes):
        plan.warnings.append(
            f"由 atom_roles 补全 {len(delete_codes) - len(plan.delete_codes)} 个页码删除"
        )

    return {
        "atom_roles": atom_roles,
        "merge_groups": merge_groups,
        "delete_codes": delete_codes,
        "warnings": list(plan.warnings),
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source": "llm_vision",
        "model": llm_atom_curate_model(),
    }


def validate_atom_curate_plan(
    *,
    plan: dict[str, Any],
    known_codes: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """校验 LLM 输出；返回规范化 plan 与 warnings。"""
    warnings: list[str] = []
    raw_deletes = [str(c).strip() for c in (plan.get("delete_codes") or []) if str(c).strip()]
    delete_set: set[str] = set()
    for c in raw_deletes:
        if c not in known_codes:
            warnings.append(f"忽略未知删除项 {c}")
            continue
        delete_set.add(c)

    merge_out: list[dict[str, Any]] = []
    used_in_merge: set[str] = set()
    for i, grp in enumerate(plan.get("merge_groups") or []):
        if not isinstance(grp, dict):
            warnings.append(f"merge_groups[{i}] 格式无效，已跳过")
            continue
        codes = [str(c).strip() for c in (grp.get("atom_codes") or []) if str(c).strip()]
        codes = [c for c in codes if c in known_codes and c not in delete_set]
        if len(codes) < 2:
            if codes:
                warnings.append(f"合并组 {codes} 不足 2 个有效原子，已跳过")
            continue
        overlap = used_in_merge.intersection(codes)
        if overlap:
            warnings.append(f"原子 {sorted(overlap)} 重复出现在多个合并组，已跳过该组")
            continue
        used_in_merge.update(codes)
        merge_out.append(
            {
                "atom_codes": codes,
                "reason": str(grp.get("reason") or "").strip(),
            }
        )

    for c in delete_set:
        if c in used_in_merge:
            warnings.append(f"原子 {c} 同时在删除与合并中，以删除为准、不合并")

    final_deletes = sorted(delete_set)
    out_plan = {
        "merge_groups": merge_out,
        "delete_codes": final_deletes,
        "warnings": list(plan.get("warnings") or []) + warnings,
    }
    if plan.get("atom_roles"):
        out_plan["atom_roles"] = plan.get("atom_roles")
    if plan.get("source"):
        out_plan["source"] = plan.get("source")
    split_codes = [
        str(c).strip()
        for c in (plan.get("split_codes") or [])
        if str(c).strip() in known_codes
    ]
    if split_codes:
        out_plan["split_codes"] = sorted(set(split_codes))
    return out_plan, warnings
