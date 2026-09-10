"""教材页文字原子：Doubao 视觉模型返回带 bbox 的语义文字块（双轨前置1）。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import build_chat_openai, llm_enabled, llm_vision_model

logger = logging.getLogger(__name__)

_CACHE_DIR = Path(__file__).resolve().parents[4] / "data" / "base_data" / "cache" / "page_text"

# 提示词变更时递增，避免沿用旧缓存（含：夹注旁汉字禁漏）
PAGE_TEXT_PROMPT_VERSION = 31

# 五类声调：轻声 / 一声 / 二声 / 三声 / 四声（以 e 为例；输出须用下列预组合字母）
_PINYIN_FIVE_TONES_GUIDE = """
【汉语拼音五类音标·强制】以元音 e 为例，共五类：
  轻声（零声）→ e （元音顶无任何调号）
  一声 → ē （平直横线）
  二声 → é （左低右高上扬）
  三声 → ě （ˇ 尖角/帽子，先降后升）
  四声 → è （左高右低下降斜线）
其余元音同理，必须输出下列预组合字符（禁止写成 e1/e2 或把调号写成独立符号）：
  a：a ā á ǎ à
  o：o ō ó ǒ ò
  e：e ē é ě è
  i：i ī í ǐ ì
  u：u ū ú ǔ ù
  ü：ü ǖ ǘ ǚ ǜ
判调只看页上该音节主元音顶的调号形状；禁止用词典读音改调；页上是一声就写一声，是四声就写四声。
【一声 vs 三声】平直短横（中间不下凹）→ 一声；ˇ（中间明显下凹）→ 三声。若尖角被压扁、看不清下凹，写一声，不要写成三声。
【易混案例·仅作形状参考】平横易被误认成四声斜线或三声尖角：若主元音顶是水平短横，必须写一声（如 mū/cān/sē），禁止写成四声 mù/càn/sè 或三声 mǔ/cǎn/sě；只有看到清晰 ˇ 下凹才写三声。放大核对调号两端是否等高、中间是否下凹。
"""

PAGE_TEXT_SYSTEM = """你是 K12 教材扫描页文字分区专家。根据附带的教材页图，划出「可独立匹配」的文字区域并识别框内全部可见文字（含汉字与拼音）。

规则：
1. 每个区域输出 role、text、bbox（相对整页宽高 0~1 小数，0≤x_start<x_end≤1、0≤y_start<y_end≤1）。
2. role 取值：
   - title：栏目标题、课节大标题、加粗板块名（如「拓展迁移」「反思评价」「口语交际」）
   - text：正文段落、活动说明、问题句、对话文字、提示便签
   - caption：图下/图旁一行说明、步骤标签（如「制泥」「成形」）
   - activity：以「活动」「探究」「试一试」等开头的整块活动指导
3. text 为框内完整 OCR 文字（保留标点；多行用换行）；不要编造看不见的字。
3b. 【页角/栏首小栏目名·强制出框】页左上/右上或栏首的短栏目字（如「阅读」「习作」「口语交际」「语文园地」等，常为竖排色条、色块小标题或装饰框内二字）必须单独输出为 **title**（或 text）框，**禁止漏识**；即使字号小、偏在页角、与插图叠印、或看起来像装饰，也要框出印刷字并写入 text。
   - 正确：单独一框「阅读」；错误：整页文字齐全却漏掉页角「阅读」；错误：把「阅读」当成页眉丢掉。
   - 【与页眉区分】规则 11 的「页眉页脚」仅指页码、书名页眉条、装饰线等；**不含**上述栏目标题。
4. 【夹注拼音·可见才写·一字一括号·语文尤其重要】
   - 【可见才写】仅当页图上该汉字**正上方确实印有**拼音音节时，才写成「字(pīn)」；页上没有注音的字**禁止**加括号。
   - 【严禁凭空捏造】禁止凭读音知识、字典或「生字常注音」习惯给未印拼音的字补音；宁可漏报也不要瞎加。
   - 【严禁漏识】请逐行扫视汉字上方空隙：凡页上印了注音，必须挂到**正下方那个字**，不可跳过句中/句末的注音。
   - 【夹注不吞邻字·最高优先级】写「字(pīn)」时，**只在该字后加括号**；其左、右相邻且页上印出的汉字必须原样保留，**禁止**因注视拼音而漏掉前后字。
     - 错误案例：页上「很简朴」且仅「朴」上有音，却输出「很朴(pǔ)」（漏「简」）或「简(pǔ)」（漏「朴」字本身写成别的）。
     - 正确：输出「很简朴(pǔ)」。多字词同理，如「粗糙」仅「糙」有音 →「粗糙(cāo)」，不可写成「粗(cāo)」或「糙(cāo)」而丢掉另一字。
   - 【多字词只挂有音的字】若「敏锐」仅「锐」上有 ruì，应写「敏锐(ruì)」；**禁止**写成「敏(mǐn)锐(ruì)」除非「敏」上确实也印了拼音。
   - 格式：每个带注音的汉字各自紧跟一个半角括号，如「盐(yán)官镇」「鼎(dǐng)沸(fèi)」「拢(lǒng)」。
   - 【严禁】把相邻两字的拼音塞进同一个括号，禁止「鼎(dǐng fèi)沸」「闷(mèn léi)雷」这类写法。
   - 【bbox 必须盖住注音】正文夹注拼音时，bbox 的 y_start 须上扩到拼音顶（不可只贴汉字顶边把 pā/dàng 等裁在框外）；**禁止**把单个夹注音节拆成远离正文的独立小框（如单独输出「dǒng」）；夹注只能写在正文字后括号里。
   - 若一整行纯拼音且下方没有对应生字表汉字，可单独保留该拼音行；读音表见规则 6，不得拆成「纯拼音行 + 汉字行」。
""" + _PINYIN_FIVE_TONES_GUIDE + """
   - 放大看夹注音节主元音顶的调号：横线→一声，ˇ→三声，下降斜线→四声；不要把一声横线认成四声，也不要把三声尖角认成一声横线。禁止用词典/语感改调。
   - 【挂对字】注音必须挂在其正下方那个汉字上（如「古董」音在「董」上则写 古董(…)，禁止 古(…)董）；音节与调号一律照页上可见字形抄，禁止再单独框出音节。
   - 感叹「啊！」课本注音几乎一律是一声 **ā**，禁止写成 á，也禁止写成无调 a。
5. 【角标 / 上标注释序号必须保留·语文尤重要】
   - 课题、诗题、句中/句末旁的小圆圈脚注序号必须写入对应 text，紧跟被注释的字词（或夹注拼音括号）之后，如「花牛歌①」「真珠③」「缘(yuán)③」。
   - 【形态强制·带圈】页上脚注角标无论印成实心圈、虚线圈还是偏上小字，一律输出 Unicode 带圈数字 ①②③④⑤⑥⑦⑧⑨⑩。
   - 【禁止】写成普通阿拉伯数字「2」「3」，或上标「²」「³」；错误案例：页上同是圈号，一侧写成 ②、一侧写成 2 / ³。
   - 不得因字号偏小、偏上而漏识；不要把角标拆成单独空框；页底脚注正文仍按正文/注释规则收录。
5b. 【课节标题序号圈 + 选学星·强制】
   - 页顶/栏首课标题左侧的 **带圈课序号**（虚线圈或实线圈里的 1/2/3…）必须输出 Unicode 带圈数字 ①②③④⑤⑥⑦⑧⑨⑩，**禁止**写成普通阿拉伯数字「3」。
   - 圈旁或右上角的 **选学星号** * / ＊（略读/选读课标记）必须保留，紧跟圈号，如「③* 现代诗二首」。
   - 正确示例：「③* 现代诗二首」「① 观潮」；错误示例：「3 现代诗二首」（漏圈）、「3 现代诗二首」（漏星）、「③ 现代诗二首」（有星却漏写）。
