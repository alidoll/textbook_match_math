const LESSON_UID = window.DUAL_TRACK_LESSON_UID;
const API = window.DUAL_TRACK_API || '/api/new-library';

const COLORS = [
  '#0d9488', '#2563eb', '#7c3aed', '#db2777', '#ea580c',
  '#ca8a04', '#16a34a', '#0891b2', '#4f46e5', '#c026d3',
];

const STEP_HINTS = {
  text_ocr: '前置1：执行新教材文字 OCR；旧教材/旧课件文字只读旧库（全库升级后应已就绪）。',
  image_ocr: '前置2：执行新教材插图 OCR；旧教材/旧课件插图只读旧库缓存。',
  block_match: '前置3：prepare 栏目/cluster → unit_id；新教材建议块名（非 old_mirror N##）。',
  prescan: '前置4：content-prescan 粗匹配 + block_hits 初稿。',
  pair_review: '前置5：主参照课配对与确认对照状态。',
  tb_units: '①-1 prepare 聚类的 unit_id 基准行。',
  tb_sources: '①-2 旧教材参照源（块名+页，不含课件）。',
  tb_scores: '①-3 每 unit 与旧教材源文本打分 + Top3。',
  tb_classify: '①-4 match/new + 三要素 + illustration_tag。',
  textbook: '①-5 一块汇总表；可勾豆包按页视觉分配。',
  cw_sources: '②-1 归档旧课件参照源：核心 vs 附属。',
  cw_scores: '②-2 每 unit 与课件源打分 + Top3。',
  cw_illustration: '②-3 插图冲突标记。',
  cw_status: '②-4 adapt / missing / cw_only。',
  courseware: '②-5 二块总表 + 附属附表。',
  cmp_join: '③-1 unit_id 左关联一块+二块。',
  cmp_mark: '③-2 compare_mark + mark_reason。',
  cmp_summary: '③-3 对照类型统计。',
  compare: '③-4 完整对照总表。',
  fusion: '④ fusion_action + 插图复用。',
  live: '现网 N01…（数据库已建块，用于对比）',
  idle: '结果已清空。从「前置 1」或「1-1」逐步点击，每步只看该步输出。',
};

let workspace = null;
let currentPage = 1;
let centerView = 'new';
let oldPageIndex = 1;
let oldSlideIndex = 1;
let fitHeight = false;
let useLlm = true;
let currentStep = 'idle';
let previewBlocks = [];
let liveBlocks = [];
let focusedBlockCode = null;
let focusedAtomCode = null;
let tableDetail = { yikuai: null, erkuai: null, compare: null, fusion: null };
let prepSlideRows = [];
let prepOldTbDoubao = null;
let busy = false;
let runningStep = null;
let lastStepPayload = null;
const DT_LAST_PREP_KEY = `dt_last_prep_${LESSON_UID}`;

function saveLastPrepPayload(data) {
  try {
    if (data?.detail?.prep) {
      sessionStorage.setItem(DT_LAST_PREP_KEY, JSON.stringify({
        step: data.step,
        mode_label: data.mode_label,
        detail: { prep: data.detail.prep },
      }));
    }
  } catch { /* ignore */ }
}

function loadLastPrepPayload() {
  try {
    const raw = sessionStorage.getItem(DT_LAST_PREP_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

const PREP_STEP_LABELS = {
  text_ocr: '1 文字OCR',
  image_ocr: '2 图片OCR',
  block_match: '3 栏目·聚类',
  prescan: '4 对照预判',
  pair_review: '5 确认对照',
};

const PREP_STEP_KEYS = Object.keys(PREP_STEP_LABELS);
const DT_PREP_STORAGE_KEY = `dt_prep_done_${LESSON_UID}`;

function loadDualTrackPrepDone() {
  try {
    const raw = sessionStorage.getItem(DT_PREP_STORAGE_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function saveDualTrackPrepDone() {
  try {
    sessionStorage.setItem(DT_PREP_STORAGE_KEY, JSON.stringify([...dualTrackPrepDone]));
  } catch { /* ignore */ }
}

/** 本页双轨会话内已成功点过的前置步骤（≠ annotate 库内进度） */
let dualTrackPrepDone = loadDualTrackPrepDone();

function markDualTrackPrepDone(step) {
  if (!PREP_STEP_KEYS.includes(step)) return;
  dualTrackPrepDone.add(step);
  saveDualTrackPrepDone();
}

function libraryPrepStatus() {
  const segs = workspace?.analysis?.segments || {};
  const done = PREP_STEP_KEYS.filter((k) => segs[k] === 'done' || segs[k] === 'skip').length;
  return { done, total: PREP_STEP_KEYS.length };
}

function libraryPrepHint() {
  const lib = libraryPrepStatus();
  if (lib.done <= 0) return '';
  return `annotate 库内约 ${lib.done}/${lib.total} 步有数据（打钩只看本页已点步骤）`;
}

function formatTextOcrRunLine(run, summary) {
  const n = run?.new_text_ocr || {};
  const s = run?.slide_text_ocr || {};
  const parts = [];
  if (s.slides_with_text != null) {
    parts.push(`课件豆包 ${s.slides_with_text}/${s.slides_total ?? '?'} 页有字`);
  }
  const oldTb = run?.old_textbook_doubao_ocr || {};
  if (oldTb.atom_count != null) {
    parts.push(`旧教材豆包 ${oldTb.pages_done ?? '?'}/${oldTb.pages_total ?? '?'} 页 · ${oldTb.atom_count} 原子`);
  }
  if (n.skipped) {
    parts.push(`新教材已 OCR，跳过重复`);
  } else if (n.pages_done != null) {
    parts.push(`新教材 ${n.pages_done}/${n.pages_total ?? '?'} 页已 OCR`);
  }
  if (n.text_atom_count != null) {
    parts.push(`${n.text_atom_count} 个文字原子已入库`);
  }
  if (summary?.new_char_total != null) {
    parts.push(`新教材共 ${summary.new_char_total} 字`);
  }
  if (summary?.low_coverage_pages > 0) {
    parts.push(`${summary.low_coverage_pages} 页覆盖率偏低`);
  }
  return parts.join(' · ') || '已执行';
}

function formatImageOcrRunLine(run) {
  const img = run?.image_ocr || {};
  const oldTb = run?.old_textbook_image_ocr || {};
  const slide = run?.slide_layout_ocr || {};
  const parts = [
    `新 ${img.pages_done ?? '?'}/${img.pages_total ?? '?'} 页 · ${img.image_atom_count ?? '?'} 图`,
    `旧教材 ${oldTb.image_atom_count ?? '?'} 图`,
    `旧课件 ${slide.slides_layout_done ?? '?'}/${slide.slides_total ?? '?'} 页 · ${slide.image_regions ?? 0} 区域`,
  ];
  return parts.join(' · ');
}

function setRunStatus({ state, text }) {
  const el = document.getElementById('dt-run-status');
  if (!el) return;
  if (!text) {
    el.hidden = true;
    el.textContent = '';
    el.className = 'dt-run-status';
    return;
  }
  el.hidden = false;
  el.textContent = text;
  el.className = `dt-run-status is-${state || 'running'}`;
}

function syncPrepButtonBadges() {
  document.querySelectorAll('.dt-step-btn--prep[data-step]').forEach((btn) => {
    const key = btn.dataset.step;
    let st = 'pending';
    if (busy && runningStep === key) {
      st = 'running';
    } else if (dualTrackPrepDone.has(key)) {
      st = 'done';
    }
    btn.dataset.segStatus = st;
    btn.title = st === 'done' ? '本页双轨已点过此步' : '';
    btn.classList.toggle('is-running', busy && runningStep === key);
  });
}

function auditViewLabel() {
  if (centerView === 'courseware') return `课件 P${oldSlideIndex}`;
  if (centerView === 'textbook') return `旧教材 P${oldPageIndex}`;
  return `新教材 P${currentPage}`;
}

function allSlideTextRows() {
  const fromPrep = prepSlideRows.length ? prepSlideRows : null;
  const fromWs = pairedOld()?.old_courseware_text_rows || [];
  const fromCache = lastStepPayload?.detail?.prep?.old_courseware_rows || [];
  return fromPrep || (fromWs.length ? fromWs : fromCache);
}

function slideTextRow(slideIndex) {
  const idx = slideIndex ?? oldSlideIndex;
  return allSlideTextRows().find((r) => r.slide_index === idx) || null;
}

function syncPrepSlideRowsFromWorkspace() {
  const rows = pairedOld()?.old_courseware_text_rows;
  if (Array.isArray(rows) && rows.length) {
    prepSlideRows = rows;
  }
}

function syncPrepSlideRowsFromPayload(data) {
  const rows = data?.detail?.prep?.old_courseware_rows;
  if (Array.isArray(rows) && rows.length) {
    prepSlideRows = rows;
  }
  const doubao = data?.detail?.prep?.old_textbook_doubao;
  if (doubao?.pages?.length) {
    prepOldTbDoubao = doubao;
  }
}

function oldDoubaoPayload() {
  if (prepOldTbDoubao?.pages?.length) return prepOldTbDoubao;
  const ws = pairedOld()?.old_textbook_doubao;
  if (ws?.pages?.length) return ws;
  return lastStepPayload?.detail?.prep?.old_textbook_doubao || null;
}

function syncOldTbDoubaoFromWorkspace() {
  const payload = pairedOld()?.old_textbook_doubao;
  if (payload?.pages?.length) prepOldTbDoubao = payload;
}

function syncOldTbDoubaoFromPayload(data) {
  const doubao = data?.detail?.prep?.old_textbook_doubao;
  if (doubao?.pages?.length) prepOldTbDoubao = doubao;
}

function oldDoubaoAtomsOnPage(pg) {
  const payload = oldDoubaoPayload();
  if (!payload) return [];
  const pageRow = (payload.pages || []).find((p) => p.page_index === pg);
  if (pageRow?.atoms?.length) return pageRow.atoms;
  return (payload.atoms || []).filter((a) => a.page_index === pg);
}

function oldImageAtomsOnPage(pg) {
  return oldAtomsOnPage(pg).filter((a) => (a.atom_type || '').toLowerCase() === 'image');
}

function slideLayoutRow(slideIndex) {
  const idx = slideIndex ?? oldSlideIndex;
  const layout = pairedOld()?.old_slide_layout;
  return (layout?.slides || []).find((s) => s.slide_index === idx) || null;
}

function slideLayoutAtomsOnSlide(slideIndex) {
  const row = slideLayoutRow(slideIndex);
  if (!row?.regions?.length) return [];
  return row.regions.map((r, i) => ({
    atom_code: `CW-P${row.slide_index}-I${i + 1}`,
    page_index: row.slide_index,
    atom_type: 'image',
    bbox: {
      x_start: r.x_start,
      y_start: r.y_start,
      x_end: r.x_end,
      y_end: r.y_end,
    },
    content: r.label || '[插图]',
    ocr_text: r.label || '',
    image_role: r.role || 'illustration',
  }));
}

function atomsForAuditView() {
  if (currentStep === 'text_ocr') {
    if (centerView === 'textbook') {
      const doubao = oldDoubaoAtomsOnPage(oldPageIndex);
      if (doubao.length) return doubao;
      return oldTextAtomsOnPage(oldPageIndex);
    }
    if (centerView === 'new') return atomsOnPageForOverlay(currentPage);
    return [];
  }
  if (currentStep === 'image_ocr') {
    if (centerView === 'new') return atomsOnPageForOverlay(currentPage);
    if (centerView === 'textbook') return oldImageAtomsOnPage(oldPageIndex);
    if (centerView === 'courseware') return slideLayoutAtomsOnSlide(oldSlideIndex);
  }
  return [];
}

function renderAtomAuditTableHtml(atoms, opts = {}) {
  const kind = opts.kind || (currentStep === 'image_ocr' ? 'image' : 'text');
  if (!atoms.length) {
    if (kind === 'image' && centerView === 'courseware') {
      return '<p class="hint">本页暂无插图区域（旧库课件版面 OCR 未跑完或本页无图）</p>';
    }
    if (kind === 'image') {
      return '<p class="hint">本页暂无插图原子（旧库图片 OCR 未跑完或本页无图）</p>';
    }
    return '<p class="hint">本页暂无文字原子</p>';
  }
  let html = '<table class="dt-table dt-audit-table"><thead><tr><th>编号</th><th>OCR 文字</th></tr></thead><tbody>';
  for (const a of atoms) {
    const text = (a.content || a.ocr_text || '').trim() || '（空）';
    const focus = focusedAtomCode === a.atom_code ? ' is-focus' : '';
    html += `<tr class="dt-audit-row${focus}" data-atom="${esc(a.atom_code)}">`;
    html += `<td class="dt-audit-code">${esc(a.atom_code)}</td>`;
    html += `<td class="dt-audit-text">${esc(text)}</td></tr>`;
  }
  html += '</tbody></table>';
  return html;
}

function renderSlideAuditHtml(row) {
  if (!row) {
    return '<p class="hint">本页暂无课件 OCR 文字。请点「1 文字OCR」执行豆包识图（或取消=检视已有缓存）。</p>';
  }
  const text = (row.text || row.excerpt || '').trim();
  const src = row.data_source && row.data_source !== '—' ? row.data_source : '未知';
  const srcLabel = {
    doubao: '豆包视觉 OCR',
    old_block_name: '旧块课件名（兜底）',
    db_ocr_text: '数据库 ocr_text',
    disabled: '未启用（检查 DUAL_TRACK_SLIDE_OCR / LLM 密钥）',
    missing_blob: '课件图缺失',
    error: '识别失败',
  }[src] || src;
  if (!text) {
    return `<p class="hint">课件 P${row.slide_index} 暂无文字（来源：${esc(srcLabel)}）。请重跑「1 文字OCR」。</p>`;
  }
  const lines = text.split(/\r?\n/).filter((ln) => ln.trim());
  let body = '';
  if (lines.length > 1) {
    body = '<ol class="dt-slide-lines">';
    for (const ln of lines) {
      body += `<li>${esc(ln.trim())}</li>`;
    }
    body += '</ol>';
  } else {
    body = `<div class="dt-slide-text-block">${esc(text)}</div>`;
  }
  return `<p class="hint">来源：${esc(srcLabel)} · ${row.char_count ?? text.length} 字</p>${body}`;
}

function bindAuditRowClicks() {
  document.querySelectorAll('#dt-detail .dt-audit-row').forEach((row) => {
    row.onclick = () => focusOcrAtom(row.dataset.atom);
  });
}

function refreshDetailIfOcrStep() {
  if (currentStep !== 'text_ocr' && currentStep !== 'image_ocr') return;
  const cached = lastStepPayload || loadLastPrepPayload();
  renderDetail(cached ? { ...cached, step: currentStep } : { step: currentStep });
}

function focusOcrAtom(atomCode) {
  if (!atomCode) return;
  focusedAtomCode = focusedAtomCode === atomCode ? null : atomCode;
  focusedBlockCode = null;
  if (centerView === 'new') {
    const atom = (workspace?.atoms || []).find((x) => x.atom_code === atomCode);
    if (atom?.page_index) currentPage = atom.page_index;
  } else if (centerView === 'textbook') {
    const atom = oldDoubaoAtomsOnPage(oldPageIndex).find((x) => x.atom_code === atomCode)
      || oldImageAtomsOnPage(oldPageIndex).find((x) => x.atom_code === atomCode)
      || (pairedOld()?.old_atoms || []).find((x) => x.atom_code === atomCode);
    if (atom?.page_index) oldPageIndex = atom.page_index;
  } else if (centerView === 'courseware') {
    const atom = slideLayoutAtomsOnSlide(oldSlideIndex).find((x) => x.atom_code === atomCode);
    if (atom?.page_index) oldSlideIndex = atom.page_index;
  }
  syncCenterTabs();
  renderCenterNav();
  renderCenterView();
  refreshDetailIfOcrStep();
}

function lowCovBadge(flag) {
  return flag ? '<span class="dt-cov-warn">偏低</span>' : '';
}

function segBadge(status) {
  const s = status || 'pending';
  const cls = `dt-seg-badge dt-seg-badge--${s}`;
  const label = { done: '已完成', running: '进行中', pending: '未开始', error: '异常', skip: '跳过' }[s] || s;
  return `<span class="${cls}">${esc(label)}</span>`;
}

function isPrepStep(step) {
  return ['text_ocr', 'image_ocr', 'block_match', 'prescan', 'pair_review'].includes(step);
}

function isMidStep(step) {
  return isPrepStep(step)
    || step.startsWith('tb_')
    || step.startsWith('cw_')
    || step.startsWith('cmp_');
}

function shouldShowAtomOverlay() {
  if (currentStep === 'idle') return false;
  if (currentStep === 'text_ocr') return centerView === 'new';
  if (currentStep === 'image_ocr') return centerView === 'new';
  if (isPrepStep(currentStep)) {
    return currentStep === 'block_match';
  }
  if (currentStep === 'live') return true;
  if (isMatchPreviewMode()) return true;
  if (currentStep === 'textbook' || currentStep === 'courseware' || currentStep === 'fusion') {
    return previewBlocks.length > 0;
  }
  return false;
}

function isMatchPreviewMode() {
  return currentStep !== 'live' && currentStep !== 'idle' && previewBlocks.length > 0;
}

function shouldShowOldAtomOverlay() {
  if (currentStep === 'text_ocr' && centerView === 'textbook') return true;
  if (currentStep === 'image_ocr' && (centerView === 'textbook' || centerView === 'courseware')) {
    return true;
  }
  if (!shouldShowAtomOverlay()) return false;
  if (focusedAtomCode || focusedBlockCode) return true;
  if (currentStep === 'live') return true;
  return isMatchPreviewMode();
}

function linkedOldBlockCode() {
  if (focusedAtomCode) {
    return atomOwner(focusedAtomCode)?.source_old_block || null;
  }
  return sourceOldBlockCode();
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function toast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3200);
}

async function readJson(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`服务器返回异常（HTTP ${r.status}）`);
  }
}

function pairedOld() {
  return workspace?.paired_old || null;
}

function blocksForDisplay() {
  if (currentStep === 'live') return liveBlocks;
  return previewBlocks;
}

function blockColor(code) {
  const codes = blocksForDisplay().map((b) => b.block_code);
  const i = codes.indexOf(code);
  return COLORS[i >= 0 ? i % COLORS.length : 0];
}

function atomOwner(atomCode) {
  for (const b of blocksForDisplay()) {
    if ((b.atom_codes || []).includes(atomCode)) return b;
  }
  return null;
}

function atomsOnPage(pg) {
  return (workspace?.atoms || []).filter((a) => a.page_index === pg);
}

function atomsOnPageForOverlay(pg) {
  const all = atomsOnPage(pg);
  if (currentStep === 'text_ocr') {
    return all.filter((a) => !['image', 'figure'].includes((a.atom_type || 'text').toLowerCase()));
  }
  if (currentStep === 'image_ocr') {
    return all.filter((a) => (a.atom_type || '').toLowerCase() === 'image');
  }
  return all;
}

function oldTextAtomsOnPage(pg) {
  return oldAtomsOnPage(pg).filter((a) => !['image', 'figure'].includes((a.atom_type || 'text').toLowerCase()));
}

function oldAtomsOnPage(pg) {
  return (pairedOld()?.old_atoms || []).filter((a) => a.page_index === pg);
}

function pageCount() {
  return workspace?.textbook_pages?.length || 0;
}

function oldPageCount() {
  return pairedOld()?.old_textbook_pages?.length || 0;
}

function oldSlideCount() {
  return pairedOld()?.old_courseware_slides?.length || 0;
}

function sourceOldBlockCode() {
  if (!focusedBlockCode) return null;
  const b = blocksForDisplay().find((x) => x.block_code === focusedBlockCode);
  return b?.source_old_block || null;
}

function dualTrackPrepStatus() {
  const done = PREP_STEP_KEYS.filter((k) => dualTrackPrepDone.has(k)).length;
  return { done, total: PREP_STEP_KEYS.length, ready: done >= PREP_STEP_KEYS.length };
}

function renderHeader() {
  const les = workspace?.lesson || {};
  const st = workspace?.stats || {};
  const po = pairedOld()?.old_lesson;
  const locked = !!les.blocks_locked;
  const chips = [
    les.display_title || les.volume_code,
    `新教材 ${st.atom_count || 0} 原子`,
    `现网 ${st.block_count || 0} 块`,
    locked ? '已锁定' : '未锁定',
  ];
  if (po) chips.push(`旧课 ${po.lesson_name || ''}`);
  const sub = document.getElementById('page-sub');
  if (sub) sub.textContent = chips.filter(Boolean).join(' · ');
  const prep = dualTrackPrepStatus();
  const prepEl = document.getElementById('dt-prep-status');
  if (prepEl) {
    const libHint = libraryPrepHint();
    const nextKey = PREP_STEP_KEYS.find((k) => !dualTrackPrepDone.has(k));
    const nextLabel = nextKey ? PREP_STEP_LABELS[nextKey] : '';
    prepEl.textContent = prep.ready
      ? `本页双轨前置 ${prep.done}/${prep.total} 步已点完，可跑 ①–④ ${libHint ? '· ' + libHint : ''}`
      : `本页双轨前置 ${prep.done}/${prep.total} — 请继续点「${nextLabel || '1 文字OCR'}」${libHint ? '· ' + libHint : ''}`;
    prepEl.classList.toggle('is-ready', prep.ready);
  }
  syncPrepButtonBadges();
  const annotateUrl = `/new-library/lessons/${encodeURIComponent(LESSON_UID)}/annotate`;
  const back = document.getElementById('link-annotate');
  if (back) {
    back.href = annotateUrl;
    back.textContent = locked ? '← 返回建块（需先解锁）' : '← 返回建块编辑';
  }
  const ro = document.getElementById('dt-readonly-note');
  if (ro) {
    ro.innerHTML = locked
      ? '本页<strong>只读</strong>。中间栏用标签切换新/旧教材/旧课件，整页全高查看。编辑请 <a href="' + esc(annotateUrl) + '">回建块页解锁</a>。'
      : '本页<strong>只读</strong>。中间栏用标签切换新/旧教材/旧课件，整页全高查看。编辑请 <a href="' + esc(annotateUrl) + '">回建块页</a>。';
  }
}

function clearResults() {
  currentStep = 'idle';
  previewBlocks = [];
  tableDetail = { yikuai: null, erkuai: null, compare: null, fusion: null };
  focusedBlockCode = null;
  focusedAtomCode = null;
  centerView = 'new';
  lastStepPayload = null;
  setRunStatus({ state: '', text: '' });
  renderDetail({ step: 'idle' });
  renderAll();
}

function renderRunningDetail(step) {
  const label = PREP_STEP_LABELS[step] || step;
  const ocrNote = (step === 'text_ocr')
    ? '顺序：旧课件 → 旧教材豆包缓存 → 新教材豆包。整课约 3–15 分钟；Flask 同步请求，完成前左侧只显示本提示。'
    : (step === 'image_ocr' ? '正在识别插图区域并写 image 原子…' : '');
  renderDetail({
    step,
    mode_label: `运行中 · ${label}`,
    running: true,
    detail: {
      prep: {
        step_label: `正在执行 ${label}`,
        segment_status: 'running',
      },
    },
  });
  const mode = document.getElementById('dt-mode-line');
  if (mode) mode.textContent = `⏳ 正在运行：${label}…`;
  setRunStatus({ state: 'running', text: `⏳ ${label} 执行中… ${ocrNote}` });
}

function syncBlocksColumnTitle(blockCount) {
  const el = document.getElementById('dt-blocks-title');
  if (!el) return;
  const n = blockCount ?? blocksForDisplay().length;
  const suffix = n ? ` (${n})` : '';
  if (currentStep === 'idle') {
    el.textContent = '区块列表（空）';
  } else if (currentStep === 'live') {
    el.textContent = `现网区块（数据库）${suffix}`;
  } else if (currentStep === 'compare' || currentStep === 'cmp_mark') {
    el.textContent = `预览区块（对照）${suffix}`;
  } else if (currentStep === 'courseware') {
    el.textContent = `预览区块（二块）${suffix}`;
  } else if (currentStep === 'textbook') {
    el.textContent = `预览区块（一块）${suffix}`;
  } else if (isMidStep(currentStep)) {
    el.textContent = `中间步骤（无预览块）${suffix}`;
  } else {
    el.textContent = `预览区块（不写库）${suffix}`;
  }
}

function renderCenterNav() {
  const info = document.getElementById('dt-page-info');
  const prev = document.getElementById('dt-page-prev');
  const next = document.getElementById('dt-page-next');
  const cap = document.getElementById('dt-page-caption');
  if (centerView === 'courseware') {
    const total = oldSlideCount();
    if (info) info.textContent = total ? `课件 第 ${oldSlideIndex} / ${total} 页` : '无课件图';
    if (prev) prev.disabled = oldSlideIndex <= 1;
    if (next) next.disabled = oldSlideIndex >= total;
    const slides = pairedOld()?.old_courseware_slides || [];
    const row = slideTextRow(oldSlideIndex);
    const slide = slides.find((s) => s.slide_index === oldSlideIndex);
    const ocrText = row?.text || slide?.ocr_text || '';
    if (cap) {
      const layoutN = slideLayoutAtomsOnSlide(oldSlideIndex).length;
      if (currentStep === 'image_ocr') {
        cap.textContent = layoutN
          ? `插图区域 ${layoutN} 个（紫框+编号）— 来自旧库课件版面 OCR`
          : '（该页暂无插图区域 — 请确认旧库已跑图片 OCR）';
      } else if (ocrText) {
        const src = row?.data_source && row.data_source !== '—' ? ` [${row.data_source}]` : '';
        cap.textContent = `OCR${src}：${ocrText.slice(0, 160)}${ocrText.length > 160 ? '…' : ''}`;
      } else if (currentStep === 'text_ocr') {
        cap.textContent = '（该页暂无 OCR 文字 — 请点「1 文字OCR」执行豆包识图）';
      } else {
        cap.textContent = '旧课件页 — 前置1 完成后可检视 OCR 文字';
      }
    }
    return;
  }
  if (centerView === 'textbook') {
    const total = oldPageCount();
    if (info) info.textContent = total ? `旧教材 第 ${oldPageIndex} / ${total} 页` : '无旧教材页';
    if (prev) prev.disabled = oldPageIndex <= 1;
    if (next) next.disabled = oldPageIndex >= total;
    const src = linkedOldBlockCode();
    if (cap) {
      if (currentStep === 'text_ocr') {
        const n = oldDoubaoAtomsOnPage(oldPageIndex).length
          || oldTextAtomsOnPage(oldPageIndex).length;
        const src = oldDoubaoAtomsOnPage(oldPageIndex).length ? '豆包蓝框' : '旧库橙框';
        cap.textContent = `旧教材文字原子 ${n} 个（${src}+编号）— 豆包优先对照`;
      } else if (currentStep === 'image_ocr') {
        const n = oldImageAtomsOnPage(oldPageIndex).length;
        cap.textContent = n
          ? `旧教材插图原子 ${n} 个（橙框+编号）— 来自旧库 image 原子`
          : '（本页暂无插图原子 — 请确认旧库已跑图片 OCR）';
      } else {
        cap.textContent = src
          ? `旧块 ${src} 的原子已高亮（橙框）`
          : '旧教材扫描页 — 先点新教材上的原子以联动高亮';
      }
    }
    return;
  }
  const total = pageCount();
  if (info) info.textContent = total ? `新教材 第 ${currentPage} / ${total} 页` : '无新教材页';
  if (prev) prev.disabled = currentPage <= 1;
  if (next) next.disabled = currentPage >= total;
  if (cap) {
    if (focusedAtomCode) {
      const uid = unitIdForOwner(atomOwner(focusedAtomCode));
      cap.textContent = uid
        ? `已选 ${focusedAtomCode} → 单元 ${uid}`
        : `已选 ${focusedAtomCode}（未匹配）`;
    } else if (focusedBlockCode) {
      cap.textContent = `高亮单元 ${focusedBlockCode}`;
    } else if (isMatchPreviewMode()) {
      cap.textContent = '已匹配原子已着色；点击原子跳转旧教材/课件页';
    } else if (currentStep === 'text_ocr') {
      const n = atomsOnPageForOverlay(currentPage).length;
      cap.textContent = `文字原子 ${n} 个（绿框+编号）— 对照页图检查漏字`;
    } else if (currentStep === 'image_ocr') {
      const n = atomsOnPageForOverlay(currentPage).length;
      cap.textContent = `插图原子 ${n} 个（绿框+编号）`;
    } else if (currentStep === 'idle') {
      cap.textContent = '整页浏览（未显示原子框）；运行步骤后可看对照着色';
    } else {
      cap.textContent = '新教材页';
    }
  }
}

function syncFitToggle() {
  const wrap = document.getElementById('dt-viewport-wrap');
  const btn = document.getElementById('dt-fit-toggle');
  if (wrap) wrap.classList.toggle('dt-fit-height', fitHeight);
  if (btn) btn.classList.toggle('is-active', fitHeight);
}

/** 设置页图 src；已缓存时 onload 不触发，须 complete 后立即 callback */
function loadPageImage(img, url, callback) {
  if (!img || !url) return;
  img.onload = () => callback();
  img.src = url;
  if (img.complete && img.naturalWidth > 0) callback();
}

function forceRepaintCenter() {
  renderCenterNav();
  const paint = () => {
    if (centerView === 'new') paintNewAtoms();
    else if (centerView === 'textbook') paintOldAtoms();
    else if (centerView === 'courseware') {
      const img = document.getElementById('dt-page-img');
      const layer = document.getElementById('dt-atom-layer');
      if (img && layer && !img.hidden) {
        layer.style.height = `${img.clientHeight}px`;
        if (currentStep === 'image_ocr') paintCoursewareLayout();
      }
    }
  };
  paint();
  requestAnimationFrame(paint);
}

function finalizeStepUi() {
  forceRepaintCenter();
  if (lastStepPayload?.mode_label) {
    const mode = document.getElementById('dt-mode-line');
    if (mode) mode.textContent = lastStepPayload.mode_label;
  }
  syncPrepButtonBadges();
}

function renderCenterView() {
  const img = document.getElementById('dt-page-img');
  const layer = document.getElementById('dt-atom-layer');
  if (!img || !layer) return;

  if (centerView === 'courseware') {
    const slides = pairedOld()?.old_courseware_slides || [];
    const slide = slides.find((s) => s.slide_index === oldSlideIndex);
    layer.innerHTML = '';
    if (!slide?.url) {
      img.hidden = true;
      layer.innerHTML = '<div class="dt-empty">无课件图片</div>';
      return;
    }
    img.hidden = false;
    loadPageImage(img, slide.url, () => {
      layer.style.height = `${img.clientHeight}px`;
      if (currentStep === 'image_ocr') paintCoursewareLayout();
    });
    return;
  }

  if (centerView === 'textbook') {
    const pages = pairedOld()?.old_textbook_pages || [];
    const pg = pages.find((p) => p.page_index === oldPageIndex);
    if (!pg?.url) {
      img.hidden = true;
      layer.innerHTML = '<div class="dt-empty">无旧教材页图</div>';
      return;
    }
    img.hidden = false;
    loadPageImage(img, pg.url, () => paintOldAtoms());
    return;
  }

  const pages = workspace?.textbook_pages || [];
  const pg = pages.find((p) => p.page_index === currentPage);
  if (!pg?.url) {
    img.hidden = true;
    layer.innerHTML = '<div class="dt-empty">本页无新教材图</div>';
    return;
  }
  img.hidden = false;
  loadPageImage(img, pg.url, () => {
    renderCenterNav();
    paintNewAtoms();
  });
}

function paintNewAtoms() {
  const img = document.getElementById('dt-page-img');
  const layer = document.getElementById('dt-atom-layer');
  if (!img || img.hidden || !layer) return;
  layer.style.height = `${img.clientHeight}px`;
  if (!shouldShowAtomOverlay()) {
    layer.innerHTML = '';
    layer.style.pointerEvents = 'none';
    return;
  }
  const matchMode = isMatchPreviewMode();
  const auditMode = currentStep === 'text_ocr' || currentStep === 'image_ocr';
  layer.style.pointerEvents = (auditMode || matchMode) ? 'auto' : 'none';

  const hasFocus = !!(focusedAtomCode || focusedBlockCode);
  const showLabel = currentStep === 'text_ocr' || currentStep === 'image_ocr';
  layer.innerHTML = atomsOnPageForOverlay(currentPage).map((a) => {
    const owner = atomOwner(a.atom_code);
    const isMatched = !!owner;
    const isSelected = focusedAtomCode === a.atom_code;
    let isFocus = false;
    let isDim = false;

    if (auditMode && focusedAtomCode) {
      isFocus = isSelected;
      isDim = !isSelected;
    } else if (matchMode && !hasFocus) {
      isDim = !isMatched;
    } else if (focusedAtomCode) {
      isFocus = isSelected;
      isDim = !isSelected;
    } else if (focusedBlockCode) {
      isFocus = owner?.block_code === focusedBlockCode;
      isDim = hasFocus && !isFocus;
    }

    return atomBoxHtml(a, {
      owner,
      isFocus,
      isDim,
      isMatched: matchMode ? isMatched : undefined,
      isClickable: auditMode || (matchMode && isMatched),
      showLabel,
    });
  }).join('');

  if (auditMode || matchMode) {
    layer.querySelectorAll('.dt-atom-box.is-clickable').forEach((el) => {
      el.onclick = (e) => {
        e.stopPropagation();
        focusAtom(el.dataset.atom);
      };
    });
  }
}

function paintOldAtoms() {
  const img = document.getElementById('dt-page-img');
  const layer = document.getElementById('dt-atom-layer');
  if (!img || img.hidden || !layer || centerView !== 'textbook') return;
  layer.style.height = `${img.clientHeight}px`;
  if (!shouldShowOldAtomOverlay()) {
    layer.innerHTML = '';
    return;
  }
  const srcOld = linkedOldBlockCode();
  const oldBlock = srcOld
    ? (pairedOld()?.old_blocks || []).find((b) => b.block_code === srcOld)
    : null;
  const focusCodes = new Set(oldBlock?.atom_codes || []);
  const hasFocus = focusCodes.size > 0;
  const atoms = (currentStep === 'text_ocr' && oldDoubaoAtomsOnPage(oldPageIndex).length)
    ? oldDoubaoAtomsOnPage(oldPageIndex)
    : (currentStep === 'text_ocr'
      ? oldTextAtomsOnPage(oldPageIndex)
      : oldImageAtomsOnPage(oldPageIndex));
  const showLabel = currentStep === 'text_ocr' || currentStep === 'image_ocr';
  const auditMode = currentStep === 'text_ocr' || currentStep === 'image_ocr';
  const useDoubaoBox = auditMode && oldDoubaoAtomsOnPage(oldPageIndex).length > 0;
  layer.innerHTML = atoms.map((a) => {
    const isFocus = auditMode ? focusedAtomCode === a.atom_code : focusCodes.has(a.atom_code);
    const isDim = auditMode
      ? (focusedAtomCode && focusedAtomCode !== a.atom_code)
      : (hasFocus && !focusCodes.has(a.atom_code));
    const boxColor = useDoubaoBox
      ? (isFocus ? '#0284c7' : '#7dd3fc')
      : ((auditMode && isFocus) || focusCodes.has(a.atom_code) ? '#ea580c' : '#fdba74');
    return atomBoxHtml(a, {
      focusCodes,
      hasFocus,
      color: boxColor,
      isDim,
      isFocus,
      showLabel,
      isClickable: auditMode,
    });
  }).join('');
  if (auditMode) {
    layer.style.pointerEvents = 'auto';
    layer.querySelectorAll('.dt-atom-box').forEach((el) => {
      el.classList.add('is-clickable');
      el.onclick = (e) => {
        e.stopPropagation();
        focusOcrAtom(el.dataset.atom);
      };
    });
  }
}

function paintCoursewareLayout() {
  const img = document.getElementById('dt-page-img');
  const layer = document.getElementById('dt-atom-layer');
  if (!img || img.hidden || !layer || centerView !== 'courseware') return;
  layer.style.height = `${img.clientHeight}px`;
  if (!shouldShowOldAtomOverlay()) {
    layer.innerHTML = '';
    return;
  }
  const atoms = slideLayoutAtomsOnSlide(oldSlideIndex);
  const auditMode = currentStep === 'image_ocr';
  const showLabel = auditMode;
  layer.innerHTML = atoms.map((a) => {
    const isFocus = focusedAtomCode === a.atom_code;
    const isDim = focusedAtomCode && focusedAtomCode !== a.atom_code;
    const boxColor = isFocus ? '#7c3aed' : '#c4b5fd';
    return atomBoxHtml(a, {
      color: boxColor,
      isFocus,
      isDim,
      showLabel,
      isClickable: auditMode,
    });
  }).join('');
  if (auditMode) {
    layer.style.pointerEvents = 'auto';
    layer.querySelectorAll('.dt-atom-box').forEach((el) => {
      el.classList.add('is-clickable');
      el.onclick = (e) => {
        e.stopPropagation();
        focusOcrAtom(el.dataset.atom);
      };
    });
  }
}

function atomBoxHtml(a, opts) {
  const {
    owner,
    focusCodes,
    hasFocus,
    color,
    isFocus,
    isDim,
    isMatched,
    isClickable,
    showLabel,
  } = opts;
  const b = a.bbox || {};
  const c = color || (owner ? blockColor(owner.block_code) : '#94a3b8');
  let focused = isFocus;
  if (focusCodes && hasFocus) {
    focused = focusCodes.has(a.atom_code);
  }
  const dim = isDim ?? (hasFocus && !focused);
  const cls = [
    'dt-atom-box',
    dim ? 'is-dim' : '',
    focused ? 'is-focus' : '',
    isMatched === false ? 'is-unmatched' : '',
    isMatched ? 'is-matched' : '',
    isClickable ? 'is-clickable' : '',
    focused && isClickable ? 'is-selected' : '',
  ].filter(Boolean).join(' ');
  const top = (b.y_start || 0) * 100;
  const left = (b.x_start || 0) * 100;
  const h = ((b.y_end || 0) - (b.y_start || 0)) * 100;
  const w = ((b.x_end || 0) - (b.x_start || 0)) * 100;
  const title = owner
    ? `${a.atom_code} → ${unitIdForOwner(owner) || owner.block_code}`
    : a.atom_code;
  const label = showLabel
    ? `<span class="dt-atom-label">${esc(a.atom_code)}</span>`
    : '';
  return `<div class="${cls}" data-atom="${esc(a.atom_code)}" title="${esc(title)}"`
    + ` style="top:${top}%;left:${left}%;width:${w}%;height:${h}%;--atom-color:${c}">${label}</div>`;
}

function unitIdForOwner(owner) {
  return owner?.unit_id || owner?.metadata_json?.unit_id || null;
}

function yikuaiRowForUnit(unitId) {
  if (!unitId || !tableDetail.yikuai?.rows) return null;
  return tableDetail.yikuai.rows.find((r) => r.unit_id === unitId) || null;
}

function erkuaiRowForUnit(unitId) {
  if (!unitId || !tableDetail.erkuai?.rows) return null;
  return tableDetail.erkuai.rows.find((r) => r.unit_id === unitId) || null;
}

function jumpToOldSideForBlock(owner) {
  if (!pairedOld() || !owner) return;
  const unitId = unitIdForOwner(owner);
  const tbRow = unitId ? yikuaiRowForUnit(unitId) : null;
  const cwRow = unitId ? erkuaiRowForUnit(unitId) : null;
  if (currentStep === 'courseware' || currentStep === 'fusion') {
    const slides = cwRow?.slide_indices || cwRow?.old_cw_ref?.slide_indices;
    if (slides?.length) {
      centerView = 'courseware';
      oldSlideIndex = slides[0];
      return;
    }
  }
  const pages = tbRow?.old_tb_ref?.pages;
  if (pages?.length) {
    centerView = 'textbook';
    oldPageIndex = pages[0];
    return;
  }
  if (owner.source_old_block) {
    const ob = (pairedOld().old_blocks || []).find((x) => x.block_code === owner.source_old_block);
    if (ob?.textbook_page_start) {
      centerView = 'textbook';
      oldPageIndex = ob.textbook_page_start;
    }
  }
}

function focusAtom(atomCode) {
  if (currentStep === 'text_ocr' || currentStep === 'image_ocr') {
    focusOcrAtom(atomCode);
    return;
  }
  const owner = atomOwner(atomCode);
  if (!owner && isMatchPreviewMode()) {
    toast('该原子未匹配到旧块');
    return;
  }
  if (focusedAtomCode === atomCode) {
    focusedAtomCode = null;
    focusedBlockCode = null;
    centerView = 'new';
  } else {
    focusedAtomCode = atomCode;
    focusedBlockCode = owner?.block_code || null;
    const atom = (workspace?.atoms || []).find((x) => x.atom_code === atomCode);
    if (atom?.page_index) currentPage = atom.page_index;
    jumpToOldSideForBlock(owner);
  }
  syncCenterTabs();
  renderCenterNav();
  renderCenterView();
  renderBlockList();
}

function renderBlockList() {
  const root = document.getElementById('dt-blocks');
  if (!root) return;
  const blocks = blocksForDisplay();
  syncBlocksColumnTitle(blocks.length);
  if (!blocks.length) {
    const msg = currentStep === 'idle'
      ? '结果已清空<br>从顶栏选一步开始；或点「现网块」看数据库'
      : '中间步骤无右侧预览块<br>请点 1-5 / 2-5 / 3-4 / ④';
    root.innerHTML = `<div class="dt-empty">${msg}</div>`;
    return;
  }
  root.innerHTML = blocks.map((b) => {
    const track = b.track || (currentStep === 'live' ? 'live' : 'fusion');
    const cmpMark = b.metadata_json?.compare_mark;
    const focused = focusedBlockCode === b.block_code;
    const atoms = b.atom_codes || [];
    const chips = atoms.slice(0, 8).map((c) => `<span class="dt-atom-chip">${esc(c)}</span>`).join('');
    const more = atoms.length > 8 ? `<span class="dt-atom-chip">+${atoms.length - 8}</span>` : '';
    return `
      <article class="dt-block-card dt-block-card--${esc(track)}${cmpMark ? ` dt-block-card--cmp-${esc(cmpMark)}` : ''}${focused ? ' is-focus' : ''}"
        data-block="${esc(b.block_code)}" tabindex="0">
        <div>
          <span class="dt-block-code">${esc(b.block_code)}</span>
          ${b.badge ? `<span class="dt-block-badge">${esc(b.badge)}</span>` : ''}
        </div>
        <div class="dt-block-name">${esc(b.block_name || '—')}</div>
        <div class="dt-block-meta">${atoms.length} 原子 · ${esc(b.unit_id || b.metadata_json?.unit_id || '—')}</div>
        <div class="dt-atom-chips">${chips}${more}</div>
      </article>`;
  }).join('');
  root.querySelectorAll('[data-block]').forEach((el) => {
    el.onclick = () => focusBlock(el.dataset.block);
  });
}

function focusBlock(code) {
  focusedAtomCode = null;
  focusedBlockCode = focusedBlockCode === code ? null : code;
  const b = blocksForDisplay().find((x) => x.block_code === code);
  if (b?.atom_codes?.length) {
    const pg = (workspace?.atoms || []).find((a) => a.atom_code === b.atom_codes[0])?.page_index;
    if (pg) currentPage = pg;
  }
  if (focusedBlockCode) {
    centerView = 'new';
    jumpToOldSideForBlock(b);
  } else {
    centerView = 'new';
  }
  syncCenterTabs();
  renderCenterNav();
  renderCenterView();
  renderBlockList();
}

function syncCenterTabs() {
  document.querySelectorAll('.dt-center-tab').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.centerView === centerView);
  });
  const po = pairedOld();
  document.querySelectorAll('.dt-center-tab').forEach((btn) => {
    const v = btn.dataset.centerView;
    if (v === 'textbook') btn.disabled = !po?.old_textbook_pages?.length;
    if (v === 'courseware') btn.disabled = !po?.old_courseware_slides?.length;
  });
}