6. 【读音表 / 生字注音·强制一字一音】
   - 页底或栏内「字上方拼音、下方汉字」的读音表，合并为 **一个** text 框。
   - text 只输出「字(pīn) 字(pīn) …」单行形式，如「巢(cháo) 苇(wěi) 瞬(shùn)」；**禁止**先单独输出一行纯拼音再输出一行汉字。
6b. 【识字加油站 / 形近字对照表·合框】栏目「识字加油站」下多行「冈—纲(gāng)（提纲）」类对照条目，必须合并为 **一个** text 框（行与行用换行），**禁止**按行拆成多个框；栏目标题「识字加油站」仍单独 title/text；表旁问句（如「你还能说出类似的字吗？」）单独一框，勿并进对照表。
7. 【正文按段分框】同一栏不要拆成过多碎行；以自然段为单位——首行缩进、段首空格、或段间明显空白分隔的，各为 **一个** text 框。禁止把相邻两段**叙述正文**并进同一框。
7b. 【禁止截断漏字·强制逐字抄全】框内印刷汉字必须与页图一一对应、一字不漏、一字不多：
   - 引号内口号（如「从不改变」）不得漏成「从不改」；段首段尾、问句勿吞字；页上有的段落/问题句都要出框。
   - 【句中漏字最严重】禁止漏掉词组中间或夹注拼音前的字（错误案例：页上「住所是很简朴」写成「住所是很朴」）。
   - 抄写完成后在脑中按行核对：页上每个可见汉字是否都出现在 text 中；水印字除外。
7c. 【页边残片勿入库】页顶/页边被裁切的半截字加省略号（如单独的「帽……」「帽......」）不要单独成框。
7c2. 【左/右侧栏旁批·强制出框】主栏左右两侧的窄栏问题句、旁批、提示便签（常为多行竖排/折行问句）必须**各自单独**一个 text 框，禁止漏掉；即使被手写红笔圈画、灰线框住或旁边有手写字，也要识别**印刷体**旁批全文。禁止只输出主栏正文而丢掉侧栏问句。
7d. 【课前导读/学习提示·合框】课题正下方连续的学习提示句（如「默读课文…把问题分类。」与紧接下句「选出你认为值得思考的问题，并尝试解决。」）视为**同一提示块**，必须合并为 **一个** text 框（可用换行），**禁止**拆成两个原子；直到出现叙述正文（如「孙膑是…」）再另起新框。
7e. 【古诗/现代诗正文·合框·强制】同一首诗的诗题、作者行（如「〔唐〕某人」）、诗句正文视为**同一语义块**：诗句无论竖排还是横排分行，必须合并为 **一个** text 框（行与行用换行），**禁止**一句一行拆成多个原子。
   - 下一首诗另起新框；诗旁插图不要并进文字框。
   - 正确：一首诗一个框（题+作者+各句换行）；错误：把「一道……」「半江……」等每句各拆一框。
7f. 【「注释」脚注栏·合框·强制】标题为「注释」的脚注区（含其下 ①②③… 多条释义）必须合并为 **一个** text 框：可先写「注释」再换行写各条，或「注释」作 title、释义正文合为一个 text；**禁止**把每条 ①/②/③ 拆成独立原子。
   - 此规则优先于「列表一项一框」：脚注序号不是活动问题列表。
   - 正确：一个注释框含多条；错误：①、②、③ 各一框。
8. 【对话气泡 / 对话框】气泡内全部台词合并为 **一个** text 区域（多行用换行），禁止按行拆成多个框；也不要与气泡外正文合并。
9. 【提示便签 / 贴士框】页角小贴士、螺旋便签内多行说明合并为 **一个** text 区域。
10. 【水印严禁入库】斜向淡色水印字样一律不要框选、不要写入 text，包括但不限于：
   「严禁外传」「违者必究」「传递追责」「追责」「(CHHT)」「CHHT」「内部资料」「仅供参考」。
11. 不要框选：纯照片/插图主体、装饰图标、页码、纯装饰页眉条/页脚线、红色印章、上述水印。
    【例外】页角栏目标题（「阅读」「习作」等）必须出框，见规则 3b，不得当页眉省略。
12. 图内必要说明字可单独 caption 框；侧边栏栏目名用 title。
13. 【地图/示意图】地图内地名、省界注记、图例字**不要**用大 text 框盖住整幅地图；若需收录，用多个小 caption 框，或并入图旁资料袋说明，禁止把整幅地图当成一个 text 区。
14. 【资料袋】「资料袋」标题与导语正文各为文字框，与内嵌地图/配图分框；正文框不要把内嵌插图区域包进去。
15. 【列表一项一框·强制】以「◇ / ◆ / ◊ / ● / • / ○ / □ / ①②③」等起头的**活动/思考问题列表**，**每一条单独一个 text 框**，即使视觉上同属一个红框、色块或列表区也禁止合并；一条内的多行（换行）仍属同一框。
   - 【例外】规则 7f 的「注释」脚注①②③、以及读音表/对照表，不适用本条，须合框。
16. 【列表方块符必留】题干前的空心/实心方块、菱形项目符号（◇◆◊□■ 等）必须写入 text 开头，**禁止漏识、禁止改成普通圆点或省略**；新旧版式只要页上有该符号，text 就必须带上。"""


class LlmTextRegion(BaseModel):
    role: Literal["title", "text", "activity", "caption"] = "text"
    text: str = ""
    x_start: float = Field(ge=0.0, le=1.0)
    y_start: float = Field(ge=0.0, le=1.0)
    x_end: float = Field(ge=0.0, le=1.0)
    y_end: float = Field(ge=0.0, le=1.0)

    @field_validator("x_end")
    @classmethod
    def _x_end_after_start(cls, v: float, info) -> float:
        xs = info.data.get("x_start", 0.0)
        if v <= xs:
            return min(1.0, xs + 0.02)
        return v

    @field_validator("y_end")
    @classmethod
    def _y_end_after_start(cls, v: float, info) -> float:
        ys = info.data.get("y_start", 0.0)
        if v <= ys:
            return min(1.0, ys + 0.02)
        return v


class LlmPageText(BaseModel):
    regions: list[LlmTextRegion] = Field(default_factory=list)


class LlmPinyinAudit(BaseModel):
    """注音校对：与输入 regions 等长的校对后 text。"""

    corrected_texts: list[str] = Field(default_factory=list)


PAGE_TEXT_PINYIN_AUDIT_SYSTEM = """你是 K12 语文教材「夹注拼音」校对员。对照页图，修正各段 OCR text 里的汉字夹注拼音；并可补回因夹注而漏掉的相邻汉字。

硬性规则：
1. 【删捏造】若某字正上方页图上没有印拼音，必须删掉该字后的 (pīn)；禁止凭字典/语感补音。
2. 【补漏识】若某字正上方页图上印有拼音，但 text 未写或写错音节/声调，必须按页上可见字形写成 字(pīn)。
3. 【五类音标】以 e 为例：轻声 e、一声 ē、二声 é、三声 ě、四声 è；a/o/i/u/ü 同理用预组合字母。
   只按页上主元音顶的调号改写：平横→一声，上扬→二声，ˇ→三声，下降斜线→四声，无调号→轻声。
   禁止用词典把页上的一声改成四声、或把三声改成一声。
   【易混案例】平横→一声（如 mū/cān/sē），不要误写成四声 mù/càn/sè 或三声 mǔ/cǎn/sě；只有清晰 ˇ 才写三声。
   【挂对字】音在哪个字上方就挂哪个字；禁止 古(dǒng)董 这类错挂。