function setCenterView(view) {
  if (view === centerView) return;
  centerView = view;
  syncCenterTabs();
  renderCenterNav();
  renderCenterView();
  refreshDetailIfOcrStep();
  if (view === 'textbook' && linkedOldBlockCode()) {
    toast(`旧块 ${linkedOldBlockCode()} 的原子已高亮`);
  }
}

function renderUnassignedHtml(followup, borderline) {
  if (!followup && !borderline) return '';
  const parts = [];
  if (followup?.count > 0) {
    parts.push(`<p class="dt-stat-line"><strong>未匹配 ${followup.count} 个原子</strong></p>`);
  } else if (followup) {
    parts.push('<p class="hint">本轨无未匹配原子。</p>');
  }
  if (borderline?.count > 0) {
    parts.push(`<p class="dt-stat-line">边界已分配 ${borderline.count} 个（建议复核）</p>`);
  }
  return parts.join('');
}

function renderUnitTableHtml(table, columns) {
  const rows = table?.rows || [];
  if (!rows.length) return '<p class="hint">无数据</p>';
  let html = '<table class="dt-table"><thead><tr>';
  for (const col of columns) html += `<th>${esc(col.label)}</th>`;
  html += '</tr></thead><tbody>';
  for (const row of rows) {
    html += '<tr>';
    for (const col of columns) {
      const v = row[col.key];
      html += `<td>${esc(col.fmt ? col.fmt(v, row) : String(v ?? '—'))}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  return html;
}

function renderDetail(data) {
  const root = document.getElementById('dt-detail');
  if (!root) return;
  const step = data?.step || currentStep;
  const parts = [];
  parts.push(`<p class="dt-step-hint">${esc(STEP_HINTS[step] || '')}</p>`);
  parts.push('<p class="hint">主键 <strong>unit_id</strong>（如 P1-C01）= 新教材栏目单元；一块/二块全课各一张总表。</p>');

  if (data?.mode_label) parts.push(`<p class="dt-stat-line"><strong>${esc(data.mode_label)}</strong></p>`);

  const detail = data?.detail || {};
  if (detail.yikuai) tableDetail.yikuai = detail.yikuai;
  if (detail.erkuai) tableDetail.erkuai = detail.erkuai;
  if (detail.compare) tableDetail.compare = detail.compare;
  if (detail.fusion) tableDetail.fusion = detail.fusion;

  if (step === 'text_ocr' || step === 'image_ocr') {
    const p = detail.prep;
    const isRunningThisStep = !!(data?.running || (busy && runningStep === step));
    if (isRunningThisStep) {
      parts.push('<div class="dt-result-banner dt-running-banner"><strong>⏳ 正在执行 OCR…</strong> 请勿关闭页面；完成后自动刷新左侧对照与中间框线。</div>');
      parts.push('<p class="hint">执行中不显示上次结果。dev.ps1 终端会在<strong>整步完成后</strong>才打印 POST 日志，期间无新日志属正常。</p>');
      root.innerHTML = parts.join('\n');
      return;
    }
    const doneInSession = dualTrackPrepDone.has(step);
    if (centerView === 'courseware' && step === 'text_ocr') {
      const slideRow = slideTextRow(oldSlideIndex);
      if (doneInSession || p?.run?.executed || p?.inspect_only || slideRow?.has_text) {
        const line = p?.run?.executed
          ? formatTextOcrRunLine(p.run, p.summary)
          : '左侧为课件 OCR 文字，中间看图对照';
        parts.push(`<div class="dt-result-banner"><strong>✓ 旧课件文字 ${p?.run?.executed ? '已识别' : '检视'}</strong> · ${esc(line)}</div>`);
      }
      parts.push(`<p class="dt-stat-line"><strong>当前页课件 OCR · ${esc(auditViewLabel())}</strong></p>`);
      parts.push('<p class="hint">豆包识图结果写在左侧；无缓存时用旧块课件名兜底。执行「1 文字OCR」会写双轨缓存。</p>');
      parts.push(renderSlideAuditHtml(slideRow));
    } else if (centerView === 'courseware' && step === 'image_ocr') {
      const auditAtoms = atomsForAuditView();
      if (doneInSession || p?.run?.executed || p?.inspect_only || auditAtoms.length) {
        const line = p?.run?.executed
          ? formatImageOcrRunLine(p.run)
          : '左侧为旧库课件插图区域，中间看紫框编号';
        parts.push(`<div class="dt-result-banner"><strong>✓ 旧课件插图 ${p?.run?.executed ? '已检视' : '检视'}</strong> · ${esc(line)}</div>`);
      }
      parts.push(`<p class="dt-stat-line"><strong>当前页插图区域 · ${esc(auditViewLabel())}</strong> <span class="hint">${auditAtoms.length} 个</span></p>`);
      parts.push('<p class="hint">数据来自旧库 <code>slide_layout</code> 缓存（旧库 intake 图片 OCR 写入）；中间<strong>紫框</strong>为豆包识别的插图区域。</p>');
      parts.push(renderAtomAuditTableHtml(auditAtoms, { kind: 'image' }));
    } else if (centerView === 'textbook' && step === 'image_ocr') {
      const auditAtoms = atomsForAuditView();
      if (doneInSession || p?.run?.executed || p?.inspect_only || auditAtoms.length) {
        const line = p?.run?.executed
          ? formatImageOcrRunLine(p.run)
          : '左侧为旧库 image 原子，中间看橙框编号';
        parts.push(`<div class="dt-result-banner"><strong>✓ 旧教材插图 ${p?.run?.executed ? '已检视' : '检视'}</strong> · ${esc(line)}</div>`);
      }
      parts.push(`<p class="dt-stat-line"><strong>当前页插图原子 · ${esc(auditViewLabel())}</strong> <span class="hint">${auditAtoms.length} 个</span></p>`);
      parts.push('<p class="hint">数据来自旧库 <code>textbook_atoms</code>（atom_type=image）；双轨只读，不在此重跑 OCR。</p>');
      parts.push(renderAtomAuditTableHtml(auditAtoms, { kind: 'image' }));
    } else if (centerView === 'textbook' && step === 'text_ocr') {
      const auditAtoms = atomsForAuditView();
      if (doneInSession || p?.run?.executed || p?.inspect_only || auditAtoms.length) {
        const line = p?.run?.executed
          ? formatTextOcrRunLine(p.run, p.summary)
          : '左侧豆包对照；旧库 atom 未改';
        parts.push(`<div class="dt-result-banner"><strong>✓ 旧教材豆包 ${p?.run?.executed ? '已识别' : '检视'}</strong> · ${esc(line)}</div>`);
      }
      parts.push(`<p class="dt-stat-line"><strong>当前页旧教材 · ${esc(auditViewLabel())}</strong> <span class="hint">${auditAtoms.length} 个豆包原子</span></p>`);
      parts.push('<p class="hint">中间<strong>蓝框</strong>为豆包分区（D 编号）；旧库橙框仅在没有豆包缓存时显示。①教材轨匹配优先用豆包正文。</p>');
      parts.push(renderAtomAuditTableHtml(auditAtoms, { kind: 'text' }));
    } else {
      const auditAtoms = atomsForAuditView();
      if (doneInSession || p?.run?.executed || p?.inspect_only) {
        const line = p?.run?.executed
          ? (step === 'text_ocr' ? formatTextOcrRunLine(p.run, p.summary) : formatImageOcrRunLine(p.run))
          : '库内已有结果，左侧逐条对照';
        parts.push(`<div class="dt-result-banner"><strong>✓ ${step === 'text_ocr' ? '文字' : '图片'} OCR ${p?.inspect_only ? '检视' : '已完成'}</strong> · ${esc(line)}</div>`);
      }
      const auditTitle = step === 'image_ocr' ? '当前页插图原子' : '当前页文字原子';
      parts.push(`<p class="dt-stat-line"><strong>${auditTitle} · ${esc(auditViewLabel())}</strong> <span class="hint">${auditAtoms.length} 个</span></p>`);
      parts.push('<p class="hint">左侧读 OCR 文字、中间看绿框编号；点行或框可联动高亮。</p>');
      parts.push(renderAtomAuditTableHtml(auditAtoms, { kind: step === 'image_ocr' ? 'image' : 'text' }));
    }
  }

  if (detail.prep) {
    const p = detail.prep;
    if (p.run?.executed && step === 'text_ocr' && !dualTrackPrepDone.has(step)) {
      const line = formatTextOcrRunLine(p.run, p.summary);
      parts.push(`<div class="dt-result-banner"><strong>✓ 文字 OCR 已执行完成</strong>${esc(line)}</div>`);
    }
    if (p.run?.executed && step === 'image_ocr' && !dualTrackPrepDone.has(step)) {
      parts.push(`<div class="dt-result-banner"><strong>✓ 图片 OCR 已执行完成</strong>${esc(formatImageOcrRunLine(p.run))}</div>`);
    }
    if (step !== 'text_ocr' && step !== 'image_ocr') {
      parts.push(`<p class="dt-stat-line"><strong>${esc(p.step_label || step)}</strong>${segBadge(dualTrackPrepDone.has(step) ? 'done' : 'pending')} <span class="hint">本页</span></p>`);
    } else {
      parts.push(`<p class="dt-stat-line"><strong>全课汇总</strong>${segBadge(dualTrackPrepDone.has(step) ? 'done' : 'pending')}</p>`);
    }
    if (p.segment_status && isPrepStep(step)) {
      parts.push(`<p class="hint">annotate 库内参考：${esc(p.segment_status)}（与上方「本页」打钩无关）</p>`);
    }
    if (p.summary && step === 'text_ocr') {
      const s = p.summary;
      parts.push(`<p class="hint">新教材 ${s.new_pages ?? '—'} 页 · ${s.new_char_total ?? 0} 字 · 旧库 ${s.old_char_total ?? 0} 字 · 旧教材豆包 ${s.old_doubao_atoms ?? 0} 原子/${s.old_doubao_pages_with_text ?? 0} 页 · 课件 ${s.slides_with_text ?? 0}/${s.old_slides ?? 0} 页</p>`);
    } else if (p.summary && step === 'image_ocr') {
      const s = p.summary;
      parts.push(`<p class="hint">新教材插图 ${s.image_atom_count ?? 0} 个 · 旧教材 image ${s.old_image_atom_count ?? 0} 个 · 旧课件区域 ${s.old_slide_image_regions ?? 0} 个（${s.old_slides_layout_done ?? 0} 页已扫）</p>`);
    } else if (p.summary) {
      const sum = p.summary;
      const lines = Object.entries(sum).map(([k, v]) => `${k}: ${v}`);
      parts.push(`<p class="hint">${esc(lines.join(' · '))}</p>`);
    }
    if (p.run?.warnings?.length) {
      parts.push(`<p class="dt-cov-warn-line">执行提示：${esc(p.run.warnings.slice(0, 5).join('；'))}</p>`);
    }
    if ((p.low_coverage_warnings || []).length) {
      parts.push(`<p class="dt-cov-warn-line">覆盖率告警：${esc(p.low_coverage_warnings.slice(0, 6).join('；'))}</p>`);
    }
    if (step === 'text_ocr') {
      parts.push('<p class="hint">新教材（textbook_atoms + 框）</p>');
      parts.push(renderUnitTableHtml({ rows: p.rows || [] }, [
        { key: 'page_index', label: '页' },
        { key: 'text_atom_count', label: '框数' },
        { key: 'char_count', label: '字数' },
        { key: 'low_coverage', label: '覆盖', fmt: (v) => lowCovBadge(v) || 'OK' },
        { key: 'atom_codes_preview', label: '编号', fmt: (v) => (Array.isArray(v) ? v.join(', ') : '—') },
        { key: 'excerpt', label: '摘要' },
      ]));
      if ((p.old_textbook_doubao_rows || []).length) {
        parts.push('<p class="hint">旧教材·豆包（双轨缓存，不改旧库）</p>');
        parts.push(renderUnitTableHtml({ rows: p.old_textbook_doubao_rows }, [
          { key: 'page_index', label: '页' },
          { key: 'text_atom_count', label: '框数' },
          { key: 'char_count', label: '字数' },
          { key: 'data_source', label: '来源' },
          { key: 'excerpt', label: '摘要' },
        ]));
      }
      if ((p.old_textbook_rows || []).length) {
        parts.push('<p class="hint">旧教材·旧库 atom（只读参考）</p>');
        parts.push(renderUnitTableHtml({ rows: p.old_textbook_rows }, [
          { key: 'page_index', label: '页' },
          { key: 'text_atom_count', label: '框数' },
          { key: 'char_count', label: '字数' },
          { key: 'low_coverage', label: '覆盖', fmt: (v) => lowCovBadge(v) || 'OK' },
          { key: 'excerpt', label: '摘要' },
        ]));
      }
      if ((p.old_courseware_rows || []).length) {
        parts.push('<p class="hint">旧课件（豆包 OCR / 块名兜底）</p>');
        parts.push(renderUnitTableHtml({ rows: p.old_courseware_rows }, [
          { key: 'slide_index', label: '页' },
          { key: 'data_source', label: '来源' },
          { key: 'char_count', label: '字数' },
          { key: 'low_coverage', label: '覆盖', fmt: (v) => lowCovBadge(v) || 'OK' },
          { key: 'excerpt', label: '摘要' },
        ]));
      }
    } else if (step === 'image_ocr') {
      if ((p.page_counts || []).length) {
        parts.push('<p class="hint">新教材 · 每页插图原子</p>');
        parts.push(renderUnitTableHtml({ rows: p.page_counts }, [
          { key: 'page_index', label: '页' },
          { key: 'image_count', label: '插图框' },
          { key: 'low_coverage', label: '覆盖', fmt: (v) => lowCovBadge(v) || 'OK' },
        ]));
      }
      if ((p.old_page_counts || []).length) {
        parts.push('<p class="hint">旧教材 · 每页 image 原子（旧库只读）</p>');
        parts.push(renderUnitTableHtml({ rows: p.old_page_counts }, [
          { key: 'page_index', label: '页' },
          { key: 'image_count', label: '插图框' },
          { key: 'low_coverage', label: '覆盖', fmt: (v) => lowCovBadge(v) || 'OK' },
        ]));
      }
      if ((p.old_courseware_layout_rows || []).length) {
        parts.push('<p class="hint">旧课件 · 每页插图区域（slide_layout 缓存）</p>');
        parts.push(renderUnitTableHtml({ rows: p.old_courseware_layout_rows }, [
          { key: 'slide_index', label: '页' },
          { key: 'region_count', label: '区域数' },
          { key: 'layout_scanned', label: '已扫', fmt: (v) => (v ? '是' : '否') },
          { key: 'low_coverage', label: '覆盖', fmt: (v) => lowCovBadge(v) || 'OK' },
        ]));
      }
      parts.push('<p class="hint">新教材插图样本</p>');
      parts.push(renderUnitTableHtml({ rows: p.rows || [] }, [
        { key: 'atom_code', label: 'atom' },
        { key: 'page_index', label: '页' },
        { key: 'image_role', label: '角色' },
        { key: 'has_bbox', label: '有框', fmt: (v) => (v ? '是' : '否') },
        { key: 'ocr_text', label: 'OCR' },
      ]));
    } else if (step === 'block_match') {
      if (p.note) parts.push(`<p class="hint">${esc(p.note)}</p>`);
      parts.push(renderUnitTableHtml({ rows: p.unit_rows || [] }, [
        { key: 'unit_id', label: 'unit' },
        { key: 'section_name', label: '栏目' },
        { key: 'suggested_name', label: '建议块名' },
        { key: 'name_excerpt', label: '新教材摘录' },
        { key: 'atom_count', label: '原子' },
      ]));
    } else if (step === 'prescan') {
      parts.push(renderUnitTableHtml({ rows: p.rows || [] }, [
        { key: 'old_block_code', label: '旧块' },
        { key: 'new_page_index', label: '新页' },
        { key: 'match_score', label: '分' },
        { key: 'excerpt', label: '摘要' },
      ]));
    } else if (step === 'pair_review') {
      const s = p.summary || {};
      parts.push(`<p class="hint">状态 ${esc(s.pair_review_status || '—')} · 旧课 ${esc(s.old_lesson_name || '')}</p>`);
    }
  }

  if (step === 'tb_units' && detail.tb_units) {
    parts.push(renderUnitTableHtml(detail.tb_units, [
      { key: 'unit_id', label: 'unit' }, { key: 'section_name', label: '栏目' },
      { key: 'page_index', label: '页' }, { key: 'atom_count', label: '原子' },
    ]));
  }
  if (step === 'tb_sources' && detail.tb_sources) {
    parts.push(renderUnitTableHtml(detail.tb_sources, [
      { key: 'label', label: '旧教材' }, { key: 'pages', label: '页', fmt: (v) => (v || []).join(',') },
      { key: 'query_excerpt', label: '摘要' },
    ]));
  }
  if (step === 'tb_scores' && detail.tb_scores) {
    parts.push(renderUnitTableHtml(detail.tb_scores, [
      { key: 'unit_id', label: 'unit' }, { key: 'best_label', label: '最佳' },
      { key: 'best_score', label: '分' }, { key: 'above_threshold', label: '过线', fmt: (v) => (v ? '是' : '否') },
    ]));
  }
  if (step === 'tb_classify' && detail.tb_classify) {
    parts.push(renderUnitTableHtml(detail.tb_classify, [
      { key: 'unit_id', label: 'unit' }, { key: 'tb_match_class', label: '分类' },
      { key: 'illustration_tag', label: '插图' }, { key: 'match_score', label: '分' },
    ]));
  }

  if (step === 'textbook' && detail.yikuai) {
    parts.push(`<p class="dt-stat-line">一块 ${detail.yikuai.row_count} 行 · ${esc(detail.yikuai.ranker === 'doubao' ? '豆包方案' : '规则基线')}</p>`);
    if (detail.yikuai_diff?.changed_units > 0) {
      const d = detail.yikuai_diff;
      parts.push(`<p class="dt-stat-line"><strong>规则→豆包</strong> ${d.changed_units}/${d.total_units} 个单元分类变化</p>`);
      parts.push('<table class="dt-table"><thead><tr><th>unit</th><th>规则</th><th>豆包</th></tr></thead><tbody>');
      for (const s of d.samples || []) {
        parts.push(`<tr><td>${esc(s.unit_id)}</td><td>${esc(s.rules_class)}</td><td>${esc(s.doubao_class)}</td></tr>`);
      }
      parts.push('</tbody></table>');
    } else if (detail.yikuai.ranker === 'doubao') {
      parts.push('<p class="hint">豆包与规则分类一致，或请勾选豆包后重跑 ①。</p>');
    }
    parts.push(renderUnitTableHtml(detail.yikuai, [
      { key: 'unit_id', label: 'unit_id' },
      { key: 'section_name', label: '栏目' },
      { key: 'tb_match_class', label: '教材分类' },
      { key: 'illustration_tag', label: '插图' },
      { key: 'atom_count', label: '原子' },
    ]));
    const ranker = data?.ranker || detail.yikuai?.ranker;
    if (ranker === 'doubao') {
      parts.push('<p class="hint">一块数据来自豆包按页分配回写 unit 行。</p>');
    }
  }

  if (step === 'courseware' && detail.erkuai) {
    parts.push(`<p class="dt-stat-line">二块 ${detail.erkuai.row_count} 行</p>`);
    parts.push(renderUnitTableHtml(detail.erkuai, [
      { key: 'unit_id', label: 'unit_id' },
      { key: 'section_name', label: '栏目' },
      { key: 'cw_adapt_status', label: '课件状态' },
      { key: 'slide_indices', label: '课件页', fmt: (v) => (v || []).join(',') || '—' },
      { key: 'atom_count', label: '原子' },
    ]));
    if ((detail.ancillary_cw || []).length) {
      parts.push(`<p class="hint">附属课件 ${detail.ancillary_cw.length} 项（独立附表，不混入核心行）</p>`);
    }
  }

  if (step === 'cw_sources' && detail.cw_sources) {
    const t = detail.cw_sources;
    parts.push(`<p class="dt-stat-line">核心 ${t.core_count} · 附属 ${t.ancillary_count}</p>`);
    parts.push(renderUnitTableHtml(t, [
      { key: 'label', label: '参照' },
      { key: 'kind', label: '类型' },
      { key: 'slide_indices', label: '页', fmt: (v) => (v || []).join(',') || '—' },
      { key: 'query_excerpt', label: 'OCR摘要' },
    ]));
  }

  if (step === 'cw_scores' && detail.cw_scores) {
    const t = detail.cw_scores;
    parts.push(`<p class="dt-stat-line">${t.row_count} 单元 · 阈值 ${t.min_score} · 参照源 ${t.core_source_count}</p>`);
    parts.push(renderUnitTableHtml(t, [
      { key: 'unit_id', label: 'unit' },
      { key: 'best_label', label: '最佳课件' },
      { key: 'best_score', label: '分' },
      { key: 'above_threshold', label: '过线', fmt: (v) => (v ? '是' : '否') },
      { key: 'slide_indices', label: '页', fmt: (v) => (v || []).join(',') || '—' },
    ]));
    parts.push('<p class="hint">展开 Top3 请看 API 原始 JSON 或下一步 2-3。</p>');
  }

  if (step === 'cw_illustration' && detail.cw_illustration) {
    const t = detail.cw_illustration;
    parts.push(`<p class="dt-stat-line">冲突单元 ${t.conflict_count} / ${t.row_count}</p>`);
    parts.push(renderUnitTableHtml(t, [
      { key: 'unit_id', label: 'unit' },
      { key: 'illustration_tag', label: '插图标签' },
      { key: 'illustration_conflict', label: '冲突', fmt: (v) => (v ? '是' : '否') },
      { key: 'image_atom_count', label: '图atom' },
      { key: 'match_score', label: '课件分' },
    ]));
  }

  if (step === 'cw_status' && detail.cw_status) {
    const t = detail.cw_status;
    parts.push(`<p class="dt-stat-line">${esc(JSON.stringify(t.summary || {}))}</p>`);
    parts.push(renderUnitTableHtml(t, [
      { key: 'unit_id', label: 'unit' },
      { key: 'cw_adapt_status', label: '状态' },
      { key: 'match_score', label: '分' },
      { key: 'best_cw_label', label: '最佳课件' },
    ]));
  }

  if ((step === 'cmp_join' || step === 'cmp_mark' || step === 'cmp_summary' || step === 'compare') && detail.yikuai_source_note) {
    parts.push(`<p class="hint">${esc(detail.yikuai_source_note)}</p>`);
  }

  if (step === 'cmp_join' && detail.cmp_join) {
    parts.push(renderUnitTableHtml(detail.cmp_join, [
      { key: 'unit_id', label: 'unit' },
      { key: 'tb_match_class', label: '教材' },
      { key: 'cw_adapt_status', label: '课件' },
      { key: 'tb_old_label', label: '旧教材' },
      { key: 'cw_old_label', label: '旧课件' },
    ]));
  }

  if (step === 'cmp_mark' && detail.cmp_mark) {
    const cmp = detail.cmp_mark;
    parts.push(`<p class="dt-stat-line">${esc(JSON.stringify(cmp.summary || {}))}</p>`);
    parts.push(renderUnitTableHtml(cmp, [
      { key: 'unit_id', label: 'unit' },
      { key: 'compare_mark', label: '对照' },
      { key: 'mark_reason', label: '依据' },
      { key: 'tb_match_class', label: '教材' },
      { key: 'cw_adapt_status', label: '课件' },
    ]));
  }

  if (step === 'cmp_summary' && detail.cmp_summary) {
    parts.push(renderUnitTableHtml(detail.cmp_summary, [
      { key: 'compare_mark', label: '类型' },
      { key: 'count', label: '数量' },
    ]));
  }

  if (step === 'compare' && detail.compare) {
    const cmp = detail.compare;
    parts.push(`<p class="dt-stat-line">对照 ${cmp.row_count} 行 · ${esc(JSON.stringify(cmp.summary || {}))}</p>`);
    parts.push('<p class="hint">右侧列表可点单元；中间点新原子看归属。</p>');
    parts.push(renderUnitTableHtml(cmp, [
      { key: 'unit_id', label: 'unit_id' },
      { key: 'compare_mark', label: '对照' },
      { key: 'tb_match_class', label: '教材' },
      { key: 'cw_adapt_status', label: '课件' },
      { key: 'illustration_tag', label: '插图' },
    ]));
  }

  if (step === 'fusion' && detail.fusion) {
    const fus = detail.fusion;
    parts.push(`<p class="dt-stat-line">融合 ${fus.row_count} 行</p>`);
    parts.push(renderUnitTableHtml(fus, [
      { key: 'unit_id', label: 'unit_id' },
      { key: 'fusion_action', label: '动作' },
      { key: 'compare_mark', label: '对照' },
      { key: 'atom_count', label: '原子' },
    ]));
    if (detail.diff_vs_current) {
      parts.push(`<p class="hint">现网 ${detail.diff_vs_current.current_block_count} 块 vs 融合 ${fus.row_count} 单元</p>`);
    }
  }

  if (step === 'live') {
    parts.push('<p class="hint">现网块来自数据库，结构仍为 N01…；跑 ①–④ 可预览 unit_id 总表。</p>');
  }

  if (step === 'idle') {
    parts.push('<p class="hint">建议顺序：前置 1→5 → ① 1-1→1-5 → ② → ③ → ④</p>');
    parts.push('<p class="hint">中间页仍可浏览新/旧教材与课件；区块着色需先跑对应预览步。</p>');
  }

  root.innerHTML = parts.join('') || '<div class="dt-empty">选择上方步骤</div>';
  bindAuditRowClicks();
  const mode = document.getElementById('dt-mode-line');
  if (mode) {
    if (data?.mode_label) {
      mode.textContent = data.mode_label;
    } else if (step === 'idle') {
      mode.textContent = '双轨逐步实验 · 点击上方步骤运行';
    } else if (step === 'live') {
      mode.textContent = '现网块';
    } else {
      mode.textContent = PREP_STEP_LABELS[step] || 'unit_id 总表预览';
    }
  }
}

function syncStepButtons() {
  document.querySelectorAll('.dt-step-btn[data-step]').forEach((btn) => {
    btn.classList.toggle('is-active', btn.dataset.step === currentStep);
    btn.disabled = busy;
  });
  syncPrepButtonBadges();
  const clearBtn = document.getElementById('dt-btn-clear');
  if (clearBtn) clearBtn.disabled = busy;
}

function renderAll() {
  renderHeader();
  syncCenterTabs();
  syncFitToggle();
  renderCenterNav();
  renderCenterView();
  renderBlockList();
  syncStepButtons();
}

async function runStep(step) {
  if (busy) {
    toast('上一步还在运行，请稍候…');
    return;
  }
  if (!step) return;
  const stepLabel = PREP_STEP_LABELS[step] || STEP_HINTS[step]?.split('：')[0] || step;
  const body = { step };
  let execute = true;
  if ((step === 'text_ocr' || step === 'image_ocr') && dualTrackPrepDone.has(step)) {
    execute = window.confirm(
      `本页会话内已跑过「${PREP_STEP_LABELS[step]}」。\n\n【确定】= 重新执行豆包 OCR（旧课件 + 旧教材缓存 + 新教材，约 3–15 分钟）\n【取消】= 不执行，只加载当前库内/缓存结果对照`,
    );
  }
  body.execute = execute;
  if (execute && step === 'text_ocr') {
    body.force_textbook = true;
    body.force_slides = true;
  }

  currentStep = step;
  focusedBlockCode = null;
  focusedAtomCode = null;
  centerView = 'new';
  syncCenterTabs();
  if (step === 'live') {
    previewBlocks = [];
    tableDetail = { yikuai: null, erkuai: null, compare: null, fusion: null };
    renderDetail({ step: 'live' });
    renderAll();
    toast('已切换现网块');
    return;
  }
  toast(execute ? `运行中：${stepLabel}…` : `检视：${stepLabel}…`);
  busy = true;
  runningStep = step;
  if (execute && (isPrepStep(step) || step === 'text_ocr' || step === 'image_ocr')) {
    lastStepPayload = null;
    renderRunningDetail(step);
    renderAll();
    syncStepButtons();
  } else if (!execute && (step === 'text_ocr' || step === 'image_ocr')) {
    toast('加载 OCR 检视…');
    syncStepButtons();
  } else {
    syncStepButtons();
  }
  const llmSteps = new Set(['textbook', 'fusion']);
  if (llmSteps.has(step) && useLlm) body.use_llm = true;
  if (tableDetail.yikuai?.rows?.length) body.cached_yikuai = tableDetail.yikuai;
  const ocrSteps = new Set(['text_ocr', 'image_ocr']);
  const timeoutMs = ocrSteps.has(step) ? 900000 : 120000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/dual-track/step`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: controller.signal,
      },
    );
    const data = await readJson(r);
    if (!r.ok || data.ok === false) {
      if (r.status === 404) throw new Error('接口 404：请重启 .\\dev.ps1');
      const err = data?.error || (r.status === 500 ? `服务器错误 HTTP ${r.status}（请看 dev.ps1 终端日志）` : '请求失败');
      throw new Error(err);
    }
    currentStep = step;
    previewBlocks = Array.isArray(data.preview_blocks) ? data.preview_blocks : [];
    if (data.reload_workspace) {
      await reloadWorkspace({ keepStep: step });
    }
    if (step === 'text_ocr' && data.detail?.prep) {
      syncPrepSlideRowsFromPayload(data);
    }
    if (data.detail?.yikuai) tableDetail.yikuai = data.detail.yikuai;
    if (data.detail?.erkuai) tableDetail.erkuai = data.detail.erkuai;
    if (data.detail?.compare) tableDetail.compare = data.detail.compare;
    if (data.detail?.fusion) tableDetail.fusion = data.detail.fusion;
    lastStepPayload = data;
    saveLastPrepPayload(data);
    if (isPrepStep(step) && (execute || !['text_ocr', 'image_ocr'].includes(step))) {
      markDualTrackPrepDone(step);
    }
    if (step === 'text_ocr' && data.detail?.prep?.run?.executed) {
      const line = formatTextOcrRunLine(data.detail.prep.run, data.detail.prep.summary);
      setRunStatus({ state: 'done', text: `✓ 文字 OCR 已完成 · ${line}` });
    } else if (step === 'text_ocr' && data.detail?.prep?.inspect_only) {
      setRunStatus({ state: 'done', text: '✓ 文字 OCR 检视 · 左侧列表对照中间绿框' });
    } else if (step === 'image_ocr' && data.detail?.prep?.run?.executed) {
      setRunStatus({ state: 'done', text: `✓ 图片 OCR 已完成 · ${formatImageOcrRunLine(data.detail.prep.run)}` });
    } else if (step === 'image_ocr' && data.detail?.prep?.inspect_only) {
      setRunStatus({ state: 'done', text: '✓ 图片 OCR 检视 · 左侧列表对照中间绿框' });
    } else if (!busy) {
      setRunStatus({ state: '', text: '' });
    }
    syncCenterTabs();
    renderDetail(data);
    renderAll();
    finalizeStepUi();
    toast(data.mode_label ? `✓ ${data.mode_label}` : '已更新');
  } catch (e) {
    if (e.name === 'AbortError') {
      toast(`请求超时（${Math.round(timeoutMs / 1000)}s），OCR 步骤可能仍在后台执行，请稍后刷新`);
      setRunStatus({ state: 'error', text: '⏱ 请求超时，请稍后重点「1 文字OCR」或刷新页面查看左侧结果' });
    } else {
      const msg = e.message || String(e);
      toast(msg);
      setRunStatus({ state: 'error', text: `✗ 失败：${msg}` });
      renderDetail({
        step,
        mode_label: '执行失败',
        detail: {
          prep: {
            step_label: PREP_STEP_LABELS[step] || step,
            run: { warnings: [msg] },
          },
        },
      });
    }
  } finally {
    clearTimeout(timer);
    busy = false;
    runningStep = null;
    syncStepButtons();
    finalizeStepUi();
  }
}