4. 【一字一括号】每个有注音的字各自一对半角括号；多字词只给页上有音的字加括号。
5. 【补漏汉字】对照页图：若夹注音节前/后页上还有汉字，但 text 漏了，必须按页图补回（错误案例：「很朴(pǔ)」而页上是「很简朴」→ 改为「很简朴(pǔ)」）。禁止删改页上已有的正确汉字。
6. 【其余不动】标点、换行、角标 ①②③、列表方块符等一律保持原样；不要合并/拆分段落。
7. 输出 corrected_texts，长度必须与输入段数相同；无改动的段原样返回。"""


class LlmPinyinToneVerify(BaseModel):
    """声调专项校对：与输入夹注项等长的纠正后音节（不含括号）。"""

    corrected_syllables: list[str] = Field(default_factory=list)


PAGE_TEXT_TONE_VERIFY_SYSTEM = """你是汉语拼音「声调调号」校对员。只根据页图（含局部放大图）上可见的调号形状，纠正每个夹注音节的声调字母。

五类音标（以 e 为例；o/u 等同理）：
  轻声 → e（无调号）
  一声 → ē（平直横线）
  二声 → é（上扬）
  三声 → ě（ˇ 尖角，明显下凹）
  四声 → è（下降斜线）

硬性规则：
1. 先看该条对应的【局部放大图】（若有），再对照整页图；只看该汉字正上方拼音主元音顶的调号。
2. 【一声 vs 三声·最易混·禁止词典】
   - 一声：调号是平直短横，两端几乎等高，中间**不下凹** → ō/ū/ē/ā/ī…
   - 三声：调号是 ˇ，中间**明显下凹**、两侧上翘 → ǒ/ǔ/ě/ǎ/ǐ…
   - 扫描页上三声尖角常被压扁，看起来像短横；若看不到清晰下凹尖角，判为一声，不要判成三声。
   - 【易混案例】平横常被误成四声斜线：若是水平短横，写一声（如 mū/cān/sē），禁止写成 mù/càn/sè。
   - 禁止用该汉字的常用读音/词典改调；页上是一声就写一声。
3. 只改声调字母，尽量保持声母与韵母骨架不变；不要增删整条注音，不要改汉字。
4. 输出 corrected_syllables，长度必须与输入项数相同；无改动则原样返回该 syllable。"""


def page_text_pinyin_audit_enabled() -> bool:
    import os

    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(repo_root / ".env", encoding="utf-8-sig", override=True)
    raw = os.getenv("PAGE_TEXT_PINYIN_AUDIT", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def page_text_tone_verify_enabled() -> bool:
    """第二轮声调专项校准；默认关闭，主路径只做一轮拼音校准。

    需要双轮时设 PAGE_TEXT_TONE_VERIFY=1（函数 _invoke_pinyin_tone_verify_llm 仍保留）。
    """
    import os

    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(repo_root / ".env", encoding="utf-8-sig", override=True)
    raw = os.getenv("PAGE_TEXT_TONE_VERIFY", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def apply_pinyin_audit_texts(
    regions: list[LlmTextRegion],
    corrected_texts: list[str] | None,
) -> list[LlmTextRegion]:
    """把校对后的 text 写回 regions；长度不一致则整页保持原样。"""
    if not regions or not corrected_texts:
        return regions
    if len(corrected_texts) != len(regions):
        logger.warning(
            "pinyin audit length mismatch: regions=%s corrected=%s",
            len(regions),
            len(corrected_texts),
        )
        return regions
    out: list[LlmTextRegion] = []
    for region, fixed in zip(regions, corrected_texts):
        text = (fixed if fixed is not None else region.text) or ""
        text = text.strip() or (region.text or "")
        out.append(region.model_copy(update={"text": text}))
    return out


def _region_needs_pinyin_audit(text: str) -> bool:
    """含汉字的正文段才值得二次校对注音。"""
    t = text or ""
    return any("\u4e00" <= ch <= "\u9fff" for ch in t)


def _invoke_pinyin_audit_llm(
    image_url: str,
    *,
    page_index: int,
    regions: list[LlmTextRegion],
) -> list[LlmTextRegion]:
    """对照页图二次校对夹注拼音（删捏造、补漏识）。失败则返回原文。"""
    if not regions or not page_text_pinyin_audit_enabled():
        return regions
    indexed = [(i, r) for i, r in enumerate(regions) if _region_needs_pinyin_audit(r.text or "")]
    if not indexed:
        return regions

    from langchain_core.messages import HumanMessage, SystemMessage

    payload = [
        {"index": i, "text": (r.text or "")[:4000]}
        for i, r in indexed
    ]
    llm = build_chat_openai(max_tokens=4096, model=llm_vision_model(), timeout=120.0)
    messages = [
        SystemMessage(content=PAGE_TEXT_PINYIN_AUDIT_SYSTEM),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"请校对【教材 P{int(page_index)}】下列 {len(payload)} 段 text 的夹注拼音。"
                        "对照页图：删掉页上不存在的注音，补上页上存在但漏写/写错的注音；"
                        "若夹注前/后页上还有汉字被 OCR 漏掉，按页图补回汉字；"
                        "勿改标点与段落结构。按输入顺序返回等长的 corrected_texts（可只含这些段的校对结果，"
                        "corrected_texts[k] 对应下方 JSON 第 k 项）。\n"
                        + json.dumps(payload, ensure_ascii=False)
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        ),
    ]
    try:
        structured = llm.with_structured_output(LlmPinyinAudit)
        audit = structured.invoke(messages)
        fixed_list = list(audit.corrected_texts or [])
        if len(fixed_list) != len(payload):
            logger.warning(
                "pinyin audit subset length mismatch P%s: in=%s out=%s",
                page_index,
                len(payload),
                len(fixed_list),
            )
            return regions
        merged = list(regions)
        for (idx, _), fixed in zip(indexed, fixed_list):
            text = (fixed or "").strip() or (merged[idx].text or "")
            merged[idx] = merged[idx].model_copy(update={"text": text})
        return merged
    except Exception as exc:
        logger.warning("pinyin audit P%s failed, keep raw OCR: %s", page_index, exc)
        return regions


_INLINE_HAN_SYL_RE = re.compile(
    r"([\u4e00-\u9fff])\(([A-Za-züÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+)\)"
)


def collect_inline_pinyin_items(regions: list[LlmTextRegion]) -> list[dict[str, Any]]:
    """收集各区域中的 字(音节) 夹注，供声调专项校对。"""
    items: list[dict[str, Any]] = []
    for ri, region in enumerate(regions or []):
        text = region.text or ""
        for m in _INLINE_HAN_SYL_RE.finditer(text):
            start, end = m.start(), m.end()
            items.append(
                {
                    "id": len(items),
                    "region_index": ri,
                    "han": m.group(1),
                    "syllable": m.group(2),
                    "span": [start, end],
                    "context": text[max(0, start - 10) : min(len(text), end + 10)],
                }
            )
    return items


def apply_tone_syllable_fixes(
    regions: list[LlmTextRegion],
    corrected_syllables: list[str] | None,
) -> list[LlmTextRegion]:
    """按 collect_inline_pinyin_items 的顺序写回纠正后的音节。"""
    items = collect_inline_pinyin_items(regions)
    if not items or not corrected_syllables:
        return regions
    if len(corrected_syllables) != len(items):
        logger.warning(
            "tone verify length mismatch: items=%s corrected=%s",
            len(items),
            len(corrected_syllables),
        )
        return regions

    by_region: dict[int, list[tuple[int, int, str, str]]] = {}
    for item, syl in zip(items, corrected_syllables):
        new_syl = (syl or "").strip() or item["syllable"]
        new_syl = re.sub(r"[()（）\s]", "", new_syl)
        if not new_syl:
            new_syl = item["syllable"]
        span = item["span"]
        by_region.setdefault(int(item["region_index"]), []).append(
            (span[0], span[1], item["han"], new_syl)
        )

    out = list(regions)
    for ri, fixes in by_region.items():
        text = out[ri].text or ""
        for start, end, han, new_syl in sorted(fixes, key=lambda x: x[0], reverse=True):
            text = text[:start] + f"{han}({new_syl})" + text[end:]
        out[ri] = out[ri].model_copy(update={"text": text})
    return out


def _decode_data_url_to_png_bytes(image_url: str) -> bytes | None:
    raw = (image_url or "").strip()
    if not raw.startswith("data:") or "," not in raw:
        return None
    try:
        return base64.standard_b64decode(raw.split(",", 1)[1])
    except Exception:
        return None


def _png_bytes_to_data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.standard_b64encode(data).decode("ascii")


def _strip_inline_pinyin_for_layout(text: str) -> str:
    """去掉夹注括号，近似印刷页上的汉字/标点排版。"""
    return _INLINE_HAN_SYL_RE.sub(r"\1", text or "")


def estimate_inline_pinyin_crop_box(
    region: LlmTextRegion,
    *,
    span_start: int,
) -> tuple[float, float, float, float]:
    """按区域 bbox + 文本位置，估计夹注音节（含上方调号）的相对裁剪框 0~1。"""
    text = region.text or ""
    rx0 = float(region.x_start)
    ry0 = float(region.y_start)
    rx1 = float(region.x_end)
    ry1 = float(region.y_end)
    lines = text.split("\n")
    pos = 0
    line_idx = 0
    col = 0
    for li, line in enumerate(lines):
        end = pos + len(line)
        if span_start <= end:
            line_idx = li
            col = max(0, span_start - pos)
            break
        pos = end + 1
    else:
        line_idx = max(0, len(lines) - 1)
        col = len(lines[line_idx]) if lines else 0

    line = lines[line_idx] if lines else ""
    plain_line = _strip_inline_pinyin_for_layout(line)
    plain_before = _strip_inline_pinyin_for_layout(line[:col])
    hans_line = re.findall(r"[\u4e00-\u9fff]", plain_line)
    hans_before = re.findall(r"[\u4e00-\u9fff]", plain_before)
    n = max(1, len(hans_line))
    k = min(len(hans_before), n - 1)

    n_lines = max(1, len(lines))
    line_h = max((ry1 - ry0) / n_lines, 0.012)
    ly0 = ry0 + line_idx * line_h
    # 上扩覆盖注音带
    cy0 = max(0.0, ly0 - line_h * 0.55)
    cy1 = min(1.0, ly0 + line_h * 0.95)
    char_w = max((rx1 - rx0) / n, 0.012)
    cx = rx0 + (rx1 - rx0) * ((k + 0.5) / n)
    cx0 = max(0.0, cx - char_w * 1.35)
    cx1 = min(1.0, cx + char_w * 1.35)
    return cx0, cy0, cx1, cy1


def crop_inline_pinyin_zoom_data_urls(
    image_url: str,
    regions: list[LlmTextRegion],
    items: list[dict[str, Any]],
    *,
    max_items: int = 24,
) -> list[tuple[int, str]]:
    """为夹注项生成局部放大 data URL，供声调校对。返回 (item_id, data_url)。"""
    from io import BytesIO

    try:
        from PIL import Image
    except Exception:
        return []

    raw = _decode_data_url_to_png_bytes(image_url)
    if not raw or not items:
        return []
    try:
        im = Image.open(BytesIO(raw)).convert("RGB")
    except Exception:
        return []
    w, h = im.size
    out: list[tuple[int, str]] = []
    for it in items[: max(0, int(max_items))]:
        ri = int(it["region_index"])
        if ri < 0 or ri >= len(regions):
            continue
        region = regions[ri]
        span = it.get("span") or [0, 0]
        cx0, cy0, cx1, cy1 = estimate_inline_pinyin_crop_box(region, span_start=int(span[0]))
        box = (
            max(0, int(cx0 * w)),
            max(0, int(cy0 * h)),
            min(w, int(cx1 * w)),
            min(h, int(cy1 * h)),
        )
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            continue
        crop = im.crop(box)
        # 放大便于看清调号
        scale = max(2, min(6, int(280 / max(crop.width, 1))))
        crop = crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.LANCZOS)
        buf = BytesIO()
        crop.save(buf, format="PNG")
        out.append((int(it["id"]), _png_bytes_to_data_url(buf.getvalue())))
    return out


def _invoke_pinyin_tone_verify_llm(
    image_url: str,
    *,
    page_index: int,
    regions: list[LlmTextRegion],
) -> list[LlmTextRegion]:
    """第三轮：只校对已有夹注的声调字母（按页上调号形状，禁止词典改调）。"""
    if not regions or not page_text_tone_verify_enabled():
        return regions
    items = collect_inline_pinyin_items(regions)
    if not items:
        return regions

    from langchain_core.messages import HumanMessage, SystemMessage

    payload = [
        {
            "id": it["id"],
            "han": it["han"],
            "syllable": it["syllable"],
            "context": it["context"],
        }
        for it in items
    ]
    zooms = crop_inline_pinyin_zoom_data_urls(image_url, regions, items)
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"请校对【教材 P{int(page_index)}】下列 {len(payload)} 条夹注的声调。"
                "后面附有整页图，以及按 id 编号的局部放大图（只含该字及上方拼音）。"
                "优先看局部放大图主元音顶的调号：平横=一声，清晰下凹ˇ=三声；"
                "看不清下凹时写一声，禁止用词典改调。"
                "按 id 顺序返回等长 corrected_syllables（只要音节，不要括号）。\n"
                + json.dumps(payload, ensure_ascii=False)
            ),
        },
        {"type": "image_url", "image_url": {"url": image_url}},
    ]
    for item_id, zoom_url in zooms:
        content.append({"type": "text", "text": f"局部放大 id={item_id}："})
        content.append({"type": "image_url", "image_url": {"url": zoom_url}})

    llm = build_chat_openai(max_tokens=2048, model=llm_vision_model(), timeout=90.0)
    messages = [
        SystemMessage(content=PAGE_TEXT_TONE_VERIFY_SYSTEM),
        HumanMessage(content=content),
    ]
    try:
        structured = llm.with_structured_output(LlmPinyinToneVerify)
        result = structured.invoke(messages)
        return apply_tone_syllable_fixes(regions, list(result.corrected_syllables or []))
    except Exception as exc:
        logger.warning("pinyin tone verify P%s failed, keep audited OCR: %s", page_index, exc)
        return regions


def page_text_llm_enabled() -> bool:
    import os

    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(repo_root / ".env", encoding="utf-8-sig", override=True)
    raw = os.getenv("DUAL_TRACK_TEXTBOOK_TEXT_OCR", "doubao").strip().lower()
    if raw in ("0", "false", "no", "rapidocr", "off"):
        return False
    return llm_enabled()


def dual_track_textbook_uses_doubao() -> bool:
    import os

    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[4]
    load_dotenv(repo_root / ".env", encoding="utf-8-sig", override=True)
    raw = os.getenv("DUAL_TRACK_TEXTBOOK_TEXT_OCR", "doubao").strip().lower()
    return raw not in ("rapidocr", "0", "false", "no", "off")


def _page_cache_path(lesson_uid: str, page_index: int) -> Path:
    safe = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", lesson_uid)
    return _CACHE_DIR / f"{safe}_p{int(page_index):03d}.json"


def load_page_text_cache(lesson_uid: str, page_index: int) -> dict[str, Any] | None:
    path = _page_cache_path(lesson_uid, page_index)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_page_text_cache(
    lesson_uid: str,
    page_index: int,
    payload: dict[str, Any],
    *,
    blob_id: str | None = None,
) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if blob_id:
        payload["blob_id"] = blob_id
    _page_cache_path(lesson_uid, page_index).write_text(
        json.dumps(payload, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def _blob_to_data_url(blob_id: str | None) -> str | None:
    if not blob_id:
        return None
    try:
        from ...models import FileBlob
        from ..blobs import read_blob_bytes

        blob = FileBlob.query.get(str(blob_id).strip())
        if not blob:
            return None
        data = read_blob_bytes(blob)
        mime = (blob.mime_type or "image/png").lower()
        prefix = (
            "data:image/jpeg;base64,"
            if "jpeg" in mime or "jpg" in mime
            else "data:image/png;base64,"
        )
        return prefix + base64.standard_b64encode(data).decode("ascii")
    except Exception as exc:
        logger.debug("page text blob %s unreadable: %s", blob_id, exc)
        return None


def _parse_json_fallback(text: str) -> dict[str, Any]:
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = m.group(1) if m else text
    m2 = re.search(r"\{.*\}", raw, re.DOTALL)
    if m2:
        raw = m2.group(0)
    data = json.loads(raw)
    return LlmPageText.model_validate(data).model_dump()


def _invoke_page_text_llm(image_url: str, *, page_index: int) -> LlmPageText:
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = build_chat_openai(max_tokens=4096, model=llm_vision_model(), timeout=120.0)
    messages = [
        SystemMessage(content=PAGE_TEXT_SYSTEM),
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": (
                        f"请分析【教材 P{int(page_index)}】，输出所有文字区域的 role、text、bbox JSON。"
                        "页角/栏首小栏目名（如「阅读」「习作」）必须出框，禁止漏识。"
                        "夹注拼音：仅当汉字正上方页上确实印有音节才写 字(pīn)；禁止凭知识给没印拼音的字加注音；"
                        "页上有的注音不可漏；多字词只挂有音的字；音在哪个字上方就挂哪个字。"
                        "五类音标（以 e 为例）：轻声 e、一声 ē、二声 é、三声 ě、四声 è；"
                        "a/o/i/u/ü 同理输出预组合字母。只看页上调号形状，禁止用词典改调。"
                        "平横→一声（如 mū/cān/sē），禁止误写成四声 mù/càn/sè；只有清晰 ˇ 才写三声。"
                        "禁止截断漏字：框内汉字须与页图逐字对应；"
                        "夹注拼音只加在有音的字后，禁止漏掉其前/后汉字"
                        "（错误：「很简朴」有音在朴 → 不可写成「很朴(pǔ)」；正确：「很简朴(pǔ)」）。"
                        "页边残片如「帽……」不要单独成框；页上问句都要出框。"
                        "左/右侧栏印刷旁批、问题便签必须各自出框，禁止只出主栏正文；有手写圈画也要抄印刷旁批。"
                        "「识字加油站」下形近字对照表多行必须合为一个 text 框（换行），禁止按行拆框。"
                        "古诗/现代诗：同一首的诗题+作者+各句必须合为一个 text 框（换行），禁止一句一框。"
                        "「注释」栏及其下 ①②③ 多条释义必须合为一个 text 框，禁止一条一框。"
                        "课题下连续学习提示（默读课文… / 选出你认为…）必须合为一个 text 框，勿拆成两框。"
                        "课题/句旁脚注角标必须写带圈 ①②③（禁止普通 2/3 或上标 ²/³；如 真珠③、缘(yuán)③）。"
                        "课节标题左侧带圈课序号必须写 ①②③（禁止写成普通 3），选学星号 * 必须保留（如 ③* 现代诗二首）。"
                        "感叹「啊！」必须写 ā，禁止无调 a 或 á。"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_url}},
            ]
        ),
    ]
    try:
        structured = llm.with_structured_output(LlmPageText)
        result = structured.invoke(messages)
    except Exception as exc:
        logger.warning("LLM structured page text failed, fallback JSON: %s", exc)
        msg = llm.invoke(messages)
        raw = getattr(msg, "content", str(msg))
        data = _parse_json_fallback(raw)
        result = LlmPageText.model_validate(data)
    # 拼音校准只跑一轮（audit）。声调专项 verify 默认关闭，代码与提示词仍保留。
    audited = _invoke_pinyin_audit_llm(
        image_url, page_index=page_index, regions=list(result.regions or [])
    )
    if page_text_tone_verify_enabled():
        audited = _invoke_pinyin_tone_verify_llm(
            image_url, page_index=page_index, regions=audited
        )
    return LlmPageText(regions=audited)

def _role_to_atom_type(role: str) -> str:
    r = (role or "text").strip().lower()
    if r == "title":
        return "title"
    return "text"


_WATERMARK_TEXT_HINTS = (
    "严禁外传",
    "违者必究",
    "传递追责",
    "追责",
    "CHHT",
    "学而思",
    "科学专用",
    "内部资料",
    "仅供参考",
    "翻印必究",
)


def _looks_like_watermark_text(text: str) -> bool:
    """斜向水印碎段 / 纯水印句：不得进入文字原子。"""
    t = (text or "").strip()
    if not t:
        return False
    compact = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    if not compact:
        return False
    hits = [h for h in _WATERMARK_TEXT_HINTS if h in t or h in compact]
    if not hits:
        return False
    # 短串几乎全是水印关键词
    if len(compact) <= 16:
        return True
    stripped = t
    for h in _WATERMARK_TEXT_HINTS:
        stripped = stripped.replace(h, "")
    left = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    return len(left) <= 2


def _looks_like_page_edge_scrap(
    text: str,
    *,
    x_start: float = 0.0,
    y_start: float = 0.0,
    x_end: float = 1.0,
    y_end: float = 1.0,
) -> bool:
    """页顶/页边裁切残片（如单独「帽……」）不应入库。

    注意：页角完整短栏目名（如「阅读」「习作」）不是残片，禁止按位置误删。
    """
    t = (text or "").strip()
    if not t:
        return False
    # 一两个汉字 + 省略号/点点 → 残片
    if re.fullmatch(r"[\u4e00-\u9fff]{1,2}\s*[…·\.]{2,}", t):
        return True
    if re.fullmatch(r"[…·\.]{2,}", t):
        return True
    # 仅当带省略/截断痕迹且贴页边时才丢；无省略号的完整短词一律保留
    hans = re.findall(r"[\u4e00-\u9fff]", t)
    if (
        len(hans) <= 2
        and len(t) <= 10
        and re.search(r"[…·\.]{2,}", t)
        and not re.search(r"[。？！?!]", t)
    ):
        if float(y_start) < 0.08 or float(x_start) > 0.88 or float(x_end) < 0.12:
            return True
    return False


_PINYIN_SYL_TOKEN = r"[A-Za-züÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+"
_CLUSTERED_INLINE_PINYIN_RE = re.compile(
    rf"([\u4e00-\u9fff])\(({_PINYIN_SYL_TOKEN}(?:\s+{_PINYIN_SYL_TOKEN})+)\)([\u4e00-\u9fff]+)"
)


def normalize_inline_pinyin_clusters(text: str) -> str:
    """
    修复豆包偶发的「多音节塞进同一括号」：
    鼎(dǐng fèi)沸 → 鼎(dǐng)沸(fèi)

    仅当括号后紧跟足够数量的「裸汉字」（其后不是又一个括号注音），
    且剩余部分不以汉字开头时才改写，避免误伤「叫(jiào zuò)什么」这类整词注音。
    """
    if not text:
        return text
    if "(" not in text:
        return repair_common_pinyin_tone_ocr(text)

    def _repl(m: re.Match[str]) -> str:
        first, syl_blob, rest = m.group(1), m.group(2), m.group(3)
        syls = syl_blob.split()
        if len(syls) < 2:
            return m.group(0)
        need = len(syls) - 1
        bare: list[str] = []
        i = 0
        while i < len(rest) and len(bare) < need:
            ch = rest[i]
            if not ("\u4e00" <= ch <= "\u9fff"):
                break
            if i + 1 < len(rest) and rest[i + 1] == "(":
                break
            bare.append(ch)
            i += 1
        if len(bare) < need:
            return m.group(0)
        leftover = rest[need:]
        if leftover and "\u4e00" <= leftover[0] <= "\u9fff":
            # 后面还有连续汉字 → 更像整词多音节注音，不拆
            return m.group(0)
        chars = [first] + bare
        return "".join(f"{c}({s})" for c, s in zip(chars, syls)) + leftover

    out = text
    for _ in range(8):
        nxt = _CLUSTERED_INLINE_PINYIN_RE.sub(_repl, out)
        if nxt == out:
            break
        out = nxt
    return repair_common_pinyin_tone_ocr(out)


def normalize_pinyin_tone_marks(text: str) -> str:
    """把拼音声调归一成五类预组合字符（轻声/一声/二声/三声/四声）。

    以 e 为例：e / ē / é / ě / è；并修正常见形近误用字母（如 ĕ→ě）。
    """
    import unicodedata

    if not text:
        return text or ""
    t = unicodedata.normalize("NFC", text)
    # 形近/错误码点 → 标准三声 ˇ 预组合
    lookalikes = str.maketrans(
        {
            "ĕ": "ě",  # U+0115 LATIN SMALL LETTER E WITH BREVE
            "Ĕ": "Ě",
            "ŏ": "ǒ",
            "Ŏ": "Ǒ",
            "ŭ": "ǔ",
            "Ŭ": "Ǔ",
            "ă": "ǎ",
            "Ă": "Ǎ",
            "ĭ": "ǐ",
            "Ĭ": "Ǐ",
        }
    )
    t = t.translate(lookalikes)
    # 数字调号 → 预组合（仅括号内音节，避免误伤正文数字）
    tone_map = {
        "1": {"a": "ā", "o": "ō", "e": "ē", "i": "ī", "u": "ū", "ü": "ǖ", "v": "ǖ"},
        "2": {"a": "á", "o": "ó", "e": "é", "i": "í", "u": "ú", "ü": "ǘ", "v": "ǘ"},
        "3": {"a": "ǎ", "o": "ǒ", "e": "ě", "i": "ǐ", "u": "ǔ", "ü": "ǚ", "v": "ǚ"},
        "4": {"a": "à", "o": "ò", "e": "è", "i": "ì", "u": "ù", "ü": "ǜ", "v": "ǜ"},
    }

    def _digit_tone(m: re.Match[str]) -> str:
        syl, digit = m.group(1), m.group(2)
        table = tone_map.get(digit)
        if not table:
            return m.group(0)
        low = syl.lower()
        # 声调落在最后一个 aoeiuü 上（iou/uei 等按拼音规则简化：优先 a o e，否则末元音）
        target = None
        for ch in ("a", "o", "e"):
            if ch in low:
                target = ch
                break
        if target is None:
            for i in range(len(low) - 1, -1, -1):
                if low[i] in "iuüv":
                    target = low[i]
                    break
        if target is None:
            return m.group(0)
        out_chars: list[str] = []
        replaced = False
        for ch in syl:
            key = "ü" if ch in "üÜvV" else ch.lower()
            if not replaced and key == target:
                mapped = table.get(key, ch)
                out_chars.append(mapped.upper() if ch.isupper() and key != "ü" else mapped)
                replaced = True
            else:
                out_chars.append(ch)
        return "(" + "".join(out_chars) + ")"

    t = re.sub(
        r"\(([a-zA-ZüÜ]+)([1-4])\)",
        _digit_tone,
        t,
    )
    return unicodedata.normalize("NFC", t)


def repair_common_pinyin_tone_ocr(text: str) -> str:
    """纠正常见声调 OCR 误识（不按词典改页上调号）。

    - 归一五类预组合音标
    - 感叹「啊！」的一声横线 ā（常被认成 á / 无调 a）
    """
    import unicodedata

    if not text:
        return text or ""
    t = normalize_pinyin_tone_marks(text)
    # 啊 + 全角括号 → 半角，便于统一匹配
    t = re.sub(
        r"啊（([A-Za-zÁáÀàĀāÄäĂă\u0300-\u036f]+)）",
        r"啊(\1)",
        t,
    )
    t = unicodedata.normalize("NFC", t)
    # 感叹「啊！」：无调 a / 二声 á / 组合附加符 → ā
    bad = r"(?:[AaÁá]|a[\u0300-\u036f]+|A[\u0300-\u036f]+)"
    t = re.sub(rf"啊\({bad}\)(\s*[！!」』”\"'])", r"啊(ā)\1", t)
    t = re.sub(rf"([“\"'『「])啊\({bad}\)([”\"'』」])", r"\1啊(ā)\2", t)
    # 引号内「啊(á)」即使后面暂未落到叹号也纠回（课本感叹常见）
    t = re.sub(rf"([“\"'])啊\({bad}\)(?=[”\"'])", r"\1啊(ā)", t)
    return t


_INLINE_PINYIN_RE = re.compile(
    r"\(([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+)\)"
)
_ORPHAN_PINYIN_RE = re.compile(
    r"^[a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]{1,10}$"
)


def expand_bbox_for_inline_pinyin(
    *,
    text: str,
    x_start: float,
    y_start: float,
    x_end: float,
    y_end: float,
    already_expanded: bool = False,
) -> tuple[float, float, float, float]:
    """正文含夹注拼音时上扩 y_start，避免框贴汉字顶边把注音裁在框外。"""
    if already_expanded or not _INLINE_PINYIN_RE.search(text or ""):
        return x_start, y_start, x_end, y_end
    y0 = float(y_start)
    y1 = float(y_end)
    h = max(y1 - y0, 1e-6)
    # 固定上扩约半行高（正文 OCR 常无换行，不能用 h/lines 估）
    pad = min(0.022, max(0.014, h * 0.08))
    return float(x_start), max(0.0, y0 - pad), float(x_end), float(y1)


def expand_atom_bbox_for_inline_pinyin(atom: dict[str, Any]) -> dict[str, Any]:
    """就地安全上扩；带标记保证幂等。"""
    if atom.get("pinyin_bbox_expanded"):
        return atom
    text = (atom.get("content") or atom.get("ocr_text") or atom.get("text") or "").strip()
    if not _INLINE_PINYIN_RE.search(text):
        return atom
    xs, ys, xe, ye = expand_bbox_for_inline_pinyin(
        text=text,
        x_start=float(atom.get("x_start") or 0),
        y_start=float(atom.get("y_start") or 0),
        x_end=float(atom.get("x_end") or 1),
        y_end=float(atom.get("y_end") or 1),
        already_expanded=False,
    )
    out = dict(atom)
    out["x_start"] = round(xs, 4)
    out["y_start"] = round(ys, 4)
    out["x_end"] = round(xe, 4)
    out["y_end"] = round(ye, 4)
    out["pinyin_bbox_expanded"] = True
    return out



def looks_like_orphan_pinyin_atom(text: str) -> bool:
    """仅含单个音节、无汉字 —— OCR 常把注音拆成远离正文的漂浮小框。"""
    plain = re.sub(r"\s+", "", text or "").strip()
    if not plain or re.search(r"[\u4e00-\u9fff]", plain):
        return False
    return bool(_ORPHAN_PINYIN_RE.fullmatch(plain))


def _pinyin_syl_base(syl: str) -> str:
    from unicodedata import normalize

    t = normalize("NFC", (syl or "").strip().lower())
    return (
        t.replace("ā", "a")
        .replace("á", "a")
        .replace("ǎ", "a")
        .replace("à", "a")
        .replace("ē", "e")
        .replace("é", "e")
        .replace("ě", "e")
        .replace("è", "e")
        .replace("ī", "i")
        .replace("í", "i")
        .replace("ǐ", "i")
        .replace("ì", "i")
        .replace("ō", "o")
        .replace("ó", "o")
        .replace("ǒ", "o")
        .replace("ò", "o")
        .replace("ū", "u")
        .replace("ú", "u")
        .replace("ǔ", "u")
        .replace("ù", "u")
        .replace("ǖ", "ü")
        .replace("ǘ", "ü")
        .replace("ǚ", "ü")
        .replace("ǜ", "ü")
    )


def _atom_xy(atom: dict[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(atom.get("x_start") or 0),
        float(atom.get("y_start") or 0),
        float(atom.get("x_end") or 1),
        float(atom.get("y_end") or 1),
    )


def _x_overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    ax0, _, ax1, _ = _atom_xy(a)
    bx0, _, bx1, _ = _atom_xy(b)
    inter = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    w = max(1e-6, min(ax1 - ax0, bx1 - bx0))
    return inter / w


def _apply_syl_to_han_in_text(text: str, han: str, syl: str, *, replace_existing: bool) -> str | None:
    """在 text 里给汉字挂上/改写音节；成功返回新文本。"""
    if not text or not han or not syl:
        return None
    if f"{han}(" in text:
        if not replace_existing:
            return None
        nb = re.sub(rf"{re.escape(han)}\([^)]*\)", f"{han}({syl})", text, count=1)
        return nb if nb != text else None
    if han not in text:
        return None
    return text.replace(han, f"{han}({syl})", 1)


def _pick_han_under_orphan(body_text: str, body: dict[str, Any], orphan: dict[str, Any]) -> str | None:
    """按孤立拼音框中心，选取正文中水平位置最接近的汉字（均分栏宽近似）。"""
    hans = [(i, ch) for i, ch in enumerate(body_text) if "\u4e00" <= ch <= "\u9fff"]
    if not hans:
        return None
    bx0, _, bx1, _ = _atom_xy(body)
    ox0, _, ox1, _ = _atom_xy(orphan)
    oxc = (ox0 + ox1) / 2.0
    span = max(bx1 - bx0, 1e-6)
    n = len(hans)
    best_ch = None
    best_dist = 1e9
    for k, (_i, ch) in enumerate(hans):
        # 第 k 个汉字的近似中心
        cx = bx0 + span * ((k + 0.5) / n)
        dist = abs(cx - oxc)
        if dist < best_dist:
            best_dist = dist
            best_ch = ch
    return best_ch


def attach_orphan_pinyin_to_hanzi(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把未并入正文的孤立音节按几何位置挂到正下方汉字；调号用孤立框原样，不做字表改写。"""
    if not atoms:
        return atoms
    used_orphan_ids: set[int] = set()
    out = [dict(a) for a in atoms]

    for i, a in enumerate(out):
        if i in used_orphan_ids:
            continue
        t = (a.get("content") or a.get("ocr_text") or a.get("text") or "").strip()
        if not looks_like_orphan_pinyin_atom(t):
            continue
        raw_syl = re.sub(r"\s+", "", t)
        base = _pinyin_syl_base(raw_syl)
        _, ay0, _, ay1 = _atom_xy(a)

        # 几何：挂到正下方正文中、水平对齐的汉字
        best_j = -1
        best_score = -1.0
        for j, b in enumerate(out):
            if j == i or j in used_orphan_ids:
                continue
            bt = (b.get("content") or b.get("ocr_text") or b.get("text") or "").strip()
            if not bt or not any("\u4e00" <= ch <= "\u9fff" for ch in bt):
                continue
            if looks_like_orphan_pinyin_atom(bt):
                continue
            _, by0, _, by1 = _atom_xy(b)
            # 拼音框在正文顶边之上、距离不太远
            if ay1 > by0 + 0.02:
                continue
            if by0 - ay1 > 0.06:
                continue
            ov = _x_overlap_ratio(a, b)
            if ov < 0.08:
                continue
            score = ov - max(0.0, by0 - ay1)
            if score > best_score:
                best_score, best_j = score, j
        if best_j < 0:
            continue
        b = out[best_j]
        bt = b.get("content") or b.get("ocr_text") or b.get("text") or ""
        han = _pick_han_under_orphan(bt, b, a)
        if not han:
            continue
        # 若正文已有该字夹注且音节骨架相同，用孤立框声调覆盖
        m = re.search(rf"{re.escape(han)}\(([^)]*)\)", bt)
        if m and _pinyin_syl_base(m.group(1)) != base:
            # 骨架不同，可能认错字，跳过
            if _pinyin_syl_base(m.group(1))[:1] != base[:1]:
                continue
        nb = _apply_syl_to_han_in_text(bt, han, raw_syl, replace_existing=True)
        if not nb:
            continue
        out[best_j] = dict(b)
        out[best_j]["content"] = nb[:2000]
        out[best_j]["ocr_text"] = nb[:8000]
        used_orphan_ids.add(i)

    return [a for idx, a in enumerate(out) if idx not in used_orphan_ids]