async function reloadWorkspace({ keepStep } = {}) {
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/annotate`);
  const data = await readJson(r);
  if (!r.ok || !data.ok) throw new Error(data.error || '加载失败');
  workspace = data;
  syncPrepSlideRowsFromWorkspace();
  syncOldTbDoubaoFromWorkspace();
  liveBlocks = (data.blocks || []).map((b) => ({ ...b, track: 'live', badge: '现网' }));
  if (keepStep) currentStep = keepStep;
  return data;
}

async function loadWorkspace() {
  const data = await reloadWorkspace();
  syncPrepSlideRowsFromWorkspace();
  const pages = data.textbook_pages || [];
  if (pages.length) currentPage = pages[0].page_index;
  const oldPages = data.paired_old?.old_textbook_pages || [];
  if (oldPages.length) oldPageIndex = oldPages[0].page_index;
  const slides = data.paired_old?.old_courseware_slides || [];
  if (slides.length) oldSlideIndex = slides[0].slide_index;
  clearResults();
  lastStepPayload = loadLastPrepPayload();
  syncPrepSlideRowsFromPayload(lastStepPayload);
  syncOldTbDoubaoFromWorkspace();
  if (!prepOldTbDoubao?.pages?.length) {
    syncOldTbDoubaoFromPayload(lastStepPayload);
  }
  if (!prepSlideRows.length) syncPrepSlideRowsFromWorkspace();
  if (dualTrackPrepDone.has('text_ocr')) {
    setRunStatus({
      state: 'done',
      text: '✓ 本会话已跑过文字 OCR · 点「1 文字OCR」可重跑或取消后仅检视',
    });
  } else if (data.prescan_ocr?.text_ocr_complete) {
    const tp = data.prescan_ocr.text_pages_done ?? '?';
    const total = data.textbook_pages?.length ?? '?';
    setRunStatus({
      state: 'done',
      text: `annotate 库内已有文字 OCR（${tp}/${total} 页）· 点「1 文字OCR」在本页执行并打钩`,
    });
  }
  renderAll();
  syncPrepButtonBadges();
  toast('已加载 · 请点击「1 文字OCR」开始（或清空后逐步点）');
}

document.querySelectorAll('.dt-step-btn[data-step]').forEach((btn) => {
  btn.addEventListener('click', () => runStep(btn.dataset.step));
});

document.getElementById('dt-btn-clear')?.addEventListener('click', () => {
  if (busy) return;
  clearResults();
  toast('已清空，请从顶栏选一步');
});

document.getElementById('dt-use-llm')?.addEventListener('change', (e) => {
  useLlm = !!e.target.checked;
});

document.querySelectorAll('.dt-center-tab').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.disabled) return;
    setCenterView(btn.dataset.centerView);
  });
});

document.getElementById('dt-fit-toggle')?.addEventListener('click', () => {
  fitHeight = !fitHeight;
  syncFitToggle();
  if (centerView === 'new') paintNewAtoms();
  else if (centerView === 'textbook') paintOldAtoms();
});

document.getElementById('dt-page-prev')?.addEventListener('click', () => {
  if (centerView === 'courseware') {
    if (oldSlideIndex > 1) oldSlideIndex -= 1;
  } else if (centerView === 'textbook') {
    if (oldPageIndex > 1) oldPageIndex -= 1;
  } else if (currentPage > 1) {
    currentPage -= 1;
  } else {
    return;
  }
  renderCenterNav();
  renderCenterView();
  refreshDetailIfOcrStep();
});

document.getElementById('dt-page-next')?.addEventListener('click', () => {
  if (centerView === 'courseware') {
    if (oldSlideIndex < oldSlideCount()) oldSlideIndex += 1;
  } else if (centerView === 'textbook') {
    if (oldPageIndex < oldPageCount()) oldPageIndex += 1;
  } else if (currentPage < pageCount()) {
    currentPage += 1;
  } else {
    return;
  }
  renderCenterNav();
  renderCenterView();
  refreshDetailIfOcrStep();
});

window.addEventListener('resize', () => {
  if (centerView === 'new') paintNewAtoms();
  else if (centerView === 'textbook') paintOldAtoms();
});

loadWorkspace().catch((e) => {
  const sub = document.getElementById('page-sub');
  if (sub) sub.textContent = e.message || String(e);
  toast(e.message || '加载失败');
});