def drop_orphan_pinyin_atoms(atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢掉已并入正文夹注的孤立拼音碎框（页图上也不再单独画框）。"""
    atoms = attach_orphan_pinyin_to_hanzi(list(atoms or []))
    body_blob = ""
    bodies: list[dict[str, Any]] = []
    for a in atoms:
        t = (a.get("content") or a.get("ocr_text") or a.get("text") or "").strip()
        if looks_like_orphan_pinyin_atom(t):
            continue
        body_blob += t
        bodies.append(a)
    body_l = body_blob.lower()
    body_bases = {
        _pinyin_syl_base(m)
        for m in re.findall(
            r"\(([A-Za-züÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ]+)\)",
            body_blob,
        )
    }
    out: list[dict[str, Any]] = []
    for a in atoms:
        t = (a.get("content") or a.get("ocr_text") or a.get("text") or "").strip()
        if looks_like_orphan_pinyin_atom(t):
            syl = re.sub(r"\s+", "", t)
            syl_l = syl.lower()
            base = _pinyin_syl_base(syl)
            if f"({syl_l})" in body_l or f"({syl})" in body_blob or syl_l in body_l:
                continue
            if base in body_bases:
                continue
            # 仍漂在某段正文正上方 → 视为夹注碎框，丢弃（避免页图多画小框）
            _, ay0, _, ay1 = _atom_xy(a)
            drop = False
            for b in bodies:
                _, by0, _, _ = _atom_xy(b)
                if ay1 <= by0 + 0.02 and by0 - ay1 < 0.08 and _x_overlap_ratio(a, b) >= 0.08:
                    drop = True
                    break
            if drop:
                continue
        out.append(a)
    return out


_FORM_PAIR_FRAG_RE = re.compile(
    r"[\u4e00-\u9fff]\s*[—\-－]\s*[\u4e00-\u9fff]"
)


def looks_like_form_pair_fragment(text: str) -> bool:
    """识字加油站对照表行/块：至少两条「字—字」条目。"""
    return len(_FORM_PAIR_FRAG_RE.findall(text or "")) >= 2


def merge_adjacent_form_pair_regions(
    regions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把按行拆开的形近字对照表 region 合成一块（换行保留）。"""
    if len(regions or []) < 2:
        return list(regions or [])
    ordered = sorted(
        regions,
        key=lambda r: (float(r.get("y_start") or 0), float(r.get("x_start") or 0)),
    )
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(ordered):
        cur = ordered[i]
        if not looks_like_form_pair_fragment(str(cur.get("text") or "")):
            out.append(cur)
            i += 1
            continue
        group = [cur]
        j = i + 1
        while j < len(ordered):
            nxt = ordered[j]
            if not looks_like_form_pair_fragment(str(nxt.get("text") or "")):
                break
            prev = group[-1]
            gap = float(nxt.get("y_start") or 0) - float(prev.get("y_end") or 0)
            if gap > 0.04:
                break
            if abs(float(nxt.get("x_start") or 0) - float(group[0].get("x_start") or 0)) > 0.2:
                break
            # 宽度应同属主栏对照表，勿吃进旁侧问句
            if float(nxt.get("x_end") or 1) - float(nxt.get("x_start") or 0) < 0.25:
                break
            group.append(nxt)
            j += 1
        if len(group) == 1:
            out.append(cur)
            i += 1
            continue
        merged = dict(group[0])
        merged["text"] = "\n".join(
            str(g.get("text") or "").strip() for g in group if str(g.get("text") or "").strip()
        )
        merged["x_start"] = min(float(g.get("x_start") or 0) for g in group)
        merged["x_end"] = max(float(g.get("x_end") or 1) for g in group)
        merged["y_start"] = min(float(g.get("y_start") or 0) for g in group)
        merged["y_end"] = max(float(g.get("y_end") or 0) for g in group)
        out.append(merged)
        i = j
    return out


def regions_to_raw_atoms(
    regions: list[LlmTextRegion] | list[dict[str, Any]],
    *,
    page_index: int,
) -> list[dict[str, Any]]:
    """Doubao 区域 → extract_atoms 同款 raw dict。"""
    items: list[dict[str, Any]] = []
    for region in regions:
        if isinstance(region, dict):
            role = str(region.get("role") or "text")
            text = (region.get("text") or "").strip()
            xs = float(region.get("x_start", 0))
            ys = float(region.get("y_start", 0))
            xe = float(region.get("x_end", 1))
            ye = float(region.get("y_end", 1))
        else:
            role = region.role
            text = (region.text or "").strip()
            xs, ys, xe, ye = region.x_start, region.y_start, region.x_end, region.y_end
        if not text:
            continue
        if _looks_like_watermark_text(text):
            continue
        if _looks_like_page_edge_scrap(
            text, x_start=xs, y_start=ys, x_end=xe, y_end=ye
        ):
            continue
        text = normalize_inline_pinyin_clusters(text)
        xs, ys, xe, ye = expand_bbox_for_inline_pinyin(
            text=text, x_start=xs, y_start=ys, x_end=xe, y_end=ye
        )
        items.append(
            {
                "role": role,
                "text": text,
                "x_start": xs,
                "y_start": ys,
                "x_end": xe,
                "y_end": ye,
                "pinyin_bbox_expanded": bool(_INLINE_PINYIN_RE.search(text)),
            }
        )
    items = merge_adjacent_form_pair_regions(items)
    items.sort(key=lambda r: (float(r["y_start"]), float(r["x_start"])))
    out: list[dict[str, Any]] = []
    pi = int(page_index)
    for i, item in enumerate(items, start=1):
        text = item["text"]
        out.append(
            {
                "atom_id": f"A{pi:03d}-{i:03d}",
                "atom_type": _role_to_atom_type(item["role"]),
                "page": pi,
                "x_start": round(float(item["x_start"]), 4),
                "y_start": round(float(item["y_start"]), 4),
                "x_end": round(float(item["x_end"]), 4),
                "y_end": round(float(item["y_end"]), 4),
                "content": text[:2000],
                "ocr_text": text[:8000],
                "parent_block_id": None,
                "bound_cw_pgs": [],
                "is_locked": False,
                "pinyin_bbox_expanded": bool(item.get("pinyin_bbox_expanded")),
            }
        )
    return drop_orphan_pinyin_atoms(out)


def extract_page_text_regions(
    *,
    lesson_uid: str,
    page_index: int,
    blob_id: str | None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """识别单页文字原子；返回 (raw_atoms, meta)。"""
    pi = int(page_index)
    blob_id = str(blob_id).strip() if blob_id else ""

    if not force_refresh:
        cached = load_page_text_cache(lesson_uid, pi)
        if (
            cached
            and cached.get("blob_id") == blob_id
            and cached.get("prompt_version") == PAGE_TEXT_PROMPT_VERSION
            and isinstance(cached.get("regions"), list)
            and cached.get("regions")
        ):
            atoms = regions_to_raw_atoms(cached["regions"], page_index=pi)
            if atoms:
                return atoms, {
                    "source": "cache",
                    "region_count": len(atoms),
                    "engine": "doubao",
                }

    if not page_text_llm_enabled():
        return [], {
            "source": "disabled",
            "warning": "豆包教材文字 OCR 未启用（DUAL_TRACK_TEXTBOOK_TEXT_OCR=rapidocr 或 LLM 密钥缺失）",
            "engine": "doubao",
        }

    image_url = _blob_to_data_url(blob_id)
    if not image_url:
        return [], {
            "source": "missing_blob",
            "warning": "教材页 blob 不可用",
            "engine": "doubao",
        }

    try:
        result = _invoke_page_text_llm(image_url, page_index=pi)
        region_dicts = [r.model_dump() for r in result.regions if (r.text or "").strip()]
        save_page_text_cache(
            lesson_uid,
            pi,
            {
                "regions": region_dicts,
                "region_count": len(region_dicts),
                "prompt_version": PAGE_TEXT_PROMPT_VERSION,
            },
            blob_id=blob_id,
        )
        atoms = regions_to_raw_atoms(result.regions, page_index=pi)
        return atoms, {
            "source": "doubao",
            "region_count": len(atoms),
            "engine": "doubao",
            "model": llm_vision_model(),
        }
    except Exception as exc:
        logger.warning("page text OCR P%s failed: %s", pi, exc)
        return [], {
            "source": "error",
            "warning": str(exc)[:300],
            "engine": "doubao",
        }


def extract_page_text_regions_from_image(
    *,
    cache_key: str,
    page_index: int,
    image_path: Path | None = None,
    image_bytes: bytes | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    从页图 PNG 跑豆包文字 OCR（教材对比等无 lesson blob 场景）。
    与 extract_page_text_regions 共用模型与缓存目录，cache 键用 content_hash。
    """
    pi = int(page_index)
    key = (cache_key or "page").strip() or "page"
    data = image_bytes
    if data is None and image_path is not None:
        try:
            data = Path(image_path).read_bytes()
        except OSError as exc:
            return [], {
                "source": "missing_image",
                "warning": f"页图不可读：{exc}",
                "engine": "doubao",
            }
    if not data:
        return [], {
            "source": "missing_image",
            "warning": "页图为空",
            "engine": "doubao",
        }

    content_hash = hashlib.sha256(data).hexdigest()[:24]
    if not force_refresh:
        cached = load_page_text_cache(key, pi)
        if (
            cached
            and cached.get("blob_id") == content_hash
            and cached.get("prompt_version") == PAGE_TEXT_PROMPT_VERSION
            and isinstance(cached.get("regions"), list)
            and cached.get("regions")
        ):
            atoms = regions_to_raw_atoms(cached["regions"], page_index=pi)
            if atoms:
                return atoms, {
                    "source": "cache",
                    "region_count": len(atoms),
                    "engine": "doubao",
                }

    if not page_text_llm_enabled():
        return [], {
            "source": "disabled",
            "warning": "豆包教材文字 OCR 未启用",
            "engine": "doubao",
        }

    image_url = "data:image/png;base64," + base64.standard_b64encode(data).decode("ascii")
    try:
        result = _invoke_page_text_llm(image_url, page_index=pi)
        region_dicts = [r.model_dump() for r in result.regions if (r.text or "").strip()]
        save_page_text_cache(
            key,
            pi,
            {
                "regions": region_dicts,
                "region_count": len(region_dicts),
                "prompt_version": PAGE_TEXT_PROMPT_VERSION,
            },
            blob_id=content_hash,
        )
        atoms = regions_to_raw_atoms(result.regions, page_index=pi)
        return atoms, {
            "source": "doubao",
            "region_count": len(atoms),
            "engine": "doubao",
            "model": llm_vision_model(),
        }
    except Exception as exc:
        logger.warning("page text OCR from image P%s failed: %s", pi, exc)
        return [], {
            "source": "error",
            "warning": str(exc)[:300],
            "engine": "doubao",
        }
