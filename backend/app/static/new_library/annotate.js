const LESSON_UID = window.ANNOTATE_LESSON_UID;
const API = window.ANNOTATE_API_PREFIX || '/api/new-library';

const INTAKE_FOCUS_KEY = 'new-library-intake-focus';
const INTAKE_RELOAD_CHANNEL = 'new-library-intake-reload';

function requestIntakeFocus() {
  try {
    localStorage.setItem(INTAKE_FOCUS_KEY, String(Date.now()));
  } catch {
    /* ignore */
  }
  try {
    const ch = new BroadcastChannel(INTAKE_RELOAD_CHANNEL);
    ch.postMessage({ type: 'reload-volume', at: Date.now() });
    ch.close();
  } catch {
    /* ignore */
  }
}

function closeAnnotateOrBack() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('from') === 'compare') {
    const ret = params.get('return');
    if (ret && ret.startsWith('/')) {
      window.location.href = ret;
      return;
    }
  }

  requestIntakeFocus();

  if (window.opener && !window.opener.closed) {
    try {
      window.opener.focus();
    } catch {
      /* same-origin only */
    }
  }

  window.close();

  setTimeout(() => {
    if (document.visibilityState !== 'visible') return;
    if (window.history.length > 1) {
      history.back();
      return;
    }
    toast('整册建设页已激活，请手动关闭此标签');
  }, 150);
}

const ANALYSIS_SEGS = [
  { key: 'text_ocr', name: '文字OCR' },
  { key: 'image_ocr', name: '图片OCR' },
  { key: 'curate', name: '整理原子' },
  { key: 'block_match', name: '栏目·块名' },
  { key: 'prescan', name: '对照预判' },
  { key: 'pair_review', name: '确认对照' },
];

const BLOCK_SEGS = [
  { key: 'build', name: '创建区块' },
  { key: 'anchor', name: '锚定旧块' },
  { key: 'lock', name: '保存锁定' },
];

const BLOCK_PIPELINE_STEPS = [
  { key: 'column_detect', name: '认栏目头' },
  { key: 'topic_cluster', name: '原子分组' },
  { key: 'balance', name: '合并碎块' },
  { key: 'attributes', name: '对照建块' },
  { key: 'anchor', name: '挂旧块锚' },
  { key: 'validate', name: '检查锚定' },
  { key: 'edit_ready', name: '待核对' },
];

const SUSPICIOUS_REASON_LABELS = {
  duplicate_anchor: '同一旧块被多个新区块锚定',
  order_jump: '锚定顺序与旧课块顺序不一致',
  low_gap: '匹配备选分差过小，建议人工确认',
};

const BLOCK_PATH_LABELS = {
  old_mirror: '旧块镜像',
  old_mirror_llm: '豆包对照旧块',
  anchor_llm: '豆包锚定旧块',
  new_cluster: '原子分组',
  manual: '手动',
};

let blockPipelinePollTimer = null;

const PROGRESS_STEP_ACTIONS = {
  text_ocr: 'ocr-text-btn',
  image_ocr: 'ocr-image-btn',
  curate: 'ai-curate-lesson-btn',
  block_match: 'prescan-suggest-btn',
  prescan: 'prescan-suggest-btn',
  pair_review: null,
  build: 'seed-from-old-lesson-btn',
  anchor: 'anchor-page-btn',
  lock: 'lock-blocks-btn',
};

let progressStepsExpanded = false;
let progressStepsResizeTimer = null;

let analysisPipelineCollapsed = true;
let blockPipelineCollapsed = true;

const PIPELINE_STEP_SLOTS = 7;

function emptyAnalysisSegments(runningKey = 'text_ocr') {
  const segs = {
    text_ocr: 'pending',
    image_ocr: 'pending',
    curate: 'pending',
    block_match: 'pending',
    prescan: 'pending',
    pair_review: 'pending',
  };
  if (runningKey) segs[runningKey] = 'running';
  return segs;
}

function beginAnalysisRerunState() {
  const segs = emptyAnalysisSegments('text_ocr');
  return {
    _analysisBusy: true,
    _analysisOptimisticSegments: segs,
    analysis: {
      ...(state?.analysis || {}),
      stage: 'running',
      label: '对照分析进行中…',
      ready_for_blocks: false,
      segments: { ...segs },
    },
  };
}

function clearAnalysisOptimisticState() {
  if (!state) return;
  const next = { ...state };
  delete next._analysisOptimisticSegments;
  state = next;
}

let state = null;
let currentPageIndex = 1;
let oldCurrentPageIndex = 1;
let refOldSyncWithNew = true;
/** 点击区块临时跳到旧块所在页；中间翻新教材页后自动清除并恢复同步 */
let oldPageOverride = null;
let selectedOldBlockCode = null;

const MODULE_HIGHLIGHT_COLORS = [
  '#14b8a6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6',
  '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#6366f1',
];
const selectedAtoms = new Set();
let undoSnapshot = null;
/** @type {string | null} */
let editingBlockCode = null;
/** @type {string | null} */
let focusedBlockCode = null;

/** 双轨实验预览（不写库）；step=live 时显示数据库现网块 */
const dualTrackPreview = {
  active: false,
  step: 'live',
  modeLabel: '',
  blocks: [],
  lastCompare: null,
};
/** @type {string | null} */
let focusedOldBlockCode = null;
/** @type {string} */
let editingBlockName = '';
/** @type {string} */
let editingStageRef = '';
const STAGE_REF_CUSTOM = '__custom__';

let hideAtomTags = false;
try {
  hideAtomTags = localStorage.getItem('annotate.hideAtomTags') === '1';
} catch {
  hideAtomTags = false;
}

function applyAtomTagVisibility() {
  const layer = document.getElementById('atom-layer');
  if (layer) layer.classList.toggle('hide-atom-tags', hideAtomTags);
  const cb = document.getElementById('hide-atom-tags-checkbox');
  if (cb) cb.checked = hideAtomTags;
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 5000);
}

async function readJson(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`服务器返回异常（HTTP ${r.status}）`);
  }
}

function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function atomsOnPage(pageIndex) {
  return (state?.atoms || []).filter((a) => a.page_index === pageIndex);
}

function blocksForDisplay() {
  if (dualTrackPreview.active && dualTrackPreview.blocks.length) {
    return dualTrackPreview.blocks;
  }
  return state?.blocks || [];
}

function isDualTrackPreview() {
  return dualTrackPreview.active && dualTrackPreview.step !== 'live';
}

function blockOccupancy() {
  const atomOwners = new Map();
  for (const b of blocksForDisplay()) {
    for (const code of b.atom_codes || []) atomOwners.set(code, b);
  }
  return { atomOwners };
}

function atomOwnerBlock(atomCode) {
  return blockOccupancy().atomOwners.get(atomCode) || null;
}

function blockColor(blockCode) {
  const codes = blocksForDisplay().map((b) => b.block_code);
  const idx = codes.indexOf(blockCode);
  const palette = MODULE_HIGHLIGHT_COLORS;
  return palette[idx >= 0 ? idx % palette.length : 0];
}

/** 旧块高亮色：优先与锚定的新块同色，便于左右对照 */
function oldBlockColor(oldBlockCode) {
  const linkedNew = findNewBlockByOldAnchor(oldBlockCode);
  if (linkedNew) return blockColor(linkedNew.block_code);
  const codes = (state?.paired_old?.old_blocks || []).map((b) => b.block_code);
  const idx = codes.indexOf(oldBlockCode);
  return MODULE_HIGHLIGHT_COLORS[idx >= 0 ? idx % MODULE_HIGHLIGHT_COLORS.length : 0];
}

function blocksColumnScroller() {
  return document.querySelector('#blocks-col .blocks-col-scroll')
    || document.getElementById('blocks-work-panel')
    || document.getElementById('blocks-col');
}

function scrollBlockIntoView(blockCode) {
  const row = document.querySelector(`#blocks-list [data-block-code="${blockCode}"]`);
  if (!row) return;
  const scroller = blocksColumnScroller();
  if (scroller) {
    const sRect = scroller.getBoundingClientRect();
    const rRect = row.getBoundingClientRect();
    if (rRect.top < sRect.top || rRect.bottom > sRect.bottom) {
      scroller.scrollTo({
        top: scroller.scrollTop + (rRect.top - sRect.top) - 12,
        behavior: 'smooth',
      });
      return;
    }
  }
  row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function scrollBlocksResultsIntoView() {
  const list = document.getElementById('blocks-list');
  const scroller = blocksColumnScroller();
  const head = document.querySelector('#blocks-work-panel > .col-head');
  requestAnimationFrame(() => {
    if (head && scroller) {
      scroller.scrollTo({
        top: Math.max(0, head.offsetTop - 4),
        behavior: 'smooth',
      });
    } else {
      list?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    const first = list?.querySelector('li[data-block-code]');
    if (first) scrollBlockIntoView(first.dataset.blockCode);
  });
}

function highlightBlockCode() {
  return editingBlockCode || focusedBlockCode;
}

function highlightOldBlockCode() {
  return focusedOldBlockCode || selectedOldBlockCode;
}

function isAtomInHighlightedBlock(atomCode) {
  const code = highlightBlockCode();
  if (!code) return false;
  const owner = atomOwnerBlock(atomCode);
  return owner?.block_code === code;
}

function firstPageForBlockAtoms(block) {
  const atomCodes = block?.atom_codes || [];
  if (!atomCodes.length) return null;
  let minPage = null;
  for (const atomCode of atomCodes) {
    const atom = (state?.atoms || []).find((x) => x.atom_code === atomCode);
    if (atom?.page_index == null) continue;
    minPage = minPage == null ? atom.page_index : Math.min(minPage, atom.page_index);
  }
  return minPage;
}

function firstPageForOldBlock(block) {
  if (block?.textbook_page_start != null) return block.textbook_page_start;
  const atoms = state?.paired_old?.old_atoms || [];
  let minPage = null;
  for (const atomCode of block?.atom_codes || []) {
    const atom = atoms.find((x) => x.atom_code === atomCode);
    if (atom?.page_index == null) continue;
    minPage = minPage == null ? atom.page_index : Math.min(minPage, atom.page_index);
  }
  return minPage;
}

function findOldBlock(oldBlockCode) {
  return (state?.paired_old?.old_blocks || []).find((b) => b.block_code === oldBlockCode) || null;
}

function findNewBlockByOldAnchor(oldBlockCode) {
  return (state?.blocks || []).find((b) => {
    const ref = anchorRefForBlock(b);
    return ref?.old_block_code === oldBlockCode;
  }) || null;
}

function navigateToNewBlockContent(block) {
  if (!block) return false;
  const page = firstPageForBlockAtoms(block);
  if (page != null && page !== currentPageIndex) {
    currentPageIndex = page;
    if (!editingBlockCode) selectedAtoms.clear();
    return true;
  }
  return false;
}

function navigateToOldBlockContent(block) {
  if (!block) return false;
  const page = firstPageForOldBlock(block);
  if (page != null && page !== oldCurrentPageIndex) {
    oldCurrentPageIndex = page;
    oldPageOverride = page;
    return true;
  }
  return false;
}

function scrollHighlightedAtomsIntoView() {
  const code = highlightBlockCode();
  if (!code) return;
  requestAnimationFrame(() => {
    const layer = document.getElementById('atom-layer');
    const viewport = document.getElementById('tb-viewport');
    if (!layer) return;
    const box = layer.querySelector('.atom-box.selected, .atom-box.editing-own');
    if (!box) return;
    if (viewport) {
      const vRect = viewport.getBoundingClientRect();
      const bRect = box.getBoundingClientRect();
      viewport.scrollTo({
        top: Math.max(0, bRect.top - vRect.top + viewport.scrollTop - viewport.clientHeight * 0.2),
        left: Math.max(0, bRect.left - vRect.left + viewport.scrollLeft - viewport.clientWidth * 0.15),
        behavior: 'smooth',
      });
    } else {
      box.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
    }
  });
}

function syncBlockFocusChrome() {
  const active = !!highlightBlockCode();
  document.getElementById('atom-layer')?.classList.toggle('block-focus-mode', active);
  document.getElementById('tb-viewport')?.classList.toggle('block-focus-mode', active);
  const oldFocus = !!highlightOldBlockCode();
  document.getElementById('old-module-layer')?.classList.toggle('old-block-focus-mode', oldFocus);
}

function scrollHighlightedOldBlockIntoView() {
  const code = highlightOldBlockCode();
  if (!code) return;
  requestAnimationFrame(() => {
    const layer = document.getElementById('old-module-layer');
    const viewport = document.getElementById('old-tb-viewport');
    if (!layer || !viewport) return;
    const box = layer.querySelector('.module-highlight-group.active-old .module-highlight-box');
    if (!box) return;
    const vRect = viewport.getBoundingClientRect();
    const bRect = box.getBoundingClientRect();
    viewport.scrollTo({
      top: Math.max(0, bRect.top - vRect.top + viewport.scrollTop - viewport.clientHeight * 0.2),
      behavior: 'smooth',
    });
  });
}

function afterBlockFocusRender() {
  syncBlockFocusChrome();
  refreshOldRefViewer();
  scrollHighlightedAtomsIntoView();
  scrollHighlightedOldBlockIntoView();
  const code = focusedBlockCode || editingBlockCode;
  if (code) scrollBlockIntoView(code);
}

function clearBlockFocus() {
  focusedBlockCode = null;
  focusedOldBlockCode = null;
}

function unfocusBlock() {
  if (editingBlockCode) return;
  clearBlockFocus();
  selectedOldBlockCode = null;
  oldPageOverride = null;
  syncOldBlockPickUi();
  renderHeader();
  renderTextbookPageNav();
  renderTextbook();
  renderBlocks();
  afterBlockFocusRender();
}

function focusBlock(block) {
  if (!block) return;
  if (editingBlockCode && editingBlockCode !== block.block_code) {
    startBlockEdit(block);
    return;
  }
  if (!editingBlockCode && focusedBlockCode === block.block_code) {
    unfocusBlock();
    return;
  }
  focusedBlockCode = block.block_code;
  const anchorRef = anchorRefForBlock(block);
  if (anchorRef?.old_block_code) {
    focusedOldBlockCode = anchorRef.old_block_code;
    selectedOldBlockCode = anchorRef.old_block_code;
    const oldBlock = findOldBlock(anchorRef.old_block_code);
    if (oldBlock) navigateToOldBlockContent(oldBlock);
  } else {
    focusedOldBlockCode = null;
    if (refOldSyncWithNew) oldPageOverride = null;
  }
  navigateToNewBlockContent(block);
  syncOldBlockPickUi();
  renderHeader();
  renderTextbookPageNav();
  renderTextbook();
  renderBlocks();
  afterBlockFocusRender();
}

function focusOldBlock(oldBlockCode) {
  if (!oldBlockCode) return;
  const oldBlock = findOldBlock(oldBlockCode);
  if (!oldBlock) return;
  const linkedNew = findNewBlockByOldAnchor(oldBlockCode);
  if (
    !editingBlockCode
    && focusedOldBlockCode === oldBlockCode
    && (!linkedNew || focusedBlockCode === linkedNew.block_code)
  ) {
    unfocusBlock();
    return;
  }
  focusedOldBlockCode = oldBlockCode;
  selectedOldBlockCode = oldBlockCode;
  navigateToOldBlockContent(oldBlock);
  if (linkedNew) {
    focusedBlockCode = linkedNew.block_code;
    navigateToNewBlockContent(linkedNew);
  } else {
    focusedBlockCode = null;
  }
  syncOldBlockPickUi();
  renderHeader();
  renderTextbookPageNav();
  renderTextbook();
  renderBlocks();
  afterBlockFocusRender();
}

function handleAtomClick(atomCode) {
  const owner = atomOwnerBlock(atomCode);
  if (owner && !(editingBlockCode && owner.block_code === editingBlockCode)) {
    if (!editingBlockCode && focusedBlockCode === owner.block_code) {
      unfocusBlock();
    } else {
      focusBlock(owner);
    }
    return;
  }
  if (selectedAtoms.has(atomCode)) selectedAtoms.delete(atomCode);
  else selectedAtoms.add(atomCode);
  catalogFocusAtomCode = atomCode;
  paintAtomLayer();
  syncAtomActionButtons();
  syncBlockFormUi();
  if (!inBuildAnchorPhase()) renderAtomCatalog();
}

function canPickAtom(atomCode) {
  const owner = atomOwnerBlock(atomCode);
  if (!owner) return true;
  return editingBlockCode != null && owner.block_code === editingBlockCode;
}

function atomLabel(a) {
  const t = (a.content || a.ocr_text || '').replace(/\s+/g, ' ').trim().slice(0, 24);
  return t ? `${a.atom_code} ${t}` : a.atom_code;
}

function blocksLocked() {
  return !!(state?.lesson?.blocks_locked);
}

function textbookPageIndexes() {
  return (state?.textbook_pages || []).map((p) => p.page_index).sort((a, b) => a - b);
}

function currentTextbookPagePosition() {
  const pages = textbookPageIndexes();
  const idx = pages.indexOf(currentPageIndex);
  return { pages, idx, total: pages.length };
}

function gotoTextbookPageIndex(pageIndex, { keepFocus = false } = {}) {
  currentPageIndex = pageIndex;
  catalogFocusAtomCode = null;
  if (refOldSyncWithNew) oldPageOverride = null;
  if (!keepFocus) clearBlockFocus();
  refreshOldRefViewer();
  renderTextbook();
  renderHeader();
  renderTextbookPageNav();
  renderBlocks();
  renderOldPrescanBridge();
  renderAnalysisRightPanel();
  syncAtomActionButtons();
}

function gotoTextbookPage(delta) {
  const { pages, idx } = currentTextbookPagePosition();
  if (idx < 0) return;
  const newIdx = idx + delta;
  if (newIdx < 0 || newIdx >= pages.length) return;
  gotoTextbookPageIndex(pages[newIdx]);
}

function syncTextbookPageIndex() {
  const pages = state?.textbook_pages || [];
  if (!pages.length) return;
  const pageIndexes = new Set(pages.map((p) => p.page_index));
  if (!pageIndexes.has(currentPageIndex)) {
    currentPageIndex = pages[0].page_index;
  }
}

function renderTextbookPageNav() {
  syncTextbookPageIndex();
  const nav = document.getElementById('textbook-page-nav');
  const shell = document.querySelector('.tb-viewport-shell');
  const info = document.getElementById('tb-page-info');
  const prevBtn = document.getElementById('tb-prev-btn');
  const nextBtn = document.getElementById('tb-next-btn');
  const { idx, total } = currentTextbookPagePosition();
  if (!nav) return;
  if (!total) {
    nav.hidden = true;
    if (shell) shell.hidden = true;
    return;
  }
  nav.hidden = false;
  if (shell) shell.hidden = false;
  if (info) {
    const page = (state?.textbook_pages || []).find((p) => p.page_index === currentPageIndex);
    const pdfNote = page?.pdf_page ? `（PDF ${page.pdf_page}）` : '';
    info.textContent = `新教材第 ${currentPageIndex} 页${pdfNote} · ${idx + 1}/${total}`;
  }
  if (prevBtn) prevBtn.disabled = idx <= 0;
  if (nextBtn) nextBtn.disabled = idx >= total - 1;
}

function syncAtomActionButtons() {
  const n = selectedAtoms.size;
  const label = document.getElementById('atom-selection-label');
  const mergeBtn = document.getElementById('merge-atoms-btn');
  const unmergeBtn = document.getElementById('unmerge-ocr-btn');
  const delBtn = document.getElementById('delete-atoms-btn');
  const clearBtn = document.getElementById('clear-atoms-btn');
  const undoBtn = document.getElementById('undo-atoms-btn');
  if (label) {
    label.textContent = n ? `已选 ${n} 个原子` : '未选中原子';
  }
  if (mergeBtn) mergeBtn.disabled = n < 2;
  if (unmergeBtn) {
    const code = n === 1 ? Array.from(selectedAtoms)[0] : null;
    const atom = code ? (state?.atoms || []).find((x) => x.atom_code === code) : null;
    const pageHasBaseline = (state?.textbook_pages || []).some(
      (p) => p.page_index === currentPageIndex && p.has_ocr_baseline,
    );
    unmergeBtn.disabled = !atom || !pageHasBaseline || !atom.can_unmerge_ocr;
    unmergeBtn.title = atom?.can_unmerge_ocr
      ? `拆回约 ${atom.ocr_source_count} 个 OCR 碎块；也可双击原子框`
      : '选中一个误合并的原子后可拆回 OCR；也可双击可拆原子框';
  }
  if (delBtn) delBtn.disabled = n < 1;
  if (clearBtn) clearBtn.hidden = !n;
  if (undoBtn) undoBtn.disabled = !undoSnapshot;
}

function captureUndoSnapshot() {
  undoSnapshot = {
    page_index: currentPageIndex,
    atoms: atomsOnPage(currentPageIndex).map((a) => ({ ...a })),
  };
  syncAtomActionButtons();
}

function clearUndoSnapshot() {
  undoSnapshot = null;
  syncAtomActionButtons();
}

function syncBlockFormUi() {
  const locked = blocksLocked();
  const blocks = state?.blocks || [];
  const btn = document.getElementById('create-block-btn');
  const cancelBtn = document.getElementById('cancel-block-edit-btn');
  const hint = document.getElementById('block-form-hint');
  const navHint = document.getElementById('blocks-nav-hint');
  const formWrap = document.getElementById('block-form-wrap');
  const lockBtn = document.getElementById('lock-blocks-btn');
  const unlockBtn = document.getElementById('unlock-blocks-btn');
  const lockedBadge = document.getElementById('blocks-locked-badge');
  const atomN = selectedAtoms.size;
  if (lockedBadge) lockedBadge.hidden = !locked;
  if (formWrap) formWrap.hidden = locked;
  if (lockBtn) lockBtn.hidden = locked || !blocks.length;
  if (unlockBtn) unlockBtn.hidden = !locked;
  if (btn) {
    if (locked) btn.hidden = true;
    else if (editingBlockCode) {
      btn.hidden = false;
      btn.textContent = '保存修改';
    } else {
      btn.hidden = false;
      btn.textContent = '创建区块';
    }
  }
  if (navHint) {
    if (locked) {
      navHint.textContent = blocks.length
        ? `已锁定 ${blocks.length} 个新区块`
        : '尚无区块';
    } else if (editingBlockCode) {
      navHint.textContent = `编辑 ${editingBlockCode} · 已选原子 ${atomN} 个`;
    } else if (atomN) {
      navHint.textContent = `待创建 · 已选原子 ${atomN} 个 · 填好模块后点「创建区块」`;
    } else {
      const wf = computeWorkflowGuide();
      if (wf.phase === 'atoms') {
        navHint.textContent = '先整理原子，再建区块';
      } else if (wf.phase === 'blocks') {
        navHint.textContent = '原子已就绪 · 可点「AI 建整课」或勾选原子创建';
      } else if (wf.phase === 'anchor') {
        navHint.textContent = '请补锚定后保存';
      } else {
        navHint.textContent = '确认无误后点顶部「保存锁定」';
      }
    }
  }
  if (cancelBtn) cancelBtn.hidden = locked || !editingBlockCode;
  const deleteAllBtn = document.getElementById('delete-all-blocks-btn');
  if (deleteAllBtn) {
    deleteAllBtn.hidden = locked || !blocks.length || !!editingBlockCode;
  }
  if (hint) {
    hint.hidden = locked;
    if (!locked) {
      hint.textContent = editingBlockCode
        ? `编辑 ${editingBlockCode}：可改模块与区块名称；点击图上原子可增删。`
        : '点击区块卡片或图上原子可高亮对照；选择模块并勾选原子后创建新区块。';
    }
  }
  syncOldBlockPickUi();
}

function stagePresetLabels() {
  return new Set((state?.block_stage_options || []).map((o) => o.label).filter(Boolean));
}

function isCustomStageRef(value) {
  const v = (value || '').trim();
  return !!v && !stagePresetLabels().has(v);
}

function syncStageRefControls(ref, { blockCode = null } = {}) {
  const value = String(ref ?? '').trim();
  const sel = blockCode
    ? document.querySelector(`[data-block-stage-select="${blockCode}"]`)
    : document.getElementById('block-stage-ref');
  const customInp = blockCode
    ? document.querySelector(`[data-block-stage-custom="${blockCode}"]`)
    : document.getElementById('block-stage-custom');
  if (!sel) return;

  const presets = stagePresetLabels();
  if (!value) {
    sel.value = '';
    if (customInp) {
      customInp.value = '';
      customInp.hidden = true;
    }
    return;
  }
  if (presets.has(value)) {
    sel.value = value;
    if (customInp) {
      customInp.value = '';
      customInp.hidden = true;
    }
    return;
  }
  sel.value = STAGE_REF_CUSTOM;
  if (customInp) {
    customInp.value = value;
    customInp.hidden = false;
  }
}

function readStageRefFromControls(blockCode = null) {
  const sel = blockCode
    ? document.querySelector(`[data-block-stage-select="${blockCode}"]`)
    : document.getElementById('block-stage-ref');
  const customInp = blockCode
    ? document.querySelector(`[data-block-stage-custom="${blockCode}"]`)
    : document.getElementById('block-stage-custom');
  if (!sel) return '';
  if (sel.value === STAGE_REF_CUSTOM) return (customInp?.value || '').trim();
  return (sel.value || '').trim();
}

function getBlockFormName() {
  if (editingBlockCode) return (editingBlockName || '').trim();
  return (document.getElementById('block-name')?.value || '').trim();
}

function getBlockFormStageRef() {
  if (editingBlockCode) {
    return readStageRefFromControls(editingBlockCode) || (editingStageRef || '').trim();
  }
  return readStageRefFromControls();
}

function setBlockFormName(name) {
  const v = String(name || '');
  if (editingBlockCode) editingBlockName = v;
  const top = document.getElementById('block-name');
  if (top) top.value = v;
}

function setBlockFormStageRef(ref) {
  const v = String(ref || '').trim();
  if (editingBlockCode) {
    editingStageRef = v;
    syncStageRefControls(v, { blockCode: editingBlockCode });
    return;
  }
  syncStageRefControls(v, {});
}

function onStageRefSelectChange(sel, blockCode = null) {
  const customInp = blockCode
    ? document.querySelector(`[data-block-stage-custom="${blockCode}"]`)
    : document.getElementById('block-stage-custom');
  if (sel.value === STAGE_REF_CUSTOM) {
    if (customInp) {
      customInp.hidden = false;
      customInp.focus();
    }
    const v = (customInp?.value || '').trim();
    if (editingBlockCode) editingStageRef = v;
    return;
  }
  if (customInp) {
    customInp.hidden = true;
    customInp.value = '';
  }
  const v = (sel.value || '').trim();
  if (editingBlockCode) editingStageRef = v;
}

function renderBlockStageOptionsHtml(currentValue) {
  const options = state?.block_stage_options || [];
  const current = (currentValue || '').trim();
  const isCustom = isCustomStageRef(current);
  let html = '<option value="">— 选择模块 —</option>';
  for (const o of options) {
    const selected = !isCustom && current && current === o.label ? ' selected' : '';
    html += `<option value="${escHtml(o.label)}"${selected}>${escHtml(o.label)}</option>`;
  }
  const customSel = isCustom ? ' selected' : '';
  html += `<option value="${STAGE_REF_CUSTOM}"${customSel}>自设…</option>`;
  return html;
}

function atomNameSuggestions(atomCodes) {
  const codes = new Set(atomCodes || []);
  const out = [];
  for (const a of state?.atoms || []) {
    if (!codes.has(a.atom_code)) continue;
    const text = (a.content || a.ocr_text || '').trim().replace(/\s+/g, ' ');
    if (!text || text.startsWith('[')) continue;
    const short = text.length > 28 ? `${text.slice(0, 28)}…` : text;
    if (!out.includes(short)) out.push(short);
    if (out.length >= 8) break;
  }
  return out;
}

function atomSummaryLine(a) {
  const raw = (a.content || a.ocr_text || '').replace(/\s+/g, ' ').trim();
  if (!raw || raw.startsWith('[')) {
    if (a.atom_type === 'image') return '图片';
    return '';
  }
  return raw.length > 48 ? `${raw.slice(0, 48)}…` : raw;
}

function blockSourceTagsHtml(block) {
  const meta = block.metadata_json || {};
  const bp = meta.block_pipeline || {};
  const pathKey = bp.source_path || meta.source_path || '';
  const path = BLOCK_PATH_LABELS[pathKey] || pathKey;
  const score = meta.match_score;
  const parts = [];
  const stage = (block.stage_ref || '').trim();
  if (stage) parts.push(`<span class="block-tag block-tag-stage">${escHtml(stage)}</span>`);
  if (score != null && !Number.isNaN(Number(score))) {
    parts.push(`<span class="block-tag block-tag-score">匹配 ${Math.round(Number(score) * 100)}%</span>`);
  }
  if (path) parts.push(`<span class="block-tag block-tag-path">${escHtml(path)}</span>`);
  return parts.length ? `<div class="block-card-tags">${parts.join('')}</div>` : '';
}

function renderBlockAtomListHtml(block) {
  const codes = block.atom_codes || [];
  const atomsByCode = new Map((state?.atoms || []).map((a) => [a.atom_code, a]));
  if (!codes.length) return '<p class="hint block-atom-empty">无绑定原子</p>';
  const items = codes.map((code) => {
    const a = atomsByCode.get(code);
    if (!a) {
      return `<li class="block-atom-item"><code>${escHtml(code)}</code></li>`;
    }
    const icon = ATOM_TYPE_ICON[a.atom_type] || '·';
    const label = atomTypeLabel(a.atom_type);
    const line = atomSummaryLine(a);
    const lineHtml = line ? `<span class="block-atom-text">${escHtml(line)}</span>` : '';
    return (
      `<li class="block-atom-item">` +
      `<code>${escHtml(code)}</code>` +
      `<span class="block-atom-type">${icon} ${escHtml(label)}</span>` +
      lineHtml +
      `</li>`
    );
  }).join('');
  return `<ul class="block-atom-list">${items}</ul>`;
}

function renderBlockTextbookSection(block) {
  const n = (block.atom_codes || []).length;
  return (
    `<details class="block-card-section block-card-textbook" open>` +
    `<summary class="block-card-section-head">` +
    `<span>📖 教材内容</span>` +
    `<span class="block-card-section-meta">${n} 个原子</span>` +
    `</summary>` +
    `<div class="block-card-section-body">${renderBlockAtomListHtml(block)}</div>` +
    `</details>`
  );
}

function renderBlockOldRefSection(block) {
  const ref = anchorRefForBlock(block);
  const locked = blocksLocked();
  if (!ref?.old_block_code) {
    return (
      `<div class="block-card-section block-card-oldref block-card-oldref--empty">` +
      `<div class="block-card-section-head-inline">🔗 对照旧块</div>` +
      `<span class="hint">未锚定旧块</span>` +
      `</div>`
    );
  }
  const name = ref.old_block_name || oldBlockName(ref.old_block_code) || '';
  const cw = (ref.cw_pgs || []).length ? ` · 课件 p${ref.cw_pgs.join(',')}` : '';
  const oldUrl = ref.old_lesson_uid
    ? `/old-library/lessons/${encodeURIComponent(ref.old_lesson_uid)}/annotate`
    : '';
  const preview = oldUrl
    ? `<a class="btn-link block-oldref-link" href="${oldUrl}" target="_blank" rel="noopener">打开旧库</a>`
    : '';
  const clearBtn = locked
    ? ''
    : `<button type="button" class="btn-link block-anchor-clear" data-clear-anchor="${escHtml(block.block_code)}">解除锚定</button>`;
  return (
    `<div class="block-card-section block-card-oldref">` +
    `<div class="block-card-section-head-inline">🔗 对照旧块</div>` +
    `<div class="block-oldref-body">` +
    `<strong>${escHtml(ref.old_block_code)}</strong> ${escHtml(name)}` +
    `<span class="block-oldref-meta">${escHtml(cw)}</span> ${preview} ${clearBtn}` +
    `</div>` +
    `</div>`
  );
}

function renderBlockCardHead(block, editing) {
  const stage = (block.stage_ref || '').trim();
  const name = (block.block_name || '').trim();
  const title = name || stage || '未命名区块';
  const previewBadge = block.badge
    ? `<span class="dual-track-badge dual-track-badge--${escHtml(block.track || 'fusion')}">${escHtml(block.badge)}</span>`
    : '';
  const srcOld = block.source_old_block
    ? `<span class="dual-track-src-old">←旧 ${escHtml(block.source_old_block)}</span>`
    : '';
  return (
    `<div class="block-card-head">` +
    `<div class="block-card-title-row">` +
    `<strong class="block-card-code">${escHtml(block.block_code)}</strong>` +
    (editing ? '' : `<span class="block-card-name">${escHtml(title)}</span>`) +
    previewBadge +
    srcOld +
    `</div>` +
    `${editing ? '' : blockSourceTagsHtml(block)}` +
    `${editing ? renderBlockMetaEditHtml(block) : ''}` +
    `</div>`
  );
}

function renderBlockStatusFooter(block, { editing, locked, suspicious, reasonLabel, suspiciousIcon }) {
  const statusOk = !suspicious;
  const statusText = statusOk ? '锚定正常' : escHtml(reasonLabel);
  const statusCls = statusOk ? 'block-status-ok' : 'block-status-warn';
  const suspiciousHint = suspicious
    ? `<p class="block-suspicious-hint hint">锚定存疑：${escHtml(reasonLabel)}（建议人工复核）</p>`
    : '';
  return (
    `<div class="block-card-footer">` +
    `<div class="block-card-status ${statusCls}">${suspiciousIcon}<span>${statusText}</span></div>` +
    suspiciousHint +
    `<div class="block-card-actions">` +
    `<button type="button" class="btn-link" data-edit-block="${block.block_code}">${editing ? '编辑中…' : '编辑'}</button>` +
    `<button type="button" class="btn-link danger" data-del-block="${block.block_code}" ${locked ? 'disabled' : ''}>删除</button>` +
    `</div>` +
    `</div>`
  );
}

function renderBlockMetaReadonlyHtml(block) {
  const stage = (block.stage_ref || '').trim();
  const name = (block.block_name || '').trim();
  return (
    `<div class="block-meta-rows">` +
    `<div class="block-meta-row">` +
    `<span class="block-meta-label">新教材模块</span>` +
    `<span class="block-meta-value">${stage ? escHtml(stage) : '<span class="block-meta-empty">—</span>'}</span>` +
    `</div>` +
    `<div class="block-meta-row">` +
    `<span class="block-meta-label">区块名称</span>` +
    `<span class="block-meta-value block-item-name">${name ? escHtml(name) : '<span class="block-meta-empty">未命名</span>'}</span>` +
    `</div>` +
    `</div>`
  );
}

function renderBlockMetaEditHtml(block) {
  const suggestions = atomNameSuggestions(Array.from(selectedAtoms));
  const stage = (editingStageRef || '').trim();
  const isCustom = isCustomStageRef(stage);
  const suggHtml = suggestions.length
    ? (
      `<div class="block-name-suggestions">` +
      `<span class="block-name-sugg-label">从原子取名：</span>` +
      suggestions.map((s) =>
        `<button type="button" class="block-name-sugg" data-apply-name="${escHtml(s)}">${escHtml(s)}</button>`,
      ).join('') +
      `</div>`
    )
    : '';
  return (
    `<div class="block-meta-rows">` +
    `<div class="block-meta-row">` +
    `<span class="block-meta-label">新教材模块</span>` +
    `<div class="block-stage-edit-col">` +
    `<select class="block-stage-inline" data-block-stage-select="${block.block_code}">` +
    renderBlockStageOptionsHtml(editingStageRef) +
    `</select>` +
    `<input type="text" class="block-stage-custom" data-block-stage-custom="${block.block_code}" ` +
    `value="${escHtml(isCustom ? stage : '')}" placeholder="输入自设模块" ${isCustom ? '' : 'hidden'} />` +
    `</div>` +
    `</div>` +
    `<div class="block-meta-row block-meta-row-name">` +
    `<span class="block-meta-label">区块名称</span>` +
    `<div class="block-name-edit-col">` +
    `<input type="text" class="block-name-inline" data-block-name-input="${block.block_code}" ` +
    `value="${escHtml(editingBlockName)}" placeholder="总结本块内容，如：科学探究-捏陶泥" />` +
    suggHtml +
    `</div>` +
    `</div>` +
    `</div>`
  );
}

function bindBlockMetaEditEvents(root) {
  root.querySelectorAll('[data-block-name-input]').forEach((inp) => {
    inp.addEventListener('click', (ev) => ev.stopPropagation());
    inp.addEventListener('input', () => {
      setBlockFormName(inp.value);
    });
  });
  root.querySelectorAll('[data-block-stage-select]').forEach((sel) => {
    sel.addEventListener('click', (ev) => ev.stopPropagation());
    const blockCode = sel.dataset.blockStageSelect || null;
    sel.addEventListener('change', () => {
      onStageRefSelectChange(sel, blockCode);
      if (sel.value !== STAGE_REF_CUSTOM) refreshBlockEditUi();
    });
  });
  root.querySelectorAll('[data-block-stage-custom]').forEach((inp) => {
    inp.addEventListener('click', (ev) => ev.stopPropagation());
    inp.addEventListener('input', () => {
      editingStageRef = inp.value;
    });
  });
  root.querySelectorAll('[data-apply-name]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      setBlockFormName(btn.dataset.applyName || '');
      refreshBlockEditUi();
    };
  });
}

function refreshBlockEditUi() {
  renderBlocks();
  paintAtomLayer();
  syncBlockFormUi();
}

function oldBlocksOnPage(pageIndex) {
  const blocks = state?.paired_old?.old_blocks || [];
  return blocks.filter((b) => {
    if (b.textbook_page_start != null && b.textbook_page_end != null) {
      return pageIndex >= b.textbook_page_start && pageIndex <= b.textbook_page_end;
    }
    const codes = b.atom_codes || [];
    const atoms = state?.paired_old?.old_atoms || [];
    return atoms.some((a) => codes.includes(a.atom_code) && a.page_index === pageIndex);
  });
}

function bboxForAtomsOnPage(atomCodes, atoms, pageIndex) {
  const onPage = (atoms || []).filter(
    (a) => (atomCodes || []).includes(a.atom_code) && a.page_index === pageIndex,
  );
  if (!onPage.length) return null;
  const xs = [];
  const ys = [];
  const xe = [];
  const ye = [];
  onPage.forEach((a) => {
    const b = a.bbox || {};
    xs.push(Number(b.x_start || 0));
    ys.push(Number(b.y_start || 0));
    xe.push(Number(b.x_end || 1));
    ye.push(Number(b.y_end || 1));
  });
  return {
    x_start: Math.min(...xs),
    y_start: Math.min(...ys),
    x_end: Math.max(...xe),
    y_end: Math.max(...ye),
  };
}

function bboxForOldBlockOnPage(block, pageIndex) {
  const box = bboxForAtomsOnPage(
    block.atom_codes,
    state?.paired_old?.old_atoms,
    pageIndex,
  );
  if (box) return box;
  const onPage = oldBlocksOnPage(pageIndex);
  const idx = onPage.findIndex((b) => b.block_code === block.block_code);
  if (idx < 0) return null;
  const n = onPage.length;
  const band = 1 / Math.max(n, 1);
  return {
    x_start: 0.02,
    y_start: idx * band + 0.008,
    x_end: 0.98,
    y_end: (idx + 1) * band - 0.008,
  };
}

function paintModuleHighlights(layer, img, items) {
  if (!layer || !img) return;
  layer.style.height = `${img.clientHeight}px`;
  layer.innerHTML = items.map((item, i) => {
    const b = item.box;
    const color = item.color || MODULE_HIGHLIGHT_COLORS[i % MODULE_HIGHLIGHT_COLORS.length];
    const cls = `module-highlight-group ${item.cls || ''}`.trim();
    return `<div class="${cls}" data-block="${item.blockCode || ''}" style="left:${b.x_start * 100}%;top:${b.y_start * 100}%;width:${(b.x_end - b.x_start) * 100}%;height:${(b.y_end - b.y_start) * 100}%;--mh-color:${color}">
      <div class="module-highlight-box"></div>
      <div class="module-highlight-label">${escHtml(item.label || '')}</div>
    </div>`;
  }).join('');
  layer.querySelectorAll('.module-highlight-group').forEach((el) => {
    const code = el.dataset.block;
    const item = items.find((x) => x.blockCode === code);
    if (item?.onClick) {
      el.onclick = (ev) => {
        ev.stopPropagation();
        item.onClick();
      };
    }
  });
}

function prescanHitsForNewPage(newPageIndex) {
  const hits = state?.content_prescan?.block_hits || [];
  return hits
    .filter((h) => Number(h.new_page_index) === Number(newPageIndex))
    .sort((a, b) => (b.match_score || 0) - (a.match_score || 0));
}

function prescanOldBlockCodesForNewPage(newPageIndex) {
  const codes = new Set();
  prescanHitsForNewPage(newPageIndex).forEach((h) => {
    if (h.old_block_code) codes.add(h.old_block_code);
  });
  return codes;
}

function prescanOldBlockCodesForCurrentNewPage() {
  return prescanOldBlockCodesForNewPage(currentPageIndex);
}

function renderOldPrescanBridge() {
  const wrap = document.getElementById('old-prescan-bridge');
  const chipsEl = document.getElementById('old-prescan-bridge-chips');
  if (!wrap || !chipsEl) return;

  const ps = state?.content_prescan;
  const show = !inBuildAnchorPhase() && ps?.status === 'done' && state?.paired_old?.old_lesson;
  if (!show) {
    wrap.hidden = true;
    return;
  }

  let hits = prescanHitsForNewPage(currentPageIndex).filter((h) => h.is_primary_lesson);
  if (!hits.length) hits = prescanHitsForNewPage(currentPageIndex).slice(0, 5);

  if (!hits.length) {
    const onPage = oldBlocksOnPage(oldCurrentPageIndex);
    if (onPage.length) {
      chipsEl.innerHTML = onPage.map((b) =>
        `<button type="button" class="old-prescan-chip old-prescan-chip--fallback" data-old-block="${escHtml(b.block_code)}">` +
        `${escHtml(b.block_code)} ${escHtml((b.block_name || '').slice(0, 12))}</button>`,
      ).join('');
      wrap.hidden = false;
      return;
    }
    chipsEl.innerHTML = '<span class="hint">本页暂无块线索，请翻页或看右侧「规则明细」</span>';
    wrap.hidden = false;
    return;
  }

  chipsEl.innerHTML = hits.map((h) => {
    const pct = Math.round((h.match_score || 0) * 100);
    const tag = h.is_primary_lesson ? '' : '跨课 ';
    const label = (h.excerpt || h.old_block_code || '').slice(0, 18);
    return (
      `<button type="button" class="old-prescan-chip${h.is_primary_lesson ? '' : ' old-prescan-chip--cross'}" ` +
      `data-old-block="${escHtml(h.old_block_code || '')}" title="${escHtml(h.excerpt || '')}">` +
      `${tag}${escHtml(h.old_block_code || '')} ${escHtml(label)} ${pct}%</button>`
    );
  }).join('');
  wrap.hidden = false;

  chipsEl.querySelectorAll('.old-prescan-chip[data-old-block]').forEach((btn) => {
    btn.onclick = () => {
      const code = btn.getAttribute('data-old-block');
      if (code) focusOldBlock(code);
    };
  });
}

function renderOldModuleHighlights() {
  const layer = document.getElementById('old-module-layer');
  const img = document.getElementById('old-tb-image');
  if (!layer || !img || img.hidden) {
    if (layer) layer.innerHTML = '';
    return;
  }
  const highlightOld = highlightOldBlockCode();
  const inFocusMode = !!highlightOld;
  const prescanCodes = inBuildAnchorPhase() ? new Set() : prescanOldBlockCodesForCurrentNewPage();
  const blocks = oldBlocksOnPage(oldCurrentPageIndex);
  const items = blocks.map((block) => {
    const box = bboxForOldBlockOnPage(block, oldCurrentPageIndex);
    if (!box) return null;
    const isActive = inFocusMode
      ? block.block_code === highlightOld
      : selectedOldBlockCode === block.block_code;
    if (inFocusMode && !isActive) return null;
    const isPrescan = prescanCodes.has(block.block_code);
    let cls = isActive ? 'active-old' : '';
    if (isPrescan && !inFocusMode) cls = `${cls} prescan-match`.trim();
    return {
      box,
      label: `${block.block_code} ${block.block_name || ''}`.trim(),
      cls,
      color: oldBlockColor(block.block_code),
      blockCode: block.block_code,
      onClick: () => {
        focusOldBlock(block.block_code);
        if (editingBlockCode && !blocksLocked()) {
          applyAnchorToEditingBlock(block.block_code);
        }
      },
    };
  }).filter(Boolean);
  if (!img.complete || !img.clientHeight) {
    img.onload = () => paintModuleHighlights(layer, img, items);
    return;
  }
  paintModuleHighlights(layer, img, items);
  syncBlockFocusChrome();
  scrollHighlightedOldBlockIntoView();
}

/** 按课内页码对齐；页数不同时按相对位置映射（勿用 PDF 绝对页码，新旧为不同 PDF） */
function resolveSyncedOldPageIndex() {
  const oldIdxList = oldTextbookPageIndexes();
  if (!oldIdxList.length) return oldCurrentPageIndex;

  if (oldIdxList.includes(currentPageIndex)) {
    return currentPageIndex;
  }

  const newIdxList = textbookPageIndexes();
  const pos = newIdxList.indexOf(currentPageIndex);
  if (pos < 0) return oldCurrentPageIndex;

  if (newIdxList.length <= 1 || oldIdxList.length <= 1) {
    return oldIdxList[Math.min(pos, oldIdxList.length - 1)];
  }

  const ratio = pos / (newIdxList.length - 1);
  const oldPos = Math.round(ratio * (oldIdxList.length - 1));
  return oldIdxList[oldPos];
}

function syncOldPageToNew() {
  if (!refOldSyncWithNew || !state?.paired_old?.old_textbook_pages?.length) return;
  oldCurrentPageIndex = resolveSyncedOldPageIndex();
}

function syncOldSyncUi() {
  const badge = document.getElementById('old-sync-badge');
  const btn = document.getElementById('old-sync-toggle-btn');
  if (badge) {
    badge.textContent = refOldSyncWithNew ? '页码同步' : '独立浏览';
    badge.classList.toggle('sync-on', refOldSyncWithNew);
    badge.title = refOldSyncWithNew
      ? '旧教材页随中间新教材页切换；点击区块后翻中间页可恢复对齐'
      : '旧教材页可单独翻页；点击恢复页码同步';
  }
  if (btn) {
    btn.textContent = refOldSyncWithNew ? '切换独立浏览' : '恢复页码同步';
    btn.title = refOldSyncWithNew
      ? '旧侧单独翻页，不再跟随新教材页'
      : '旧侧页码对齐当前新教材页';
  }
}

function refreshOldRefViewer() {
  if (!state?.paired_old?.old_lesson) return;
  syncOldSyncUi();
  renderOldTextbook();
  renderOldModuleHighlights();
  renderOldPrescanBridge();
}

function toggleOldPageSync(forceSync) {
  const next = typeof forceSync === 'boolean' ? forceSync : !refOldSyncWithNew;
  if (next === refOldSyncWithNew) {
    if (next && oldPageOverride != null) {
      oldPageOverride = null;
      syncOldPageToNew();
      toast('已恢复页码同步');
    }
    refreshOldRefViewer();
    return;
  }
  refOldSyncWithNew = next;
  oldPageOverride = null;
  if (refOldSyncWithNew) syncOldPageToNew();
  refreshOldRefViewer();
  toast(refOldSyncWithNew ? '已恢复页码同步' : '已切换为独立浏览');
}

function oldTextbookPageIndexes() {
  return (state?.paired_old?.old_textbook_pages || []).map((p) => p.page_index).sort((a, b) => a - b);
}

function currentOldTextbookPagePosition() {
  const pages = oldTextbookPageIndexes();
  const idx = pages.indexOf(oldCurrentPageIndex);
  return { pages, idx, total: pages.length };
}

function syncOldTextbookPageIndex() {
  const pages = state?.paired_old?.old_textbook_pages || [];
  if (!pages.length) return;
  const pageIndexes = new Set(pages.map((p) => p.page_index));
  if (!pageIndexes.has(oldCurrentPageIndex)) {
    oldCurrentPageIndex = pages[0].page_index;
  }
}

function gotoOldTextbookPage(delta) {
  const { pages, idx } = currentOldTextbookPagePosition();
  if (idx < 0) return;
  const newIdx = idx + delta;
  if (newIdx < 0 || newIdx >= pages.length) return;
  oldCurrentPageIndex = pages[newIdx];
  oldPageOverride = null;
  if (refOldSyncWithNew) {
    refOldSyncWithNew = false;
    toast('已切换为独立浏览');
  }
  clearBlockFocus();
  refreshOldRefViewer();
}

function renderOldTextbook() {
  const paired = state?.paired_old;
  const pages = paired?.old_textbook_pages || [];
  const shell = document.getElementById('old-tb-viewport-shell');
  const empty = document.getElementById('old-tb-empty');
  const nav = document.getElementById('old-tb-page-nav');
  const img = document.getElementById('old-tb-image');
  const info = document.getElementById('old-tb-page-info');
  const prevBtn = document.getElementById('old-tb-prev-btn');
  const nextBtn = document.getElementById('old-tb-next-btn');

  if (!pages.length) {
    if (shell) shell.hidden = true;
    if (empty) empty.hidden = false;
    if (nav) nav.hidden = true;
    if (img) img.hidden = true;
    return;
  }

  if (refOldSyncWithNew && oldPageOverride == null) {
    syncOldPageToNew();
  }
  syncOldTextbookPageIndex();
  const page = pages.find((p) => p.page_index === oldCurrentPageIndex);
  if (shell) shell.hidden = false;
  if (empty) empty.hidden = true;
  if (nav) nav.hidden = false;

  const { idx, total } = currentOldTextbookPagePosition();
  if (info) {
    const pdfNote = page?.pdf_page ? `（PDF ${page.pdf_page}）` : '';
    info.textContent = `旧教材第 ${oldCurrentPageIndex} 页${pdfNote} · ${idx + 1}/${total}`;
  }
  if (prevBtn) prevBtn.disabled = idx <= 0;
  if (nextBtn) nextBtn.disabled = idx >= total - 1;

  if (!page?.url) {
    if (img) img.hidden = true;
    return;
  }
  if (img) {
    img.hidden = false;
    img.src = page.url;
    img.onload = () => {
      renderOldModuleHighlights();
    };
  }
}

function renderOldRef() {
  const paired = state?.paired_old;
  const col = document.getElementById('old-ref-col');
  const title = document.getElementById('old-ref-title');
  const hint = document.getElementById('old-ref-hint');
  const link = document.getElementById('old-annotate-link');
  const compareLink = document.getElementById('link-compare');
  const body = document.body;
  if (!paired?.old_lesson) {
    if (col) col.hidden = true;
    if (compareLink) compareLink.hidden = !state?.pair_review?.primary_old_lesson;
    body?.classList.remove('has-old-ref');
    if (compareLink && !compareLink.hidden) {
      const vol = state?.lesson?.volume_code;
      const q = new URLSearchParams({ from: 'annotate' });
      if (vol) q.set('volume', vol);
      compareLink.href = `/new-library/lessons/${encodeURIComponent(LESSON_UID)}/compare?${q}`;
    }
    return;
  }
  body?.classList.add('has-old-ref');
  if (col) col.hidden = false;
  const ol = paired.old_lesson;
  if (compareLink) {
    compareLink.hidden = false;
    const vol = state?.lesson?.volume_code;
    const q = new URLSearchParams({ from: 'annotate' });
    if (vol) q.set('volume', vol);
    compareLink.href = `/new-library/lessons/${encodeURIComponent(LESSON_UID)}/compare?${q}`;
  }
  if (title) {
    title.textContent = `旧教材参照 · ${ol.lesson_no} ${ol.lesson_name}`;
  }
  if (hint) {
    hint.textContent = '左栏为主参照旧课的教材 PDF 页（非课件）。页码随中间新教材同步；跨课旧块请用「跨课找旧块」。';
  }
  if (link) {
    link.onclick = (ev) => {
      ev.preventDefault();
      window.location.href = ol.annotate_url || '#';
    };
  }
}

function anchorRefForBlock(block) {
  const refs = block?.metadata_json?.anchor_old_refs || [];
  return refs[0] || null;
}

function oldBlockName(code) {
  const b = (state?.paired_old?.old_blocks || []).find((x) => x.block_code === code);
  return b?.block_name || '';
}

function syncOldBlockPickUi() {
  const el = document.getElementById('old-block-pick-label');
  if (!el) return;
  if (blocksLocked()) {
    el.hidden = true;
    return;
  }
  let code = selectedOldBlockCode;
  let pending = true;
  if (!code && editingBlockCode) {
    const block = (state?.blocks || []).find((b) => b.block_code === editingBlockCode);
    const ref = anchorRefForBlock(block);
    if (ref?.old_block_code) {
      code = ref.old_block_code;
      pending = false;
    }
  }
  if (!code) {
    el.hidden = true;
    return;
  }
  const name = oldBlockName(code);
  el.hidden = false;
  el.textContent = pending
    ? `当前对照旧块：${code}${name ? ` ${name}` : ''}（保存时将写入锚定）`
    : `已对照旧块：${code}${name ? ` ${name}` : ''}（可点下方「解除锚定」取消）`;
}

async function applyAnchorToEditingBlock(oldBlockCode) {
  if (!editingBlockCode || blocksLocked()) return;
  const block = (state?.blocks || []).find((b) => b.block_code === editingBlockCode);
  if (!block) return;
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(editingBlockCode)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          block_name: block.block_name,
          atom_codes: block.atom_codes || [],
          anchor_old_block_code: oldBlockCode,
          anchor_old_page_index: oldCurrentPageIndex,
        }),
      },
    );
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '锚定失败');
    state = d;
    selectedOldBlockCode = oldBlockCode;
    renderBlocks();
    renderOldModuleHighlights();
    syncOldBlockPickUi();
    toast(`${editingBlockCode} 已对照旧块 ${oldBlockCode}`);
  } catch (e) {
    toast(e.message || String(e));
  }
}

function anchorLabelForBlock(block) {
  const ref = anchorRefForBlock(block);
  return ref?.old_block_code ? `← ${ref.old_block_code}` : '';
}

function anchorHtmlForBlock(block) {
  const ref = anchorRefForBlock(block);
  if (!ref?.old_block_code) return '';
  const name = ref.old_block_name || oldBlockName(ref.old_block_code) || '';
  const locked = blocksLocked();
  const clearBtn = locked
    ? ''
    : `<button type="button" class="btn-link block-anchor-clear" data-clear-anchor="${escHtml(block.block_code)}" title="解除与旧块的对照锚定">解除锚定</button>`;
  return `<div class="block-anchor">对照旧块 <strong>${escHtml(ref.old_block_code)}</strong> ${escHtml(name)}${clearBtn}</div>`;
}

async function clearBlockAnchor(blockCode) {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const block = (state?.blocks || []).find((b) => b.block_code === blockCode);
  if (!block || !anchorRefForBlock(block)) return;
  if (!confirm(`解除 ${blockCode} 与旧块 ${anchorRefForBlock(block).old_block_code} 的对照锚定？`)) return;
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(blockCode)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          block_name: block.block_name || '',
          stage_ref: block.stage_ref || '',
          atom_codes: block.atom_codes || [],
          clear_anchor_old: true,
        }),
      },
    );
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '解除锚定失败');
    state = d;
    if (selectedOldBlockCode === anchorRefForBlock(block)?.old_block_code) {
      selectedOldBlockCode = null;
    }
    if (focusedOldBlockCode) focusedOldBlockCode = null;
    if (editingBlockCode === blockCode) {
      renderBlocks();
      syncOldBlockPickUi();
    } else {
      renderAll();
    }
    toast(`${blockCode} 已解除旧块锚定`);
  } catch (e) {
    toast(e.message || String(e));
  }
}

function anchorTagHtml(block) {
  const ref = anchorRefForBlock(block);
  if (!ref?.old_block_code) return '';
  return `<span class="block-anchor-tag">← ${escHtml(ref.old_block_code)}</span>`;
}

function blocksOnPage(pageIndex) {
  return (state?.blocks || []).filter((b) =>
    (b.atom_codes || []).some((code) => {
      const a = (state?.atoms || []).find((x) => x.atom_code === code);
      return a && Number(a.page_index) === Number(pageIndex);
    }),
  );
}

function unanchoredBlockCount() {
  return (state?.blocks || []).filter((b) => !anchorRefForBlock(b)).length;
}

function pagesWithAtomsCount() {
  const counts = state?.page_atom_counts || {};
  return Object.values(counts).filter((n) => n > 0).length;
}

function allowsInLessonAuto() {
  const pr = state?.pair_review || {};
  return !!pr.allows_in_lesson_auto;
}

function lessonAtomsStat() {
  return state?.lesson_atoms_status || {
    atoms_ready: (state?.stats?.atom_count || 0) > 0,
    curate_ready: false,
  };
}

function segStatusClass(status) {
  if (status === 'running') return 'is-running';
  if (status === 'done') return 'is-done';
  if (status === 'skip') return 'is-skip';
  if (status === 'error') return 'is-error';
  return 'is-pending';
}

function annotateSegColumn(key, name, status) {
  const cls = segStatusClass(status);
  return (
    `<div class="annotate-segcol">` +
    `<span class="annotate-seg seg-${key} ${cls}"></span>` +
    `<span class="annotate-seg-name ${cls}">${escHtml(name)}</span>` +
    `</div>`
  );
}

function stripStepNumber(name) {
  return String(name || '').replace(/^[①②③④⑤⑥⑦⑧⑨]\s*/, '');
}

function analysisProgressSegments() {
  if (state?._analysisOptimisticSegments) {
    return { ...state._analysisOptimisticSegments };
  }
  return normalizeAnalysisSegments(state?.analysis || {});
}

function analysisSegmentsDoneCount(segments) {
  return ANALYSIS_SEGS.filter((s) => {
    const st = segments[s.key];
    return st === 'done' || st === 'skip';
  }).length;
}

function blockSegmentsDoneCount(segments) {
  return BLOCK_PIPELINE_STEPS.filter((s) => {
    const st = segments[s.key];
    return st === 'done' || st === 'skip';
  }).length;
}

function blockPipelineHasProgress() {
  const segs = blockPipelineSegments();
  return BLOCK_PIPELINE_STEPS.some((s) => {
    const st = segs[s.key];
    return st === 'done' || st === 'skip' || st === 'running' || st === 'error';
  });
}

function shouldShowBlockPipelineLane() {
  const a = state?.analysis || {};
  const bc = state?.stats?.block_count || 0;
  return !!(
    state?._blockPipelineBusy
    || a.ready_for_blocks
    || bc > 0
    || blockPipelineHasProgress()
  );
}

function updatePipelineLaneToggle(lane, toggleBtn, summaryEl, collapsed, busy, summaryText) {
  if (!lane) return;
  lane.classList.toggle('is-collapsed', collapsed);
  lane.classList.toggle('is-expanded', !collapsed);
  lane.classList.toggle('is-busy', busy);
  const stepsEl = lane.querySelector('.progress-steps');
  if (stepsEl) stepsEl.hidden = collapsed;
  if (summaryEl) summaryEl.textContent = summaryText;
  if (toggleBtn) {
    toggleBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    toggleBtn.title = collapsed ? toggleBtn.dataset.expandTitle || '展开' : toggleBtn.dataset.collapseTitle || '收起';
  }
}

function renderAnalysisPipelineCollapse() {
  const lane = document.getElementById('analysis-pipeline-lane');
  const toggleBtn = document.getElementById('analysis-pipeline-collapse');
  const summaryEl = document.getElementById('analysis-pipeline-summary');
  if (!lane) return;
  const busy = !!state?._analysisBusy;
  const collapsed = analysisPipelineCollapsed && !busy;
  const segs = analysisProgressSegments();
  const done = analysisSegmentsDoneCount(segs);
  if (toggleBtn) {
    toggleBtn.dataset.expandTitle = '展开对照六步';
    toggleBtn.dataset.collapseTitle = '收起对照六步';
  }
  updatePipelineLaneToggle(
    lane,
    toggleBtn,
    summaryEl,
    collapsed,
    busy,
    `${done}/${ANALYSIS_SEGS.length}`,
  );
}

function renderBlockPipelineCollapse() {
  const lane = document.getElementById('block-pipeline-lane');
  const toggleBtn = document.getElementById('block-pipeline-collapse');
  const summaryEl = document.getElementById('block-pipeline-summary');
  if (!lane) return;
  const busy = !!state?._blockPipelineBusy;
  const collapsed = blockPipelineCollapsed && !busy;
  const segs = blockPipelineSegments();
  const done = blockSegmentsDoneCount(segs);
  if (toggleBtn) {
    toggleBtn.dataset.expandTitle = '展开建块七步';
    toggleBtn.dataset.collapseTitle = '收起建块七步';
  }
  updatePipelineLaneToggle(
    lane,
    toggleBtn,
    summaryEl,
    collapsed,
    busy,
    `${done}/${BLOCK_PIPELINE_STEPS.length}`,
  );
}

function findCurrentProgressIndex(segments, steps) {
  for (let i = 0; i < steps.length; i++) {
    if (segments[steps[i].key] === 'running') return i;
  }
  for (let i = 0; i < steps.length; i++) {
    const st = segments[steps[i].key];
    if (st !== 'done' && st !== 'skip' && st !== 'error') return i;
  }
  return Math.max(0, steps.length - 1);
}

function progressStepsMaxVisible() {
  return PIPELINE_STEP_SLOTS;
}

function renderProgressSteps(el, segments, steps, { slotCount = PIPELINE_STEP_SLOTS } = {}) {
  if (!el) return;

  const currentIdx = findCurrentProgressIndex(segments, steps);
  let html = `<div class="progress-steps-inner" style="--pipeline-step-count:${slotCount}">`;
  let prevStepIndex = null;

  steps.forEach((step, idx) => {
    const segSt = segments[step.key] || 'pending';
    const uiStatus = resolveStepUiStatus(segSt, idx, currentIdx);
    const isCurrent = uiStatus === 'current';

    if (prevStepIndex != null) {
      const leftSt = segments[steps[prevStepIndex].key] || 'pending';
      html += `<div class="progress-step-connector ${progressConnectorClass(leftSt, segSt)}" aria-hidden="true"></div>`;
    }

    html += renderProgressStepNode(step, uiStatus, {
      clickable: false,
      isCurrent,
      segStatus: segSt,
      errorMsg: state?.analysis?.error,
    });
    prevStepIndex = idx;
  });

  const padCount = Math.max(0, slotCount - steps.length);
  for (let p = 0; p < padCount; p++) {
    if (prevStepIndex != null) {
      html += '<div class="progress-step-connector is-finished" aria-hidden="true"></div>';
    }
    html += (
      '<div class="progress-step-unit progress-step-unit--pad" aria-hidden="true">' +
      '<div class="progress-step-node-wrap"><div class="progress-step-node progress-step-node--pad"></div></div>' +
      '</div>'
    );
    prevStepIndex = steps.length + p;
  }

  html += '</div>';
  el.innerHTML = html;
}

function resolveStepUiStatus(segStatus, index, currentIdx) {
  if (segStatus === 'error') return 'failed';
  if (segStatus === 'skip') return 'skipped';
  if (segStatus === 'running') return 'current';
  if (segStatus === 'done') return 'success';
  if (index === currentIdx) return 'current';
  return 'pending';
}

function progressConnectorClass(leftSegStatus, rightSegStatus) {
  if (leftSegStatus === 'error') return 'is-failed';
  if (leftSegStatus === 'running') return 'is-partial';
  if (leftSegStatus === 'done') return 'is-finished';
  if (leftSegStatus === 'skip') return 'is-finished';
  if (rightSegStatus === 'running') return 'is-partial';
  return 'is-pending';
}

function renderProgressStepNode(step, uiStatus, { clickable, isCurrent, segStatus, errorMsg }) {
  const label = stripStepNumber(step.name);
  let nodeInner = '';
  if (uiStatus === 'success') {
    nodeInner = '<span class="progress-step-check" aria-hidden="true">✓</span>';
  } else if (uiStatus === 'failed') {
    nodeInner = '<span class="progress-step-cross" aria-hidden="true">✕</span>';
  } else if (uiStatus === 'skipped') {
    nodeInner = '<span class="progress-step-dash" aria-hidden="true">−</span>';
  } else if (uiStatus === 'current') {
    nodeInner = '<span class="progress-step-dot" aria-hidden="true"></span>';
  }
  const clickCls = clickable ? ' is-clickable' : '';
  const currentAttr = isCurrent ? ' aria-current="step"' : '';
  const title = errorMsg && uiStatus === 'failed' ? `${step.name}：${errorMsg}` : step.name;
  return (
    `<div class="progress-step-unit" role="listitem">` +
    `<div class="progress-step-node-wrap${clickCls}" data-step="${escHtml(step.key)}"` +
    `${currentAttr} aria-label="${escHtml(step.name)}" title="${escHtml(title)}"` +
    ` tabindex="${clickable ? '0' : '-1'}">` +
    `<div class="progress-step-node progress-step-node--${uiStatus}">${nodeInner}</div>` +
    `<span class="progress-step-label progress-step-label--${uiStatus}">${escHtml(label)}</span>` +
    `</div></div>`
  );
}

function renderProgressFoldNode(hiddenCount, hiddenNames, expanded) {
  const tip = expanded
    ? '点击收起中间步骤'
    : `还有 ${hiddenCount} 个步骤：${hiddenNames.join(' · ')}`;
  const icon = expanded
    ? '<span class="progress-step-fold-minus" aria-hidden="true">−</span>'
    : '<span class="progress-step-fold-dots" aria-hidden="true"><i></i><i></i><i></i></span>';
  const foldLabel = expanded ? '收起' : '更多';
  return (
    `<div class="progress-step-unit progress-step-fold-unit" role="listitem">` +
    `<button type="button" class="progress-step-node-wrap progress-step-fold-btn${expanded ? ' is-expanded' : ''}"` +
    ` title="${escHtml(tip)}" aria-label="${escHtml(tip)}" aria-expanded="${expanded ? 'true' : 'false'}">` +
    `<div class="progress-step-node progress-step-node--fold">${icon}</div>` +
    `<span class="progress-step-label progress-step-label--fold">${foldLabel}</span>` +
    `</button></div>`
  );
}

function onProgressStepClick(stepKey) {
  const adv = document.querySelector('.annotate-advanced-steps');
  if (adv && !adv.open) adv.open = true;
  const btnId = PROGRESS_STEP_ACTIONS[stepKey];
  if (btnId) {
    const btn = document.getElementById(btnId);
    if (btn) {
      btn.focus();
      btn.classList.add('progress-step-flash');
      setTimeout(() => btn.classList.remove('progress-step-flash'), 800);
    }
    return;
  }
  if (stepKey === 'pair_review' && !inBuildAnchorPhase()) {
    document.getElementById('analysis-verdict-bar')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }
}

function renderPipeTrack(el, segments, steps) {
  renderProgressSteps(el, segments, steps);
}

function renderSegTrack(el, segments, steps, { barsOnly = true } = {}) {
  if (!el) return;
  if (!barsOnly) {
    renderPipeTrack(el, segments, steps);
    return;
  }
  el.classList.toggle('annotate-seg-track--bars-only', barsOnly);
  el.classList.toggle('annotate-seg-track--labeled', !barsOnly);
  if (barsOnly) {
    el.innerHTML = steps.map((s) => {
      const cls = segStatusClass(segments[s.key] || 'pending');
      return `<span class="annotate-seg seg-${s.key} ${cls}" title="${escHtml(s.name)}"></span>`;
    }).join('');
    return;
  }
  el.innerHTML = steps.map((s) => annotateSegColumn(s.key, s.name, segments[s.key] || 'pending')).join('');
}

function normalizeAnalysisSegments(a) {
  const base = {
    text_ocr: 'pending',
    image_ocr: 'pending',
    curate: 'pending',
    block_match: 'pending',
    prescan: 'pending',
    pair_review: 'pending',
    ...(a?.segments || {}),
  };
  const stage = a?.stage || '';
  if (stage === 'ready') {
    base.pair_review = base.pair_review === 'skip' ? 'skip' : 'done';
  }
  return base;
}

function inBuildAnchorPhase() {
  if (blocksLocked()) return true;
  if (state?._buildPhaseActive) return true;
  return (state?.stats?.block_count || 0) > 0;
}

function renderRightColumnMode() {
  const analysisPanel = document.getElementById('analysis-right-panel');
  const blocksPanel = document.getElementById('blocks-work-panel');
  const showBlocks = inBuildAnchorPhase();
  if (analysisPanel) {
    analysisPanel.hidden = showBlocks;
    analysisPanel.classList.toggle('is-panel-active', !showBlocks);
  }
  if (blocksPanel) {
    blocksPanel.hidden = !showBlocks;
    blocksPanel.classList.toggle('is-panel-active', showBlocks);
    if (showBlocks) {
      const dualTrack = document.getElementById('dual-track-panel');
      if (dualTrack?.open && !state?._dualTrackPanelOpen) dualTrack.open = false;
    }
  }
  document.body.classList.toggle('annotate-phase-analysis', !showBlocks);
  document.body.classList.toggle('annotate-phase-build', showBlocks);
}

function deriveBuildAnchorSegments() {
  const stats = state?.stats || {};
  const bc = stats.block_count || 0;
  const unanchored = unanchoredBlockCount();
  const fullyNew = state?.pair_review?.status === 'no_old';
  const locked = blocksLocked();
  let build = 'pending';
  if (bc > 0) build = 'done';
  let anchor = 'pending';
  if (fullyNew || !state?.paired_old?.old_blocks?.length) anchor = 'skip';
  else if (bc > 0 && unanchored <= 0) anchor = 'done';
  else if (bc > 0 && unanchored < bc) anchor = 'running';
  let lock = 'pending';
  if (locked) lock = 'done';
  else if (bc > 0 && (anchor === 'done' || anchor === 'skip')) lock = 'running';
  return { build, anchor, lock };
}

function emptyBlockPipelineSegments() {
  const segs = {};
  BLOCK_PIPELINE_STEPS.forEach((s) => { segs[s.key] = 'pending'; });
  return segs;
}

function blockPipelineSegments() {
  if (state?._blockPipelineJob?.segments) {
    return { ...emptyBlockPipelineSegments(), ...state._blockPipelineJob.segments };
  }
  if (state?.block_pipeline?.segments) {
    return { ...emptyBlockPipelineSegments(), ...state.block_pipeline.segments };
  }
  return emptyBlockPipelineSegments();
}

function blockingSummaryData() {
  return state?.block_pipeline?.summary
    || state?._blockPipelineJob?.summary
    || null;
}

function renderBlockingSummaryCard() {
  const el = document.getElementById('blocking-summary-card');
  if (!el) return;
  const summary = blockingSummaryData();
  const show = !!state?._showBlockingSummary && summary && !state?._blockPipelineBusy;
  if (!show) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  const dist = summary.match_distribution || {};
  const pathLabel = BLOCK_PATH_LABELS[summary.source_path] || summary.source_path || '—';
  const suspicious = summary.suspicious_blocks || [];
  el.hidden = false;
  el.innerHTML = `
    <div class="blocking-summary-head">
      <span class="blocking-summary-icon" aria-hidden="true">✅</span>
      <strong class="blocking-summary-title">建块完成</strong>
    </div>
    <p class="blocking-summary-meta">共生成 ${summary.total_blocks || 0} 个区块 · 路径：${escHtml(pathLabel)}</p>
    <div class="blocking-summary-grid">
      <div class="blocking-summary-stat blocking-summary-stat--full">完全匹配 <strong>${dist.full || 0}</strong></div>
      <div class="blocking-summary-stat blocking-summary-stat--partial">部分匹配 <strong>${dist.partial || 0}</strong></div>
      <div class="blocking-summary-stat blocking-summary-stat--new">新增区块 <strong>${dist.new || 0}</strong></div>
      <div class="blocking-summary-stat blocking-summary-stat--warn">存疑 <strong>${summary.suspicious_count || 0}</strong>${(summary.suspicious_count || 0) > 0 ? ' ⚠' : ''}</div>
    </div>
    ${suspicious.length ? `<button type="button" class="blocking-summary-link" id="blocking-summary-suspicious-btn">查看存疑区块 →</button>` : ''}
    <button type="button" class="blocking-summary-primary" id="blocking-summary-edit-btn">开始编辑</button>
  `;
  document.getElementById('blocking-summary-suspicious-btn')?.addEventListener('click', () => {
    focusFirstSuspiciousBlock(suspicious);
  });
  document.getElementById('blocking-summary-edit-btn')?.addEventListener('click', () => {
    state = { ...state, _showBlockingSummary: false };
    renderBlockingSummaryCard();
    scrollBlocksResultsIntoView();
  });
}

function focusFirstSuspiciousBlock(suspiciousBlocks) {
  const first = (suspiciousBlocks || [])[0];
  if (!first?.block_code) return;
  const block = (state?.blocks || []).find((b) => b.block_code === first.block_code);
  if (block) focusBlock(block);
  const row = document.querySelector(`#blocks-list [data-block-code="${first.block_code}"]`);
  if (row) {
    row.classList.add('block-suspicious-focus');
    row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    setTimeout(() => row.classList.remove('block-suspicious-focus'), 2400);
  }
}

function stopBlockPipelinePoll() {
  if (blockPipelinePollTimer) {
    clearTimeout(blockPipelinePollTimer);
    blockPipelinePollTimer = null;
  }
}

function renderAnnotatePipelines() {
  const a = state?.analysis || {};
  const analysisSegments = analysisProgressSegments();
  renderProgressSteps(
    document.getElementById('analysis-progress-steps'),
    analysisSegments,
    ANALYSIS_SEGS,
  );
  renderAnalysisPipelineCollapse();

  const blockLane = document.getElementById('block-pipeline-lane');
  const showBlockLane = shouldShowBlockPipelineLane();
  if (blockLane) blockLane.hidden = !showBlockLane;
  if (showBlockLane) {
    renderProgressSteps(
      document.getElementById('block-progress-steps'),
      blockPipelineSegments(),
      BLOCK_PIPELINE_STEPS,
    );
    renderBlockPipelineCollapse();
  }

  const statusLine = document.getElementById('pipeline-status-line');
  if (statusLine) {
    const ps = state?.content_prescan;
    const stats = state?.stats || {};
    const bc = stats.block_count || 0;
    const unanchored = unanchoredBlockCount();
    let analysisPart = state?._analysisBusy
      ? '对照分析进行中…'
      : (a.label || '待对照分析');
    if (!state?._analysisBusy && ps?.status === 'done') {
      const ai = prescanDisplayLabel(ps);
      analysisPart = [analysisPart, ai].filter(Boolean).join(' · ');
    }
    const buildSegs = deriveBuildAnchorSegments();
    let buildPart = '待建块';
    if (state?._blockPipelineBusy) buildPart = '建块流水线运行中…';
    else if (blocksLocked()) buildPart = '已锁定';
    else if (bc > 0) {
      buildPart = unanchored > 0 ? `建块 ${bc} · ${unanchored} 待锚定` : `建块 ${bc} · 已锚定`;
    } else if (buildSegs.build === 'running' || inBuildAnchorPhase()) {
      buildPart = '建块锚定中';
    }
    statusLine.textContent = `${analysisPart} | ${buildPart}`;
    statusLine.title = statusLine.textContent;
  }

  const locked = blocksLocked();
  const analysisBtn = document.getElementById('run-analysis-btn');
  const buildBtn = document.getElementById('run-build-anchor-btn');
  const analysisBusy = !!state?._analysisBusy;
  if (analysisBtn) {
    analysisBtn.disabled = locked || analysisBusy;
    analysisBtn.textContent = analysisBusy ? '分析中…' : (a.stage === 'ready' ? '重新对照分析' : '运行对照分析');
  }
  if (buildBtn) {
    buildBtn.disabled = locked || analysisBusy || !!state?._blockPipelineBusy || !a.ready_for_blocks;
    buildBtn.textContent = state?._blockPipelineBusy ? '建块中…' : '运行建块与锚定';
    buildBtn.title = a.ready_for_blocks
      ? '整课 7 步：认栏目头→分组→合并→对照建块→挂锚→查锚→核对'
      : '须先完成对照分析（含确认对照）';
  }
}

function scheduleProgressStepsResize() {
  if (progressStepsResizeTimer) clearTimeout(progressStepsResizeTimer);
  progressStepsResizeTimer = setTimeout(() => {
    progressStepsResizeTimer = null;
    renderAnnotatePipelines();
  }, 150);
}

async function runLessonAnalysis() {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  if (!confirm('对本课重新运行对照分析？\n\n将执行：文字OCR → 图片OCR → 整理原子 → 栏目·块名 → 对照预判。\n不会自动重建区块；完成后请再点「运行建块与锚定」。\n可能耗时数分钟。')) return;
  analysisPipelineCollapsed = false;
  state = { ...state, ...beginAnalysisRerunState() };
  renderAnnotatePipelines();
  toast('对照分析已开始，步骤已重置…');
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/analysis`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '对照分析失败');
    state = { ...d, _analysisBusy: false };
    delete state._analysisOptimisticSegments;
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    const applied = d.analysis_applied_pair_review;
    const warnN = (d.analysis_warnings || []).length;
    if (applied) {
      toast(`对照分析完成，已自动确认（${applied}）`);
    } else if (warnN) {
      toast(`对照分析完成（${warnN} 条提示），请点确认/改动大/无对应`);
    } else {
      toast('对照分析完成，请确认对照结论');
    }
  } catch (e) {
    clearAnalysisOptimisticState();
    state = { ...state, _analysisBusy: false };
    renderAnnotatePipelines();
    toast(e.message || String(e));
    try {
      const rr = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/annotate`);
      const rd = await readJson(rr);
      if (rr.ok && rd.ok) {
        state = { ...state, ...rd, _analysisBusy: false };
        renderAll();
      }
    } catch {
      /* 保留当前界面 */
    }
  } finally {
    analysisPipelineCollapsed = true;
    renderAnalysisPipelineCollapse();
  }
}

async function runLessonBuildAnchor() {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  if (!state?.analysis?.ready_for_blocks) {
    toast('请先完成对照分析（含确认对照）');
    return;
  }
  if (!state?.paired_old?.old_blocks?.length) {
    toast('无粗分旧课区块，请手动勾选原子建块');
    return;
  }
  let pathHint = '';
  try {
    const pathR = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/block-pipeline/path`);
    const pathD = await readJson(pathR);
    if (pathR.ok && pathD.path) {
      pathHint = pathD.path === 'old_mirror'
        ? '将走旧块镜像（①–③ 栏目分析 → ④豆包建块 → ⑤豆包锚定）'
        : `将走原子分组（${pathD.reason || '无旧课参照'}；先认栏目头再按组建块）`;
    }
  } catch {
    pathHint = '';
  }
  const confirmMsg = pathHint
    ? `整课建块与锚定（7 步流水线）\n${pathHint}\n继续？`
    : '整课建块与锚定（7 步流水线）\n继续？';
  if (!confirm(confirmMsg)) return;

  stopBlockPipelinePoll();
  blockPipelineCollapsed = false;
  state = {
    ...state,
    _buildPhaseActive: true,
    _blockPipelineBusy: true,
    _blockPipelineJob: null,
    _showBlockingSummary: false,
  };
  renderRightColumnMode();
  renderAnnotatePipelines();
  renderBlockingSummaryCard();
  const btn = document.getElementById('run-build-anchor-btn');
  if (btn) btn.disabled = true;

  try {
    const runR = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/block-pipeline/run`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const runD = await readJson(runR);
    if (!runR.ok || !runD.ok) throw new Error(runD.error || '启动建块失败');
    state = { ...state, _blockPipelineJob: runD.job };
    renderAnnotatePipelines();
    const job = await pollBlockPipelineJob();
    await bulkSuggestAllUnanchored(true);
    const reloadR = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/annotate`);
    const reloadD = await readJson(reloadR);
    if (reloadR.ok && reloadD.ok) {
      state = {
        ...reloadD,
        _blockPipelineJob: job,
        _buildPhaseActive: true,
        _showBlockingSummary: true,
        block_pipeline: reloadD.block_pipeline || {
          segments: job.segments,
          summary: job.summary,
          path_info: job.path_info,
          state: 'done',
        },
      };
      renderAll();
      scrollBlocksResultsIntoView();
    }
    const total = job?.summary?.total_blocks || job?.created_total || 0;
    toast(`建块与锚定完成：${total} 个区块`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    stopBlockPipelinePoll();
    blockPipelineCollapsed = true;
    state = { ...state, _blockPipelineBusy: false };
    if (btn) btn.disabled = blocksLocked() || !state?.analysis?.ready_for_blocks;
    renderAnnotatePipelines();
  }
}

function pollBlockPipelineJob() {
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/block-pipeline/job`);
        const d = await readJson(r);
        if (!r.ok || !d.ok) throw new Error(d.error || '轮询失败');
        const job = d.job;
        if (!job) {
          reject(new Error('无建块任务'));
          return;
        }
        state = { ...state, _blockPipelineJob: job };
        renderAnnotatePipelines();
        if (job.state === 'done') {
          if (d.workspace) {
            state = {
              ...d.workspace,
              _blockPipelineBusy: false,
              _blockPipelineJob: job,
              _buildPhaseActive: true,
              _showBlockingSummary: true,
            };
          } else {
            state = {
              ...state,
              _blockPipelineBusy: false,
              _showBlockingSummary: true,
              block_pipeline: {
                ...(state.block_pipeline || {}),
                segments: job.segments,
                summary: job.summary,
                path_info: job.path_info,
                state: 'done',
              },
            };
          }
          renderAll();
          resolve(job);
          return;
        }
        if (job.state === 'error') {
          state = { ...state, _blockPipelineBusy: false };
          renderAll();
          reject(new Error(job.error || job.label || '建块失败'));
          return;
        }
        blockPipelinePollTimer = setTimeout(poll, 400);
      } catch (err) {
        state = { ...state, _blockPipelineBusy: false };
        renderAll();
        reject(err);
      }
    };
    poll();
  });
}

function pairReviewSuggestLabel(status) {
  return {
    confirmed: '确认对照',
    heavy_change: '同课改动大',
    no_old: '无对应/配错',
    swap_primary: '换主课',
  }[status] || '';
}

function prescanLessonRole(scoreRow, candidates) {
  if (scoreRow.is_primary) return '粗分主课';
  const cand = (candidates || []).find((c) => c.lesson_id === scoreRow.old_lesson_id);
  if (cand?.match_rank) return `粗分 rank${cand.match_rank}`;
  return '结构邻居';
}

function renderPairReview() {
  const pr = state?.pair_review;
  if (!pr) return;

  const hasPrimary = !!pr.primary_old_lesson;
  const confirmGroup = document.getElementById('step-group-confirm');
  const prescanBtn = document.getElementById('prescan-suggest-btn');
  const ps = state?.content_prescan;
  const needsConfirm = hasPrimary && pr.status === 'pending'
    && (ps?.status === 'done' || state?.analysis?.stage === 'await_confirm' || state?.analysis?.stage === 'needs_review');

  if (prescanBtn) prescanBtn.hidden = !hasPrimary;
  if (confirmGroup) {
    const hideForRightPanel = !inBuildAnchorPhase() && ps?.status === 'done';
    confirmGroup.hidden = !needsConfirm || hideForRightPanel;
    confirmGroup.classList.remove('is-current', 'is-done', 'workflow-next-btn');
    confirmGroup.querySelectorAll('.pair-review-btn').forEach((btn) => {
      btn.classList.remove('pair-review-suggested', 'workflow-next-btn');
      btn.disabled = !needsConfirm;
      const suggested = ps?.suggested_pair_status;
      if (needsConfirm && suggested && btn.dataset.pairStatus === suggested) {
        btn.classList.add('pair-review-suggested');
      }
    });
  }

  const verdictEl = document.getElementById('pair-review-verdict');
  if (verdictEl) {
    const aiLabel = ps?.status === 'done' ? prescanDisplayLabel(ps) : '';
    if (hasPrimary && aiLabel) {
      verdictEl.hidden = false;
      if (pr.status === 'pending') {
        const hideForRightPanel = !inBuildAnchorPhase() && ps?.status === 'done';
        verdictEl.textContent = hideForRightPanel
          ? `AI 建议：${aiLabel}（见右侧「一键确认」）`
          : `AI 建议：${aiLabel}（请点对应按钮确认）`;
      } else {
        verdictEl.textContent = `AI 建议：${aiLabel} · 已选：${pr.status_label || pr.status}`;
      }
    } else {
      verdictEl.hidden = true;
      verdictEl.textContent = '';
    }
  }

  const badge = document.getElementById('pair-review-status-badge');
  if (badge) {
    if (hasPrimary) {
      badge.hidden = false;
      badge.textContent = pr.status === 'pending' ? '待确认对照' : (pr.status_label || '');
      badge.className = `pair-review-status-badge status-${pr.status || 'pending'}`;
    } else {
      badge.hidden = true;
    }
  }

  const swap = document.getElementById('pair-review-swap');
  const sel = document.getElementById('primary-old-select');
  const candidates = (pr.candidates || []).filter((c) =>
    ['exact', 'high_similarity', 'traceability'].includes(c.match_tier),
  );
  const suggestSwap = ps?.suggested_pair_status === 'swap_primary'
    || ps?.coarse_agreement === 'suggest_swap';
  if (swap && sel) {
    swap.hidden = !hasPrimary || candidates.length <= 1 || !suggestSwap;
    sel.innerHTML = candidates.map((c) => {
      const label = `${c.lesson_no || ''} ${c.lesson_name || ''} · ${c.match_label || ''}`.trim();
      const selAttr = c.annotate_primary ? ' selected' : '';
      return `<option value="${escHtml(c.lesson_uid)}"${selAttr}>${escHtml(label)}</option>`;
    }).join('');
  }
}

function prescanOcrStat() {
  return state?.prescan_ocr || {};
}

function prescanDisplayLabel(ps) {
  if (!ps) return '';
  const llm = ps.result_json?.llm_prescan;
  if (llm?.pair_status) {
    const base = pairReviewSuggestLabel(llm.pair_status);
    if (llm.change_level === 'minor' && llm.pair_status === 'confirmed') {
      return `${base}（换词微调）`;
    }
    return base;
  }
  return pairReviewSuggestLabel(ps.suggested_pair_status)
    || ps.suggested_pair_status_label
    || ps.coarse_agreement_label
    || '';
}

let catalogFocusAtomCode = null;
let atomCatalogFilter = '';
let atomCatalogSearch = '';

const ATOM_TYPE_ICON = {
  text: '📝',
  title: '🏷️',
  image: '🖼️',
};

function atomTypeLabel(type) {
  return { text: '文本', title: '标题', image: '图像' }[type] || type || '其他';
}

function pageAtomTypeCounts(pageIndex) {
  const counts = { text: 0, title: 0, image: 0, other: 0 };
  atomsOnPage(pageIndex).forEach((a) => {
    const t = a.atom_type || 'other';
    if (counts[t] != null) counts[t] += 1;
    else counts.other += 1;
  });
  return counts;
}

function pageStageHint(pageIndex) {
  const stages = new Set();
  for (const b of state?.blocks || []) {
    if (!(b.atom_codes || []).some((c) => {
      const a = (state?.atoms || []).find((x) => x.atom_code === c);
      return a && a.page_index === pageIndex;
    })) continue;
    const s = (b.stage_ref || '').trim();
    if (s) stages.add(s);
  }
  if (!stages.size) return '尚未分栏（建块后显示模块）';
  return Array.from(stages).slice(0, 2).join('、');
}

function prescanMatchScoreDisplay(ps) {
  if (!ps) return { pct: null, tone: 'pending', label: '待分析' };
  const llm = ps.result_json?.llm_prescan;
  if (llm?.pair_status === 'confirmed') {
    return { pct: null, tone: 'good', label: 'AI：建议确认对照' };
  }
  if (llm?.pair_status === 'heavy_change') {
    return { pct: null, tone: 'warn', label: 'AI：同课改动大' };
  }
  if (llm?.pair_status === 'no_old') {
    return { pct: null, tone: 'bad', label: 'AI：无对应' };
  }
  const primary = (ps.result_json?.lesson_scores || []).find((s) => s.is_primary);
  const pct = primary
    ? Math.round((primary.score || 0) * 100)
    : (ps.lesson_body_similarity != null ? Math.round(ps.lesson_body_similarity * 100) : null);
  const suggest = prescanDisplayLabel(ps);
  if (pct != null && pct >= 38) return { pct, tone: 'good', label: suggest || '建议确认' };
  if (pct != null && pct <= 22) return { pct, tone: 'bad', label: suggest || '匹配偏低' };
  return { pct, tone: 'warn', label: suggest || '建议人工复核' };
}

function focusAtomFromCatalog(atomCode) {
  if (!atomCode || blocksLocked()) return;
  const owner = atomOwnerBlock(atomCode);
  if (owner && !editingBlockCode) {
    focusBlock(owner);
    return;
  }
  catalogFocusAtomCode = atomCode;
  selectedAtoms.clear();
  selectedAtoms.add(atomCode);
  paintAtomLayer();
  syncAtomActionButtons();
  renderAtomCatalog();
  scrollCatalogAtomIntoView(atomCode);
}

function scrollCatalogAtomIntoView(atomCode) {
  requestAnimationFrame(() => {
    const layer = document.getElementById('atom-layer');
    const viewport = document.getElementById('tb-viewport');
    const box = layer?.querySelector(`.atom-box[data-atom="${atomCode}"]`);
    if (box && viewport) {
      const vRect = viewport.getBoundingClientRect();
      const bRect = box.getBoundingClientRect();
      viewport.scrollTo({
        top: Math.max(0, bRect.top - vRect.top + viewport.scrollTop - viewport.clientHeight * 0.25),
        left: Math.max(0, bRect.left - vRect.left + viewport.scrollLeft - viewport.clientWidth * 0.15),
        behavior: 'smooth',
      });
    }
    const row = document.querySelector(`#atom-catalog-list [data-atom-code="${atomCode}"]`);
    row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
}

function buildMatchReferenceHtml(ps) {
  if (!ps || ps.status === 'pending') {
    return '<p class="hint">运行顶部「对照分析」后显示 Top3 候选课。</p>';
  }
  if (ps.status === 'failed') {
    return `<p class="hint">${escHtml(ps.summary_text || '对照预判失败')}</p>`;
  }

  const candidates = state?.pair_review?.candidates || [];
  const scores = (ps.result_json?.lesson_scores || []).slice(0, 3);
  const hits = ps.block_hits || [];
  const llmReason = ps.result_json?.llm_prescan?.reason || '';

  const medals = ['🥇', '🥈', '🥉'];
  const cards = scores.map((s, i) => {
    const pct = Math.round((s.score || 0) * 100);
    const name = `${s.lesson_no || ''} ${s.lesson_name || ''}`.trim();
    const role = prescanLessonRole(s, candidates);
    const overlap = i === 0 && llmReason
      ? llmReason.slice(0, 56)
      : (role === '粗分主课' ? '粗分主参照课' : role);
    return (
      `<div class="match-ref-card${i === 0 ? ' match-ref-card--top' : ''}">` +
      `<div class="match-ref-card-head"><span>${medals[i] || ''}</span> ${escHtml(name)} ` +
      `<span class="match-ref-pct">${pct}%</span></div>` +
      `<div class="match-ref-overlap hint">${escHtml(overlap)}</div></div>`
    );
  }).join('');

  const hitListCompact = hits.slice(0, 10).map((h) => {
    const tag = h.is_primary_lesson ? '' : '跨课 ';
    return `<li>${tag}${escHtml(h.old_block_code || '')} · 新p${h.new_page_index ?? '?'} ${Math.round((h.match_score || 0) * 100)}%</li>`;
  }).join('');

  return (
    `${cards || '<p class="hint">无候选课得分</p>'}` +
    `<details class="prescan-details-fold"><summary>规则明细 · 共 ${hits.length} 条块命中</summary>` +
    `<ul class="content-prescan-hits">${hitListCompact}${hits.length > 10 ? `<li class="hint">… 共 ${hits.length} 条</li>` : ''}</ul></details>`
  );
}

function renderAnalysisVerdictBar() {
  const el = document.getElementById('analysis-verdict-bar');
  if (!el) return;
  const ps = state?.content_prescan;
  const pr = state?.pair_review || {};
  const locked = blocksLocked();
  const score = prescanMatchScoreDisplay(ps);
  const needsConfirm = pr.primary_old_lesson && pr.status === 'pending'
    && ps?.status === 'done';

  const pctLine = score.pct != null
    ? `<span class="analysis-score-pct">${score.pct}%</span> <span class="analysis-score-note">规则参考</span>`
    : '';

  el.className = `analysis-panel-block analysis-verdict-bar tone-${score.tone}`;
  el.innerHTML = `
    <div class="analysis-verdict-top">
      <strong>对照预判</strong>
      ${pctLine}
      <span class="analysis-verdict-label">${escHtml(score.label)}</span>
    </div>
    ${pr.status !== 'pending' ? `<p class="hint analysis-verdict-done">已选：${escHtml(pr.status_label || pr.status)}</p>` : ''}
    ${needsConfirm && !locked ? `
      <div class="analysis-verdict-actions">
        <button type="button" class="btn btn-sm analysis-btn-confirm pair-review-btn" data-pair-status="confirmed">一键确认</button>
        <button type="button" class="btn btn-sm analysis-btn-doubt pair-review-btn" data-pair-status="heavy_change">标记存疑</button>
      </div>
      <p class="hint">存疑=同课改动大；配错请用顶部高级区「无对应」</p>
    ` : ''}
  `;

  el.querySelectorAll('.pair-review-btn').forEach((btn) => {
    const suggested = ps?.suggested_pair_status || ps?.result_json?.llm_prescan?.pair_status;
    if (needsConfirm && suggested === btn.dataset.pairStatus) {
      btn.classList.add('pair-review-suggested');
    }
    btn.disabled = locked || !needsConfirm;
    btn.onclick = () => {
      if (!btn.disabled && btn.dataset.pairStatus) submitPairReview(btn.dataset.pairStatus);
    };
  });
}

function renderAtomOverview() {
  const el = document.getElementById('analysis-atom-overview');
  if (!el) return;
  const counts = pageAtomTypeCounts(currentPageIndex);
  const total = atomsOnPage(currentPageIndex).length;
  const stage = pageStageHint(currentPageIndex);
  el.innerHTML = `
    <div class="atom-overview-line"><strong>本页 ${total} 个原子</strong></div>
    <div class="atom-overview-types">
      ${counts.text ? `<span>📝 文本 ${counts.text}</span>` : ''}
      ${counts.title ? `<span>🏷️ 标题 ${counts.title}</span>` : ''}
      ${counts.image ? `<span>🖼️ 图像 ${counts.image}</span>` : ''}
      ${counts.other ? `<span>· 其他 ${counts.other}</span>` : ''}
    </div>
    <div class="hint atom-overview-stage">所属：${escHtml(stage)}</div>
  `;
}

function renderAtomCatalog() {
  const listEl = document.getElementById('atom-catalog-list');
  const searchInp = document.getElementById('atom-catalog-search');
  const filterSel = document.getElementById('atom-catalog-filter');
  if (!listEl) return;

  if (searchInp && searchInp.value !== atomCatalogSearch) searchInp.value = atomCatalogSearch;
  if (filterSel && filterSel.value !== atomCatalogFilter) filterSel.value = atomCatalogFilter;

  let rows = atomsOnPage(currentPageIndex).filter((a) => !a.is_placeholder);
  if (atomCatalogFilter) rows = rows.filter((a) => a.atom_type === atomCatalogFilter);
  const q = atomCatalogSearch.trim().toLowerCase();
  if (q) {
    rows = rows.filter((a) => {
      const hay = `${a.atom_code} ${a.content || ''} ${a.ocr_text || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }

  if (!rows.length) {
    listEl.innerHTML = '<li class="hint atom-catalog-empty">本页无匹配原子</li>';
    return;
  }

  listEl.innerHTML = rows.map((a) => {
    const icon = ATOM_TYPE_ICON[a.atom_type] || '·';
    const summary = atomSummaryLine(a) || '—';
    const owner = atomOwnerBlock(a.atom_code);
    const active = catalogFocusAtomCode === a.atom_code || selectedAtoms.has(a.atom_code);
    return (
      `<li class="atom-catalog-item${active ? ' is-active' : ''}" data-atom-code="${escHtml(a.atom_code)}">` +
      `<span class="atom-catalog-icon">${icon}</span>` +
      `<span class="atom-catalog-code">${escHtml(a.atom_code)}</span>` +
      `<span class="atom-catalog-type">${escHtml(atomTypeLabel(a.atom_type))}</span>` +
      `<span class="atom-catalog-text">${escHtml(summary)}</span>` +
      `${owner ? `<span class="atom-catalog-owner">${escHtml(owner.block_code)}</span>` : ''}` +
      `</li>`
    );
  }).join('');

  listEl.querySelectorAll('.atom-catalog-item').forEach((row) => {
    row.onclick = () => focusAtomFromCatalog(row.dataset.atomCode);
  });
}

function renderAnalysisRightPanel() {
  if (inBuildAnchorPhase()) return;
  renderAnalysisVerdictBar();
  const prescanEl = document.getElementById('analysis-prescan-content');
  const ps = state?.content_prescan;
  const busy = Boolean(state?._prescanBusy);
  if (prescanEl) {
    if (busy || ps?.status === 'running') {
      prescanEl.innerHTML = '<p class="hint">正在对照预判…</p>';
    } else {
      prescanEl.innerHTML = buildMatchReferenceHtml(ps);
    }
  }
  renderAtomOverview();
  renderAtomCatalog();
  renderRightColumnMode();
}

function buildPrescanHtml(ps) {
  return buildMatchReferenceHtml(ps);
}

function renderContentPrescan() {
  const ps = state?.content_prescan;
  const suggestBtn = document.getElementById('prescan-suggest-btn');
  const statusEl = document.getElementById('build-step-status');
  const locked = blocksLocked();
  const busy = Boolean(state?._prescanBusy);

  if (suggestBtn) {
    suggestBtn.disabled = locked || busy || !lessonAtomsStat().curate_ready;
  }

  if (busy || ps?.status === 'running') {
    if (statusEl) statusEl.textContent = '⑤ 对照预判中…';
  }

  renderAnalysisRightPanel();

  if (ps?.recommended_old_lesson_id && ps.coarse_agreement === 'suggest_swap') {
    const sel = document.getElementById('primary-old-select');
    if (sel) {
      const cand = (state?.pair_review?.candidates || []).find(
        (c) => c.lesson_id === ps.recommended_old_lesson_id,
      );
      if (cand?.lesson_uid) sel.value = cand.lesson_uid;
    }
  }
}

async function runPrescanSuggest() {
  const btn = document.getElementById('prescan-suggest-btn');
  if (btn) btn.disabled = true;
  if (!lessonAtomsStat().curate_ready) {
    toast('请先 AI 整理本课（OCR + 整理原子）', true);
    if (btn) btn.disabled = false;
    return;
  }
  state = { ...state, _prescanBusy: 'match' };
  renderContentPrescan();
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/content-prescan`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const d = await readJson(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
    state = { ...d, _prescanBusy: null };
    renderAll();
    toast('对照预判完成');
  } catch (e) {
    state = { ...state, _prescanBusy: null };
    renderContentPrescan();
    toast(String(e.message || e), true);
  }
}

function syncInLessonAutoButtons() {
  const auto = allowsInLessonAuto();
  const seedLessonBtn = document.getElementById('seed-from-old-lesson-btn');
  if (seedLessonBtn) {
    seedLessonBtn.disabled = blocksLocked() || !auto;
    seedLessonBtn.title = auto
      ? '对照旧块在本课全部教材页自动建块（完成后点「补全锚定」）'
      : '须点「确认对照」后可用；改动大/无对应时请手动建块或「跨课找旧块」';
  }
}

async function submitPairReview(status) {
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/pair-review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '保存失败');
    state = d;
    renderAll();
    const labels = {
      confirmed: '已确认主参照对照，可使用课内自动建块',
      heavy_change: '已标记同课改动大，请手动建块或跨课找旧块',
      no_old: '已隐藏主参照旧课，请独立建块',
    };
    toast(labels[status] || '已更新对照状态');
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function swapPrimaryOldLesson() {
  const sel = document.getElementById('primary-old-select');
  const uid = sel?.value;
  if (!uid) return;
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/pair-review/swap-primary`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_lesson_uid: uid }),
      },
    );
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '更换失败');
    state = d;
    renderAll();
    toast('已更换主参照旧课，请重新 Step 0 确认');
  } catch (e) {
    toast(e.message || String(e));
  }
}

/** 推断当前应执行的建块步骤（对照分析六步 → 建块 → 锚定） */
function analysisSegments() {
  return state?.analysis?.segments || {};
}

function lessonTextOcrComplete() {
  return !!(state?.prescan_ocr?.text_ocr_complete);
}

function lessonImageOcrComplete() {
  const po = state?.prescan_ocr || {};
  return !!(po.image_ocr_complete || (po.text_ocr_complete && !po.lesson_has_images));
}

function lessonOcrComplete() {
  return lessonTextOcrComplete() && lessonImageOcrComplete();
}

function ocrReadyForCurate() {
  const po = state?.prescan_ocr || {};
  if (!po.text_ocr_complete) return false;
  if (po.image_ocr_complete) return true;
  return !po.lesson_has_images && (state?.stats?.atom_count || 0) > 0;
}

function computeWorkflowGuide() {
  const locked = blocksLocked();
  const pr = state?.pair_review || {};
  const stats = state?.stats || {};
  const segs = analysisSegments();
  const atomsReady = lessonAtomsStat().atoms_ready;
  const prescanDone = state?.content_prescan?.status === 'done';
  const pageAtoms = atomsOnPage(currentPageIndex).length;
  const pageBlocks = blocksOnPage(currentPageIndex).length;
  const unboundOnPage = atomsOnPage(currentPageIndex).filter((a) => !atomOwnerBlock(a.atom_code)).length;
  const hasPairedOld = !!(state?.paired_old?.old_blocks?.length);
  const unanchored = unanchoredBlockCount();
  const pagesWithAtoms = pagesWithAtomsCount();
  const hasPairLesson = !!pr.primary_old_lesson;

  const analysisSteps = [
    { id: 'text_ocr', label: '文字OCR' },
    { id: 'image_ocr', label: '图片OCR' },
    { id: 'curate', label: '整理原子' },
  ];
  if (hasPairLesson) {
    analysisSteps.push(
      { id: 'block_match', label: '栏目·块名' },
      { id: 'prescan', label: '对照预判' },
      { id: 'pair_review', label: '确认对照' },
    );
  }
  const buildOffset = hasPairLesson ? 6 : 3;
  const buildSteps = [
    { id: 'blocks', label: `${buildOffset + 1} 创建区块` },
  ];
  if (hasPairedOld || (pr.referenced_old_lessons || []).length) {
    buildSteps.push({ id: 'anchor', label: `${buildOffset + 2} 锚定旧块` });
  }
  buildSteps.push({ id: 'lock', label: `${buildOffset + buildSteps.length + 1} 保存锁定` });
  const steps = [...analysisSteps, ...buildSteps];

  if (locked) {
    return {
      steps: steps.map((s) => ({ ...s, status: 'done' })),
      nextHint: '本课区块已锁定。如需修改请点顶部「解锁」，完成后可进入「新旧对比」。',
      nextBtnId: null,
      progress: `全课 ${stats.block_count || 0} 个区块 · 已锁定`,
      pageHint: '',
      pageHintDone: true,
      phase: 'done',
      progressPercent: 100,
      stepTotal: steps.length,
    };
  }

  let phase = 'lock';
  const textDone = segs.text_ocr === 'done';
  const imageDone = segs.image_ocr === 'done' || segs.image_ocr === 'skip';
  const curateDone = segs.curate === 'done';
  const blockDone = segs.block_match === 'done';
  const prescanSegDone = segs.prescan === 'done';
  const pairDone = segs.pair_review === 'done' || segs.pair_review === 'skip';

  const analysisStage = state?.analysis?.stage || '';

  if (!textDone && !['await_confirm', 'needs_review', 'ready'].includes(analysisStage)) {
    phase = 'text_ocr';
  } else if (!imageDone && !['await_confirm', 'needs_review', 'ready'].includes(analysisStage)) {
    phase = 'image_ocr';
  } else if (!curateDone && !['await_confirm', 'needs_review', 'ready'].includes(analysisStage)) {
    phase = 'curate';
  } else if (hasPairLesson && !blockDone) phase = 'block_match';
  else if (hasPairLesson && !prescanSegDone && !prescanDone) phase = 'prescan';
  else if (hasPairLesson && !pairDone && pr.status === 'pending') phase = 'pair_review';
  else if (stats.block_count === 0 || (pageBlocks === 0 && unboundOnPage > 0)) phase = 'blocks';
  else if ((hasPairedOld || unanchored > 0) && unanchored > 0) phase = 'anchor';

  const segStatus = (id) => {
    const st = segs[id];
    if (st === 'done' || st === 'skip') return 'done';
    if (st === 'running') return 'current';
    if (id === phase) return 'current';
    return 'pending';
  };

  const stepsOut = steps.map((s) => {
    if (['text_ocr', 'image_ocr', 'curate', 'block_match', 'prescan', 'pair_review'].includes(s.id)) {
      return { ...s, status: segStatus(s.id) };
    }
    const order = steps.map((x) => x.id);
    const si = order.indexOf(s.id);
    const pi = order.indexOf(phase);
    let status = 'pending';
    if (si < pi) status = 'done';
    else if (si === pi) status = 'current';
    return { ...s, status };
  });

  let nextBtnId = null;
  let nextHint = '';
  let pageHint = '';
  let pageHintDone = false;

  if (phase === 'text_ocr') {
    nextBtnId = 'ocr-text-btn';
    nextHint = '第一步：点「高级 → 文字 OCR」。';
    pageHint = '文字OCR';
  } else if (phase === 'image_ocr') {
    nextBtnId = 'ocr-image-btn';
    nextHint = '第二步：点「高级 → 图片 OCR」。';
    pageHint = '图片OCR';
  } else if (phase === 'curate') {
    nextBtnId = 'ai-curate-lesson-btn';
    nextHint = '第三步：点「高级 → 整理原子」。';
    pageHint = '整理原子';
  } else if (phase === 'block_match' || phase === 'prescan') {
    nextBtnId = 'prescan-suggest-btn';
    nextHint = '第四～五步：点「运行对照分析」或「栏目·预判」。';
    pageHint = '栏目·块名 · 对照预判';
  } else if (phase === 'pair_review') {
    nextBtnId = null;
    nextHint = '第六步：见右侧 <strong>一键确认</strong> 完成确认对照。';
    pageHint = '人工确认 · 左=旧教材，中=新教材+原子';
  } else if (phase === 'blocks') {
    if (hasPairedOld && allowsInLessonAuto()) {
      nextBtnId = 'seed-from-old-lesson-btn';
      nextHint = `第 ${buildOffset + 1} 步：点顶部 <strong>「AI 建整课」</strong>，对照旧块在本课全部页生成新区块。`;
      pageHint = `${buildOffset + 1} 建块 · 左=旧教材+旧块，中=新教材原子→新区块`;
    } else {
      nextBtnId = 'create-block-btn';
      nextHint = allowsInLessonAuto()
        ? '原子已就绪。请在中间教材图上 <strong>勾选原子</strong>，在右侧选择模块并填写名称，再点 <strong>「创建区块」</strong>。'
        : '改动较大或无主参照：请<strong>勾选原子手动建块</strong>；需要旧块来源时用「跨课找旧块」。';
      pageHint = `${buildOffset + 1} 手动建块 · 中=新页+原子，右=模块与块名`;
    }
  } else if (phase === 'anchor') {
    nextBtnId = 'anchor-page-btn';
    nextHint = `还有 ${unanchored} 个区块未锚定。点 <strong>「本页锚定」</strong>或 <strong>「补全锚定」</strong>。`;
    pageHint = `${buildOffset + 2} 锚定 · 左=旧块列表，中=已建新区块，连旧块来源`;
  } else {
    nextBtnId = 'lock-blocks-btn';
    nextHint = unanchored > 0
      ? `仍有 ${unanchored} 块未锚定，可用「补全锚定」在全库找旧块来源。`
      : '本课区块已就绪。请逐页检查对照无误后，点顶部 <strong>「保存锁定」</strong>。';
    pageHint = unanchored > 0
      ? '补全锚定 · 未匹配块用「跨课找旧块」检索全库'
      : (pageBlocks > 0
        ? '本页区块已就绪。全部页完成后点顶部「保存锁定」。'
        : '本页无新区块。可翻页继续，或点「保存锁定」锁定全课。');
    pageHintDone = pageBlocks > 0 && unanchored === 0;
  }

  const progress = [
    `${pagesWithAtoms}/${stats.page_count || 0} 页有原子`,
    `${stats.block_count || 0} 个区块`,
    hasPairedOld && unanchored ? `${unanchored} 待锚定` : null,
  ].filter(Boolean).join(' · ');

  const stepTotal = stepsOut.length;
  const stepDone = stepsOut.filter((s) => s.status === 'done').length;
  let progressPercent = phase === 'done' ? 100 : Math.round((stepDone / stepTotal) * 100);
  if (phase !== 'done' && stepsOut.some((s) => s.status === 'current')) {
    let intra = 0.2;
    if (phase === 'text_ocr' && (stats.page_count || 0) > 0) {
      intra = Math.min(0.95, (state?.prescan_ocr?.text_pages_done || 0) / stats.page_count);
    } else if (phase === 'image_ocr' && (stats.page_count || 0) > 0) {
      intra = Math.min(0.95, (state?.prescan_ocr?.image_pages_done || 0) / stats.page_count);
    } else if (phase === 'curate' && (stats.page_count || 0) > 0) {
      intra = Math.min(0.95, pagesWithAtoms / stats.page_count);
    } else if (phase === 'blocks' && (stats.block_count || 0) > 0) {
      intra = Math.min(0.95, stats.block_count / Math.max(1, stats.page_count));
    } else if (phase === 'anchor' && unanchored >= 0) {
      const anchored = (stats.block_count || 0) - unanchored;
      intra = stats.block_count ? Math.min(0.95, anchored / stats.block_count) : 0.2;
    }
    progressPercent = Math.min(
      99,
      Math.round(((stepDone + intra) / stepTotal) * 100),
    );
  }

  return {
    steps: stepsOut,
    nextHint,
    nextBtnId,
    progress,
    pageHint,
    pageHintDone,
    phase,
    progressPercent,
    stepTotal,
  };
}

function renderWorkflowGuide() {
  if (!state) return;

  const pr = state?.pair_review || {};
  const guide = computeWorkflowGuide();
  const pageHintEl = document.getElementById('page-action-hint');
  const statusEl = document.getElementById('build-step-status');
  const locked = blocksLocked();

  const stepBtnMap = {
    text_ocr: document.getElementById('step-group-text-ocr'),
    image_ocr: document.getElementById('step-group-image-ocr'),
    curate: document.getElementById('ai-curate-lesson-btn'),
    block_match: document.getElementById('prescan-suggest-btn'),
    prescan: document.getElementById('prescan-suggest-btn'),
    pair_review: document.getElementById('step-group-confirm'),
    blocks: document.getElementById('run-build-anchor-btn'),
    anchor: document.getElementById('bulk-suggest-anchor-btn'),
  };

  guide.steps.forEach((s) => {
    const el = stepBtnMap[s.id];
    if (!el) return;
    el.classList.remove('is-current', 'is-done');
    if (s.id === 'pair_review' && pr.status !== 'pending') {
      if (s.status === 'done' || s.status === 'skip') el.classList.add('is-done');
      return;
    }
    if (s.status === 'current') el.classList.add('is-current');
    if (s.status === 'done') el.classList.add('is-done');
  });

  if (statusEl) {
    const plain = (guide.pageHint || guide.nextHint || '')
      .replace(/<[^>]+>/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    statusEl.textContent = plain.slice(0, 120);
  }

  if (pageHintEl) {
    if (guide.pageHint && !locked && guide.phase !== 'text_ocr' && guide.phase !== 'image_ocr') {
      pageHintEl.hidden = false;
      pageHintEl.textContent = guide.pageHint;
      pageHintEl.classList.toggle('is-done', !!guide.pageHintDone);
    } else {
      pageHintEl.hidden = true;
    }
  }

  document.querySelectorAll('.workflow-next-btn').forEach((el) => {
    el.classList.remove('workflow-next-btn');
  });
  if (guide.nextBtnId && !locked) {
    document.getElementById(guide.nextBtnId)?.classList.add('workflow-next-btn');
  }

  const toolbarBtnIds = [
    'ocr-text-btn',
    'ocr-image-btn',
    'ai-curate-lesson-btn',
    'prescan-suggest-btn',
    'seed-from-old-lesson-btn',
    'anchor-page-btn',
    'bulk-suggest-anchor-btn',
    'lock-blocks-btn',
  ];
  toolbarBtnIds.forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.disabled = locked
      || (id === 'ocr-image-btn' && !lessonTextOcrComplete())
      || (id === 'prescan-suggest-btn' && !lessonAtomsStat().curate_ready)
      || (id === 'ai-curate-lesson-btn' && !ocrReadyForCurate());
  });
  document.querySelectorAll('.pair-review-btn').forEach((btn) => {
    btn.disabled = locked;
  });
}

function renderHeader() {
  const les = state?.lesson || {};
  const st = state?.stats || {};
  const pageAtoms = atomsOnPage(currentPageIndex).length;
  const paired = state?.paired_old;
  const chips = [
    les.display_title || les.volume_code,
    `教材 ${st.page_count || 0} 页`,
    `本页 ${pageAtoms} 个原子`,
    `全课 ${st.atom_count || 0} 个原子`,
    `新区块 ${st.block_count || 0}`,
  ].filter(Boolean);
  if (paired?.old_lesson) {
    const ol = paired.old_lesson;
    const matchNote = `${paired.match_label || paired.match_tier || ''}${paired.similarity_score != null ? ` ${(paired.similarity_score * 100).toFixed(1)}%` : ''}`.trim();
    chips.push(`对照旧课 ${ol.lesson_no} ${ol.lesson_name}${matchNote ? ` · ${matchNote}` : ''}`);
  }
  document.getElementById('page-sub').innerHTML = chips
    .map((c) => `<span class="annotate-meta-chip">${escHtml(c)}</span>`)
    .join('');
  const colTitle = document.querySelector('#textbook-col .col-head h2');
  if (colTitle) colTitle.textContent = `新教材页 + 原子（本页 ${pageAtoms}）`;
}

function atomBoxSortKey(atom) {
  const owner = atomOwnerBlock(atom.atom_code);
  const highlightingOwn = isAtomInHighlightedBlock(atom.atom_code);
  const locked = owner && !highlightingOwn;
  if (locked) return 0;
  if (selectedAtoms.has(atom.atom_code) || highlightingOwn) return 2;
  return 1;
}

function paintAtomLayer() {
  const img = document.getElementById('tb-image');
  const layer = document.getElementById('atom-layer');
  if (!img || img.hidden || !layer) return;
  layer.classList.toggle('block-edit-mode', !!editingBlockCode);
  layer.classList.toggle('block-focus-mode', !!highlightBlockCode());
  layer.style.height = `${img.clientHeight}px`;
  const pageAtoms = [...atomsOnPage(currentPageIndex)].sort(
    (a, b) => atomBoxSortKey(a) - atomBoxSortKey(b),
  );
  if (!pageAtoms.length && !blocksLocked()) {
    const hint = !lessonTextOcrComplete() && (stats.atom_count || 0) > 0
      ? `本页尚无原子（全课已有 ${stats.atom_count} 个）。请点顶部 ①② 补全整课 OCR`
      : !lessonTextOcrComplete()
        ? '请在顶部 ① 点「文字 OCR」（整课全部页）'
        : !lessonImageOcrComplete()
          ? '请在顶部 ② 点「图片 OCR」（整课全部页）'
          : '请在顶部 ③ 点「整理本课原子」';
    layer.innerHTML = `<div class="atom-empty-state">
      <p>本页尚无原子</p>
      <p class="hint">${escHtml(hint)}</p>
    </div>`;
    syncAtomActionButtons();
    applyAtomTagVisibility();
    return;
  }
  layer.innerHTML = pageAtoms.map((a) => {
    const b = a.bbox || {};
    const owner = atomOwnerBlock(a.atom_code);
    const highlightingOwn = isAtomInHighlightedBlock(a.atom_code);
    const editingOwn = editingBlockCode && owner?.block_code === editingBlockCode;
    const locked = owner && !highlightingOwn && !editingOwn ? ' locked' : '';
    const jumpable = owner && !blocksLocked() && !editingBlockCode ? ' atom-jumpable' : '';
    const pairFocus = highlightingOwn && !editingBlockCode && !!highlightBlockCode();
    const catalogFocus = catalogFocusAtomCode === a.atom_code ? ' catalog-focus' : '';
    const sel = (selectedAtoms.has(a.atom_code) || pairFocus)
      ? ' selected'
      : '';
    const pairHighlightCls = pairFocus ? ' pair-highlight' : '';
    const pendingRemove = editingBlockCode && highlightingOwn && !selectedAtoms.has(a.atom_code)
      ? ' pending-remove'
      : '';
    const editingOwnCls = (highlightingOwn || editingOwn) ? ' editing-own' : '';
    const preview = (a.content || '').replace(/\s+/g, ' ').trim().slice(0, 12);
    const ownerNote = owner ? ` · ${owner.block_code} ${owner.block_name || ''}` : '';
    const unmergeNote = a.can_unmerge_ocr ? ' · 双击拆回 OCR' : '';
    const title = `${a.atom_code}${preview ? ` ${preview}` : ''}${ownerNote}${unmergeNote}`;
    const unmergeCls = a.can_unmerge_ocr ? ' can-unmerge' : '';
    const color = owner ? blockColor(owner.block_code) : '';
    const focusColor = pairFocus ? blockColor(highlightBlockCode()) : '';
    const colorStyle = [
      color ? `--atom-block-color:${color};` : '',
      focusColor ? `--atom-focus-color:${focusColor};` : '',
    ].join('');
    const tagHtml = owner && !highlightingOwn && !editingOwn
      ? `<span class="atom-tag">${a.atom_code}<span class="atom-owner">${owner.block_code}</span></span>`
      : `<span class="atom-tag">${a.atom_code}</span>`;
    return `<div class="atom-box${sel}${catalogFocus}${pairHighlightCls}${locked}${jumpable}${pendingRemove}${editingOwnCls}${unmergeCls}" data-atom="${a.atom_code}" title="${title.replace(/"/g, '&quot;')}" style="${colorStyle}left:${b.x_start * 100}%;top:${b.y_start * 100}%;width:${(b.x_end - b.x_start) * 100}%;height:${(b.y_end - b.y_start) * 100}%">${tagHtml}</div>`;
  }).join('');
  layer.querySelectorAll('.atom-box').forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      handleAtomClick(el.dataset.atom);
    };
  });
  syncAtomActionButtons();
  applyAtomTagVisibility();
  if (highlightBlockCode()) scrollHighlightedAtomsIntoView();
}

function renderTextbook() {
  const page = (state?.textbook_pages || []).find((p) => p.page_index === currentPageIndex);
  const img = document.getElementById('tb-image');
  const layer = document.getElementById('atom-layer');
  if (!page?.url) {
    img.hidden = true;
    layer.innerHTML = '<p class="hint">本页无教材图，请返回接入页生成页图</p>';
    syncBlockFocusChrome();
    return;
  }
  img.hidden = false;
  const paint = () => paintAtomLayer();
  img.onload = paint;
  img.src = page.url;
  if (img.complete && img.naturalWidth > 0) {
    paint();
    requestAnimationFrame(paint);
  }
}

function renderBlockStageRefOptions() {
  const sel = document.getElementById('block-stage-ref');
  if (!sel) return;
  const current = readStageRefFromControls() || getBlockFormStageRef();
  sel.innerHTML = renderBlockStageOptionsHtml(current);
  syncStageRefControls(current, {});
  sel.onchange = () => onStageRefSelectChange(sel);
}

function orderedBlockCodes() {
  return (state?.blocks || []).map((b) => b.block_code);
}

async function reorderBlock(blockCode, delta) {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const codes = orderedBlockCodes();
  const idx = codes.indexOf(blockCode);
  if (idx < 0) return;
  const newIdx = idx + delta;
  if (newIdx < 0 || newIdx >= codes.length) return;
  const reordered = codes.slice();
  [reordered[idx], reordered[newIdx]] = [reordered[newIdx], reordered[idx]];
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/reorder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ block_codes: reordered }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '排序失败');
    const prevEdit = editingBlockCode;
    state = d;
    if (prevEdit && d.code_remap?.[prevEdit]) editingBlockCode = d.code_remap[prevEdit];
    renderBlocks();
    renderTextbook();
    toast(delta < 0 ? '已上移' : '已下移');
  } catch (e) {
    toast(e.message || String(e));
  }
}

function renderBlocks() {
  const root = document.getElementById('blocks-list');
  const blocks = blocksForDisplay();
  const locked = blocksLocked() || isDualTrackPreview();
  const countEl = document.getElementById('blocks-count');
  if (countEl) {
    countEl.textContent = blocks.length ? String(blocks.length) : '';
    countEl.hidden = !blocks.length;
  }
  const previewBanner = isDualTrackPreview()
    ? `<li class="dual-track-preview-banner">
        <strong>${escHtml(dualTrackPreview.modeLabel || '双轨预览')}</strong>
        <span class="hint">仅预览，未写入数据库。点「现网块」恢复。</span>
      </li>`
    : '';
  if (!blocks.length) {
    const guide = computeWorkflowGuide();
    root.innerHTML = `${previewBanner}<li class="blocks-empty-guide hint">
      <p><strong>尚无新区块</strong></p>
      <p class="hint">${escHtml(
        guide.phase === 'atoms' || (state?.stats?.atom_count || 0) === 0
          ? '请先完成「① 整理原子」，再创建区块。'
          : '中间列勾选原子后在右侧创建，或点顶部「AI 建整课」。',
      )}</p>
    </li>`;
    return;
  }
  root.innerHTML = previewBanner + blocks.map((b, idx) => {
    const editing = !isDualTrackPreview() && editingBlockCode === b.block_code;
    const focused = !editing && focusedBlockCode === b.block_code;
    const meta = b.metadata_json || {};
    const suspicious = meta.suspicious;
    const reasonKey = meta.suspicious_reason || '';
    const reasonLabel = SUSPICIOUS_REASON_LABELS[reasonKey] || reasonKey || '存疑区块';
    const suspiciousIcon = suspicious
      ? `<span class="block-suspicious-icon" title="${escHtml(reasonLabel)}">⚠</span>`
      : '';
    const previewCls = isDualTrackPreview() ? ' block-dual-track-preview' : '';
    return `
    <li class="${editing ? 'block-editing' : ''}${focused ? ' block-focused' : ''}${locked ? ' block-row-locked' : ''}${suspicious ? ' block-suspicious' : ''}${previewCls}" data-block-code="${b.block_code}">
      <div class="block-card-move-btns">
        <button type="button" data-move-block="${b.block_code}" data-move-delta="-1" ${idx <= 0 || isDualTrackPreview() ? 'disabled' : ''}>▲</button>
        <button type="button" data-move-block="${b.block_code}" data-move-delta="1" ${idx >= blocks.length - 1 || isDualTrackPreview() ? 'disabled' : ''}>▼</button>
      </div>
      <div class="block-item-main" data-focus-block="${b.block_code}" title="点击查看本区块绑定的教材原子；再次点击取消高亮">
        <div class="block-card-layers">
          ${renderBlockCardHead(b, editing)}
          ${renderBlockTextbookSection(b)}
          ${isDualTrackPreview() ? '' : renderBlockOldRefSection(b)}
          ${isDualTrackPreview()
    ? `<div class="block-card-footer"><p class="hint">预览块 · ${escHtml(b.badge || '')} · ${(b.atom_codes || []).length} 个原子</p></div>`
    : renderBlockStatusFooter(b, { editing, locked, suspicious, reasonLabel, suspiciousIcon })}
        </div>
      </div>
    </li>`;
  }).join('');
  if (isDualTrackPreview()) {
    root.querySelectorAll('[data-focus-block]').forEach((el) => {
      el.onclick = () => {
        const code = el.dataset.focusBlock;
        const block = blocks.find((x) => x.block_code === code);
        if (!block) return;
        focusBlock(block);
      };
    });
    return;
  }
  bindBlockMetaEditEvents(root);
  root.querySelectorAll('[data-clear-anchor]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      clearBlockAnchor(btn.dataset.clearAnchor);
    };
  });
  root.querySelectorAll('[data-focus-block]').forEach((el) => {
    el.onclick = () => {
      const code = el.dataset.focusBlock;
      const block = (state?.blocks || []).find((x) => x.block_code === code);
      if (!block) return;
      if (editingBlockCode && editingBlockCode !== code && !blocksLocked()) {
        startBlockEdit(block);
        return;
      }
      focusBlock(block);
    };
  });
  root.querySelectorAll('[data-move-block]').forEach((btn) => {
    btn.onclick = () => {
      if (btn.disabled) return;
      reorderBlock(btn.dataset.moveBlock, Number(btn.dataset.moveDelta));
    };
  });
  root.querySelectorAll('[data-edit-block]').forEach((btn) => {
    btn.onclick = () => {
      if (locked) return;
      const block = (state?.blocks || []).find((x) => x.block_code === btn.dataset.editBlock);
      if (!block || editingBlockCode === block.block_code) return;
      startBlockEdit(block);
    };
  });
  root.querySelectorAll('[data-del-block]').forEach((btn) => {
    btn.onclick = async () => {
      if (blocksLocked()) return;
      if (!confirm(`删除区块 ${btn.dataset.delBlock}？`)) return;
      const deleted = btn.dataset.delBlock;
      const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(deleted)}`, { method: 'DELETE' });
      const d = await readJson(r);
      if (!r.ok || !d.ok) throw new Error(d.error || '删除失败');
      state = d;
      if (editingBlockCode === deleted) {
        editingBlockCode = null;
        clearBlockFocus();
      } else if (focusedBlockCode === deleted) {
        clearBlockFocus();
      }
      renderAll();
      toast('已删除区块');
    };
  });
}

function startBlockEdit(block) {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  editingBlockCode = block.block_code;
  focusedBlockCode = block.block_code;
  editingBlockName = (block.block_name || '').trim();
  editingStageRef = (block.stage_ref || '').trim();
  setBlockFormName(editingBlockName);
  setBlockFormStageRef(editingStageRef);
  selectedAtoms.clear();
  for (const code of block.atom_codes || []) selectedAtoms.add(code);
  const anchorRef = anchorRefForBlock(block);
  selectedOldBlockCode = anchorRef?.old_block_code || null;
  focusedOldBlockCode = selectedOldBlockCode;
  navigateToNewBlockContent(block);
  if (selectedOldBlockCode) {
    const oldBlock = findOldBlock(selectedOldBlockCode);
    if (oldBlock) navigateToOldBlockContent(oldBlock);
  }
  syncBlockFormUi();
  renderBlockStageRefOptions();
  renderTextbook();
  renderBlocks();
  afterBlockFocusRender();
  scrollBlockIntoView(block.block_code);
  renderOldBlockPicker();
  toast(`正在编辑 ${block.block_code}`);
}

function renderAll() {
  if (blocksLocked()) {
    editingBlockCode = null;
    editingBlockName = '';
    editingStageRef = '';
    clearBlockFocus();
    selectedAtoms.clear();
    const nameInp = document.getElementById('block-name');
    if (nameInp) nameInp.value = '';
    syncStageRefControls('', {});
  }
  renderOldRef();
  renderPairReview();
  renderContentPrescan();
  renderRightColumnMode();
  renderOldPrescanBridge();
  renderAnnotatePipelines();
  renderBlockingSummaryCard();
  renderHeader();
  renderTextbookPageNav();
  renderTextbook();
  refreshOldRefViewer();
  renderBlockStageRefOptions();
  renderBlocks();
  syncBlockFormUi();
  syncBlockFocusChrome();
  syncInLessonAutoButtons();
  renderWorkflowGuide();
}

async function load() {
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/annotate`);
  const d = await readJson(r);
  if (!r.ok || !d.ok) throw new Error(d.error || '加载失败');
  state = d;
  if ((d.stats?.block_count || 0) > 0) state._buildPhaseActive = true;
  renderAll();
}

async function submitBlockForm() {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const name = getBlockFormName();
  const stageRef = getBlockFormStageRef();
  if (!name && !stageRef) {
    toast('请选择新教材模块或填写区块名称');
    return;
  }
  if (!editingBlockCode && selectedAtoms.size === 0) {
    toast('请至少勾选一个原子');
    return;
  }
  const payload = {
    block_name: name,
    stage_ref: stageRef,
    atom_codes: Array.from(selectedAtoms),
  };
  const isEdit = !!editingBlockCode;
  const anchoredOld = selectedOldBlockCode;
  if (anchoredOld) {
    payload.anchor_old_block_code = anchoredOld;
    payload.anchor_old_page_index = oldCurrentPageIndex;
  } else if (isEdit) {
    const block = (state?.blocks || []).find((b) => b.block_code === editingBlockCode);
    if (block && anchorRefForBlock(block)) {
      payload.clear_anchor_old = true;
    }
  }
  const activeBtn = document.getElementById('create-block-btn');
  if (activeBtn) activeBtn.disabled = true;
  try {
    const url = isEdit
      ? `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(editingBlockCode)}`
      : `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks`;
    const r = await fetch(url, {
      method: isEdit ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '保存失败');
    state = d;
    const savedCode = editingBlockCode;
    editingBlockCode = null;
    editingBlockName = '';
    editingStageRef = '';
    clearBlockFocus();
    selectedAtoms.clear();
    if (anchoredOld) selectedOldBlockCode = null;
    const nameInp = document.getElementById('block-name');
    if (nameInp) nameInp.value = '';
    syncStageRefControls('', {});
    renderAll();
    toast(savedCode
      ? `已保存 ${savedCode}${anchoredOld ? `，已锚定旧块 ${anchoredOld}` : ''}`
      : `新区块已创建${anchoredOld ? `，已锚定旧块 ${anchoredOld}` : ''}`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (activeBtn) activeBtn.disabled = false;
    const wrap = document.getElementById('old-block-picker-wrap');
    if (wrap) wrap.hidden = true;
  }
}

function renderOldBlockPicker() {
  const paired = state?.paired_old;
  const blocks = paired?.old_blocks || [];
  const wrap = document.getElementById('old-block-picker-wrap');
  const sel = document.getElementById('old-block-picker');

  if (!wrap || !sel) return;
  if (!blocks.length) {
    wrap.hidden = true;
    return;
  }

  wrap.hidden = false;
  sel.innerHTML = `<option value="">— 选择旧区块 —</option>` +
    blocks.map(b => `<option value="${escHtml(b.block_code)}">${escHtml(b.block_code)} ${escHtml(b.block_name)}</option>`).join('');

  sel.value = selectedOldBlockCode || '';
  sel.onchange = (ev) => {
    selectedOldBlockCode = ev.target.value || null;
    renderOldModuleHighlights();
    syncOldBlockPickUi();
  };
}

document.getElementById('create-block-btn').onclick = () => submitBlockForm();
document.getElementById('cancel-block-edit-btn')?.addEventListener('click', () => {
  editingBlockCode = null;
  editingBlockName = '';
  editingStageRef = '';
  clearBlockFocus();
  selectedAtoms.clear();
  const nameInp = document.getElementById('block-name');
  if (nameInp) nameInp.value = '';
  syncStageRefControls('', {});
  const wrap = document.getElementById('old-block-picker-wrap');
  if (wrap) wrap.hidden = true;
  renderAll();
});
document.getElementById('tb-prev-btn')?.addEventListener('click', () => gotoTextbookPage(-1));
document.getElementById('tb-next-btn')?.addEventListener('click', () => gotoTextbookPage(1));
document.getElementById('old-tb-prev-btn')?.addEventListener('click', () => gotoOldTextbookPage(-1));
document.getElementById('old-tb-next-btn')?.addEventListener('click', () => gotoOldTextbookPage(1));

async function ocrLessonPhase(phase, { btnId, label, confirmMsg, toastMsg }) {
  const pages = state?.textbook_pages || [];
  const allHaveOcr = pages.length > 0 && pages.every((p) => p.has_ocr_baseline);
  const ocrScope = allHaveOcr ? 'lesson_all' : 'missing_pages';
  if (!confirm(confirmMsg)) return;
  const btn = document.getElementById(btnId);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '识别中…';
  }
  toast(toastMsg);
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/extract-lesson`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ocr_scope: ocrScope, ocr_phase: phase }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || 'OCR 失败');
    state = d;
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    const done = d.pages_ocr_this_run || 0;
    const total = d.pages_total || pages.length;
    const warnN = (d.warnings || []).length;
    toast(warnN
      ? `${label} 完成 ${done}/${total} 页（${warnN} 条提示）`
      : `${label} 完成 ${done}/${total} 页`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
}

async function ocrTextLesson() {
  return ocrLessonPhase('text', {
    btnId: 'ocr-text-btn',
    label: '文字 OCR',
    confirmMsg: '对本课教材页执行文字 OCR 并写入原子。\n误操作可用「撤销上一步」。继续？',
    toastMsg: '正在文字 OCR…',
  });
}

async function ocrImageLesson() {
  if (!lessonTextOcrComplete()) {
    toast('请先完成 ① 文字 OCR');
    return;
  }
  return ocrLessonPhase('images', {
    btnId: 'ocr-image-btn',
    label: '图片 OCR',
    confirmMsg: '对本课教材页执行图片 OCR（插图区域）。\n误操作可用「撤销上一步」。继续？',
    toastMsg: '正在图片 OCR…',
  });
}

document.getElementById('ocr-text-btn')?.addEventListener('click', () => ocrTextLesson());
document.getElementById('ocr-image-btn')?.addEventListener('click', () => ocrImageLesson());

async function aiCurateWholeLesson() {
  if (!ocrReadyForCurate()) {
    toast('请先完成 ① 文字 OCR 与 ② 图片 OCR');
    return;
  }
  if (!confirm('对本课全部教材页执行 AI 整理（合并/删除原子）。\n误操作可用「撤销上一步」。继续？')) return;
  const btn = document.getElementById('ai-curate-lesson-btn');
  const label = '整理本课原子';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '整理中…';
  }
  toast('正在 AI 整理本课原子…');
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/ai-curate-lesson`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ extract_first: false }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '整理失败');
    state = d;
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    toast(`本课整理完成：${d.curated_count || 0}/${d.page_count || 0} 页`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
}

document.getElementById('ai-curate-lesson-btn')?.addEventListener('click', () => aiCurateWholeLesson());

document.getElementById('save-atoms-btn')?.addEventListener('click', async () => {
  if (!confirm('保存本页：删除库中本页旧原子并按当前框重新编号？')) return;
  const btn = document.getElementById('save-atoms-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/save-page`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page_index: currentPageIndex }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '保存失败');
    state = d;
    clearUndoSnapshot();
    renderAll();
    toast('本页已保存并重新编号');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
});

document.getElementById('clear-atoms-btn')?.addEventListener('click', () => {
  selectedAtoms.clear();
  renderTextbook();
  syncAtomActionButtons();
  syncBlockFormUi();
});

document.getElementById('delete-atoms-btn')?.addEventListener('click', async () => {
  const codes = Array.from(selectedAtoms);
  if (!codes.length || !confirm(`删除 ${codes.length} 个原子？`)) return;
  captureUndoSnapshot();
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ atom_codes: codes }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '删除失败');
    state = d;
    selectedAtoms.clear();
    renderAll();
    toast('已删除');
  } catch (e) {
    toast(e.message || String(e));
  }
});

document.getElementById('merge-atoms-btn')?.addEventListener('click', async () => {
  const codes = Array.from(selectedAtoms);
  if (codes.length < 2 || !confirm(`合并 ${codes.length} 个原子？`)) return;
  captureUndoSnapshot();
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/merge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ atom_codes: codes }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '合并失败');
    state = d;
    selectedAtoms.clear();
    if (d.atom_code) selectedAtoms.add(d.atom_code);
    renderAll();
    toast(`已合并为 ${d.atom_code}`);
  } catch (e) {
    toast(e.message || String(e));
  }
});

document.getElementById('unmerge-ocr-btn')?.addEventListener('click', async () => {
  const code = Array.from(selectedAtoms)[0];
  if (!code) return;
  await unmergeAtomToOcr(code, { confirm: true });
});

async function unmergeAtomToOcr(atomCode, { confirm = false } = {}) {
  const code = String(atomCode || '').trim();
  if (!code) return;
  const atom = (state?.atoms || []).find((x) => x.atom_code === code);
  if (!atom?.can_unmerge_ocr) {
    toast('该原子无法拆回 OCR（需先有 OCR 基准且为误合并块）');
    return;
  }
  const n = atom.ocr_source_count || 0;
  if (confirm && !window.confirm(
    `将「${atomLabel(atom)}」拆回约 ${n} 个 OCR 碎块并重编号。继续？`,
  )) {
    return;
  }
  captureUndoSnapshot();
  const btn = document.getElementById('unmerge-ocr-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/unmerge-ocr`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ atom_code: code, renumber: true }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '拆回失败');
    state = d;
    selectedAtoms.clear();
    (d.split_into || []).forEach((c) => selectedAtoms.add(c));
    clearBlockFocus();
    clearUndoSnapshot();
    renderAll();
    syncAtomActionButtons();
    const count = d.ocr_piece_count || (d.split_into || []).length;
    const renumNote = d.renumbered ? '，已重编号' : '';
    toast(`已拆回 ${count} 个 OCR 碎块${renumNote}`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    syncAtomActionButtons();
  }
}

document.getElementById('undo-atoms-btn')?.addEventListener('click', async () => {
  if (!undoSnapshot) return;
  const snap = undoSnapshot;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/restore-page`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page_index: snap.page_index, atoms: snap.atoms }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '恢复失败');
    state = d;
    clearUndoSnapshot();
    gotoTextbookPageIndex(snap.page_index);
    toast('已撤销上一步');
  } catch (e) {
    toast(e.message || String(e));
  }
});

document.getElementById('old-sync-toggle-btn')?.addEventListener('click', () => toggleOldPageSync());
document.getElementById('old-sync-badge')?.addEventListener('click', () => toggleOldPageSync());
document.getElementById('old-sync-badge')?.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' || ev.key === ' ') {
    ev.preventDefault();
    toggleOldPageSync();
  }
});

document.querySelectorAll('.pair-review-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    const status = btn.dataset.pairStatus;
    if (status) submitPairReview(status);
  });
});
document.getElementById('primary-old-swap-btn')?.addEventListener('click', () => swapPrimaryOldLesson());
document.getElementById('prescan-suggest-btn')?.addEventListener('click', () => runPrescanSuggest());

document.getElementById('atom-catalog-search')?.addEventListener('input', (ev) => {
  atomCatalogSearch = ev.target.value;
  renderAtomCatalog();
});
document.getElementById('atom-catalog-filter')?.addEventListener('change', (ev) => {
  atomCatalogFilter = ev.target.value;
  renderAtomCatalog();
});

document.getElementById('anchor-page-btn')?.addEventListener('click', async () => {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  if (!state?.paired_old?.old_blocks?.length) {
    toast('无粗分旧课区块');
    return;
  }
  const btn = document.getElementById('anchor-page-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '补锚定中…';
  }
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/anchor-existing-on-page`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          new_page_index: currentPageIndex,
          old_page_index: oldCurrentPageIndex,
          replace_existing: true,
        }),
      },
    );
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '补锚定失败');
    state = d;
    renderAll();
    toast(`本页已补锚定 ${d.updated_count || 0} 个区块`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '本页锚定';
    }
  }
});

document.getElementById('seed-from-old-lesson-btn')?.addEventListener('click', async () => {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  if (!state?.paired_old?.old_blocks?.length) {
    toast('无粗分旧课区块，请先在旧侧建块');
    return;
  }
  if (!allowsInLessonAuto()) {
    toast('请先点「确认对照」，或改用「跨课找旧块」');
    return;
  }
  const pageCount = state?.stats?.page_count || textbookPageIndices().length || 0;
  if (
    !confirm(
      `对照旧块在本课全部 ${pageCount || '各'} 页自动建块？\n\n`
      + '建块完成后请点「补全锚定」。在已有区块上追加，不会清空。\n'
      + '若要重来请先点「一键清空区块」。耗时可能数分钟，请勿关闭页面。',
    )
  ) {
    return;
  }
  const btn = document.getElementById('seed-from-old-lesson-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '整课建块中…';
  }
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/seed-from-old-lesson`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replace_existing: false, run_anchor: false }),
      },
    );
    const d = await readJson(r);
    if (!r.ok) throw new Error(d.error || `整课建块失败（HTTP ${r.status}）`);
    if (d.ok === false) throw new Error(d.error || '整课建块失败');
    if (d.workspace_reload_error) {
      const reloadR = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/annotate`);
      const reloadD = await readJson(reloadR);
      if (reloadR.ok && reloadD.ok !== false) {
        state = { ...reloadD, ...d };
      } else {
        state = { ...state, ...d };
      }
    } else {
      state = d;
    }
    renderAll();
    const n = d.created_count || 0;
    const pages = d.pages_seeded || 0;
    let msg = `整课已建 ${n} 个新区块（${pages} 页有产出）`;
    if ((d.errors || []).length) msg += ` · ${d.errors.length} 页跳过`;
    if (n > 0) msg += ' · 可点「补全锚定」';
    toast(msg);
  } catch (e) {
    const errText = String(e.message || e);
    if (errText.includes('服务器返回异常')) {
      toast(`${errText}。若刚更新代码请重启 dev.ps1；多页建块较慢也可能超时`, true);
    } else {
      toast(errText, true);
    }
  } finally {
    if (btn) {
      btn.disabled = blocksLocked() || !allowsInLessonAuto();
      btn.textContent = 'AI 建整课';
    }
  }
});

function blockNameHint() {
  const name = getBlockFormName();
  if (name) return name;
  return getBlockFormStageRef();
}

function atomCodesForSuggest() {
  if (selectedAtoms.size) return Array.from(selectedAtoms);
  if (editingBlockCode) {
    const block = (state?.blocks || []).find((b) => b.block_code === editingBlockCode);
    if (block?.atom_codes?.length) return block.atom_codes.slice();
  }
  const onPage = (state?.blocks || []).filter((b) =>
    (b.atom_codes || []).some((code) => {
      const a = (state?.atoms || []).find((x) => x.atom_code === code);
      return a && Number(a.page_index) === Number(currentPageIndex);
    }),
  );
  if (onPage.length === 1) return (onPage[0].atom_codes || []).slice();
  return atomsOnPage(currentPageIndex)
    .filter((a) => !atomOwnerBlock(a.atom_code))
    .map((a) => a.atom_code);
}

async function suggestAnchorForBlock(blockCode, autoApply) {
  const block = (state?.blocks || []).find((b) => b.block_code === blockCode);
  if (!block) return null;
  const atomCodes = (block.atom_codes || []).slice();
  if (!atomCodes.length) return null;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/corpus/suggest-anchors`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      atom_codes: atomCodes,
      block_name: block.block_name || '',
      limit: 3,
    }),
  });
  const d = await readJson(r);
  if (!r.ok || !d.ok || !d.suggestions?.length) return null;
  const top = d.suggestions[0];
  if (autoApply && !top.anchor_locked) {
    const ar = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(blockCode)}/apply-anchor-suggestion`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ suggestion: top }),
      },
    );
    const applied = await readJson(ar);
    if (ar.ok && applied.ok) {
      state = applied;
      return top;
    }
  }
  return top;
}

async function suggestAnchorFromSelection() {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const atomCodes = atomCodesForSuggest();
  if (!atomCodes.length) {
    toast('请先选原子、编辑区块，或翻到有未绑定原子的页');
    return;
  }
  const btn = document.getElementById('btn-suggest-anchor');
  if (btn) { btn.disabled = true; btn.textContent = '推荐中…'; }
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/corpus/suggest-anchors`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        atom_codes: atomCodes,
        block_name: blockNameHint(),
        limit: 5,
        search_scope: 'unit_first',
      }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '检索失败');
    if (!d.suggestions?.length) {
      toast('未找到合适旧块（已搜同单元+全册）');
      return;
    }
    const top = d.suggestions[0];
    const srcNote = top.is_cross_lesson
      ? `跨课 · ${top.source_lesson_label || ''}`
      : '主参照课';
    const rankerNote = d.ranker === 'anchor_llm' ? ' · 豆包' : '';
    const noteSuffix = d.search_note ? ` · ${d.search_note}` : '';
    if (editingBlockCode && !top.anchor_locked) {
      const ar = await fetch(
        `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(editingBlockCode)}/apply-anchor-suggestion`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ suggestion: top }),
        },
      );
      const applied = await readJson(ar);
      if (!ar.ok || !applied.ok) throw new Error(applied.error || '写入锚定失败');
      state = applied;
      renderAll();
      toast(`已锚定 ${srcNote} · ${top.block_code} ${top.block_name}（${top.reason} ${Math.round(top.score * 100)}%${rankerNote}）`);
      return;
    }
    selectedOldBlockCode = top.block_code || top.block_id;
    renderOldModuleHighlights();
    syncOldBlockPickUi();
    const more = d.suggestions.length > 1 ? `；另有 ${d.suggestions.length - 1} 个候选` : '';
    toast(`${srcNote} · ${top.block_code} ${top.block_name}（${top.reason} ${Math.round(top.score * 100)}%${rankerNote}）${noteSuffix}${more}`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '跨课找旧块'; }
  }
}

async function bulkSuggestAllUnanchored(silent = false) {
  if (blocksLocked()) {
    if (!silent) toast('本课区块已锁定');
    return;
  }
  const targets = (state?.blocks || []).filter((b) => !anchorRefForBlock(b));
  if (!targets.length) {
    if (!silent) toast('所有新区块均已锚定');
    return;
  }
  const btn = document.getElementById('bulk-suggest-anchor-btn');
  if (btn && !silent) { btn.disabled = true; btn.textContent = '补全中…'; }
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/anchor-all-unanchored`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replace_existing: true }),
      },
    );
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '补全锚定失败');
    state = d;
    const ok = d.updated_count || (d.updated || []).length || 0;
    const skip = (d.skipped || []).length;
    if (!silent) renderAll();
    const srcNote = d.anchor_source === 'anchor_llm' ? '（豆包）' : '';
    if (!silent) toast(`批量补全锚定${srcNote}：成功 ${ok}，跳过 ${skip}`);
  } catch (e) {
    if (!silent) toast(e.message || String(e));
  } finally {
    if (btn && !silent) { btn.disabled = false; btn.textContent = '补全未锚定'; }
  }
}

document.getElementById('run-analysis-btn')?.addEventListener('click', () => runLessonAnalysis());
document.getElementById('run-build-anchor-btn')?.addEventListener('click', () => runLessonBuildAnchor());
document.getElementById('analysis-pipeline-collapse')?.addEventListener('click', (ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  if (state?._analysisBusy) return;
  analysisPipelineCollapsed = !analysisPipelineCollapsed;
  renderAnalysisPipelineCollapse();
});
document.getElementById('block-pipeline-collapse')?.addEventListener('click', (ev) => {
  ev.preventDefault();
  ev.stopPropagation();
  if (state?._blockPipelineBusy) return;
  blockPipelineCollapsed = !blockPipelineCollapsed;
  renderBlockPipelineCollapse();
});

document.getElementById('btn-suggest-anchor')?.addEventListener('click', suggestAnchorFromSelection);
document.getElementById('bulk-suggest-anchor-btn')?.addEventListener('click', () => bulkSuggestAllUnanchored(false));

async function deleteAllBlocks() {
  const blocks = state?.blocks || [];
  if (!blocks.length || blocksLocked()) return;
  if (!confirm(`清空本课全部 ${blocks.length} 个新区块？\nOCR 与整理结果保留，可重新建块。`)) return;
  const btn = document.getElementById('delete-all-blocks-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/delete-all`,
      { method: 'POST' },
    );
    const d = await readJson(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
    state = d;
    editingBlockCode = null;
    focusedBlockCode = null;
    selectedAtoms.clear();
    renderAll();
    toast(`已清空 ${blocks.length} 个区块`);
  } catch (e) {
    toast(String(e.message || e), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.getElementById('delete-all-blocks-btn')?.addEventListener('click', () => deleteAllBlocks());

document.getElementById('lock-blocks-btn')?.addEventListener('click', async () => {
  const blocks = state?.blocks || [];
  if (!blocks.length || !confirm(`锁定 ${blocks.length} 个新区块？`)) return;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/lock`, { method: 'POST' });
  const d = await readJson(r);
  if (!r.ok || !d.ok) { toast(d.error || '锁定失败'); return; }
  state = d;
  renderAll();
  toast('已锁定');
});

document.getElementById('unlock-blocks-btn')?.addEventListener('click', async () => {
  if (!confirm('解锁后可继续编辑？')) return;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/unlock`, { method: 'POST' });
  const d = await readJson(r);
  if (!r.ok || !d.ok) { toast(d.error || '解锁失败'); return; }
  state = d;
  renderAll();
  toast('已解锁');
});

document.getElementById('hide-atom-tags-checkbox')?.addEventListener('change', (ev) => {
  hideAtomTags = Boolean(ev.target.checked);
  try {
    localStorage.setItem('annotate.hideAtomTags', hideAtomTags ? '1' : '0');
  } catch {
    /* ignore */
  }
  applyAtomTagVisibility();
});
document.getElementById('atom-layer')?.addEventListener('dblclick', (ev) => {
  const box = ev.target.closest('.atom-box');
  if (!box?.dataset?.atom) return;
  ev.preventDefault();
  ev.stopPropagation();
  const code = box.dataset.atom;
  const atom = (state?.atoms || []).find((x) => x.atom_code === code);
  if (!atom?.can_unmerge_ocr) return;
  unmergeAtomToOcr(code, { confirm: false });
});
applyAtomTagVisibility();

(function initCompareReturnUi() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('from') !== 'compare') return;
  const back = document.getElementById('annotate-close-btn');
  if (back) {
    back.textContent = '← 返回对比';
    back.title = '回到新旧对比审核页';
  }
  const compareLink = document.getElementById('link-compare');
  if (compareLink) compareLink.hidden = false;
})();

document.getElementById('annotate-close-btn')?.addEventListener('click', closeAnnotateOrBack);

window.addEventListener('resize', scheduleProgressStepsResize);

function syncDualTrackStepButtons() {
  document.querySelectorAll('.dual-track-step-btn').forEach((btn) => {
    const step = btn.dataset.dualStep;
    btn.classList.toggle('is-active', step === dualTrackPreview.step);
    btn.disabled = !!state?._dualTrackBusy;
  });
}

function renderUnassignedFollowupHtml(followup, borderline) {
  if (!followup && !borderline) return '';
  const parts = [];
  if (followup?.pipeline?.length) {
    parts.push('<details class="dual-track-followup-pipeline" open><summary>未匹配后续流程</summary><ol class="dual-track-pipeline-ol">');
    for (const p of followup.pipeline) {
      parts.push(`<li><strong>${escHtml(p.label)}</strong> ${escHtml(p.detail)}</li>`);
    }
    parts.push('</ol></details>');
  }
  if (followup?.count > 0) {
    parts.push(`<p><strong>未匹配 ${followup.count} 个原子</strong>（阈值下无归属 → 倾向新课新增）</p>`);
    if (followup.suggested_new_blocks?.length) {
      parts.push('<p class="hint">建议新课新增预览块：</p><ul class="dual-track-nc-list">');
      for (const cl of followup.suggested_new_blocks) {
        parts.push(
          `<li>第${cl.page_index}页 <strong>${escHtml(cl.suggested_block_name)}</strong> — ${cl.atom_count} 原子</li>`,
        );
      }
      parts.push('</ul>');
    }
    parts.push('<table class="dual-track-compare-table dual-track-unassigned-table"><thead><tr>'
      + '<th>原子</th><th>页</th><th>原因</th><th>最高分</th><th>候选旧块</th></tr></thead><tbody>');
    for (const row of (followup.items || []).slice(0, 20)) {
      const cands = (row.top_candidates || row.top_candidates_textbook || [])
        .slice(0, 2)
        .map((c) => `${c.old_block_code}(${c.score})`)
        .join(' ');
      parts.push(
        `<tr><td>${escHtml(row.atom_code)}</td>`
        + `<td>${row.page_index}</td>`
        + `<td title="${escHtml(row.reason_label || '')}">${escHtml((row.reason_label || row.reason || '').slice(0, 12))}</td>`
        + `<td>${row.best_score ?? '—'}</td>`
        + `<td>${escHtml(cands || '—')}</td></tr>`,
      );
      if (row.excerpt) {
        parts.push(`<tr class="dual-track-excerpt-row"><td colspan="5" class="hint">${escHtml(row.excerpt)}</td></tr>`);
      }
    }
    if ((followup.items || []).length > 20) {
      parts.push(`<tr><td colspan="5" class="hint">…另有 ${followup.items.length - 20} 个</td></tr>`);
    }
    parts.push('</tbody></table>');
  } else if (followup) {
    parts.push('<p class="hint">本轨无未匹配原子（全部得分 ≥ 阈值）。可调高 min_score 做边界测试。</p>');
  }
  if (borderline?.count > 0) {
    parts.push(`<p><strong>边界已分配 ${borderline.count} 个</strong>（得分 ${borderline.ceiling} 以下，建议复核）</p>`);
    parts.push('<ul class="dual-track-nc-list">');
    for (const row of (borderline.items || []).slice(0, 8)) {
      parts.push(
        `<li>${escHtml(row.atom_code)} → ${escHtml(row.assigned_old_block)} (${row.score}) · ${escHtml(row.excerpt || '')}</li>`,
      );
    }
    parts.push('</ul>');
  }
  return parts.join('');
}

function scrollDualTrackResultsIntoView(step) {
  document.getElementById('dual-track-result')?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  if (step !== 'compare') {
    setTimeout(() => {
      document.getElementById('blocks-list')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 280);
  }
}

function stepViewHint(step) {
  const hints = {
    textbook: '① 教材轨：下方列表为 TB- 预览块（绿色「教材轨」标签）；点块卡片可在中间页高亮原子。',
    courseware: '② 课件轨：列表变为 CW- 预览块（蓝色「课件轨」标签）。',
    compare: '③ 两轨对比：看本面板内表格；右侧列表仍是上一步预览，可再点①或②切换。',
    fusion: '④ 融合：列表为 F- 融合块；若有未匹配会多出 NC-P* 新课新增预览块。',
    live: '已恢复现网 N01… 数据库块（无预览横幅）。',
  };
  return hints[step] || '';
}

function renderDualTrackResult(data) {
  const box = document.getElementById('dual-track-result');
  const modeLine = document.getElementById('dual-track-mode-line');
  if (!box) return;
  const step = data?.step || dualTrackPreview.step;
  if (step === 'live') {
    box.hidden = true;
    box.innerHTML = '';
    if (modeLine) {
      modeLine.hidden = true;
      modeLine.textContent = '';
    }
    syncDualTrackStepButtons();
    return;
  }
  box.hidden = false;
  if (modeLine) {
    modeLine.hidden = false;
    modeLine.textContent = data.mode_label || '';
  }
  const parts = [];
  const viewHint = stepViewHint(step);
  if (viewHint) {
    parts.push(`<p class="dual-track-step-hint"><strong>本步怎么看：</strong>${escHtml(viewHint)}</p>`);
  }
  const hintEl = document.getElementById('dual-track-view-hint');
  if (hintEl && viewHint) hintEl.textContent = viewHint;
  if (data.granularity_note) {
    parts.push(`<p class="hint">${escHtml(data.granularity_note)}</p>`);
  }
  if (data.production_note) {
    parts.push(`<p class="hint dual-track-prod-note">${escHtml(data.production_note)}</p>`);
  }
  const cov = data.detail?.coverage || data.detail?.track_a_summary;
  if (cov && step !== 'compare' && step !== 'fusion') {
    parts.push(
      `<p>参照单元 ${cov.profile_count ?? '—'} · 已分配原子 ${cov.assigned_atoms ?? '—'} · 未匹配 ${cov.leftover_count ?? '—'}</p>`,
    );
  }
  if (step === 'textbook' || step === 'courseware') {
    const track = data.detail || {};
    parts.push(renderUnassignedFollowupHtml(
      track.unassigned_followup,
      track.borderline_followup,
    ));
  }
  if (step === 'compare' && data.detail?.compare) {
    const cmp = data.detail.compare;
    const sum = cmp.summary || {};
    parts.push(
      `<p><strong>按旧块编码对比</strong>：一致原子位 ${sum.agree_atom_assignments ?? 0} · 分歧位 ${sum.disagree_atom_slots ?? 0}</p>`,
    );
    parts.push('<table class="dual-track-compare-table"><thead><tr>'
      + '<th>旧块</th><th>教材轨</th><th>课件轨</th><th>一致</th></tr></thead><tbody>');
    for (const row of cmp.by_old_block || []) {
      const tbN = (row.textbook_atoms || []).length;
      const cwN = (row.courseware_atoms || []).length;
      const agN = (row.agree_atoms || []).length;
      parts.push(
        `<tr><td>${escHtml(row.old_block_code)}</td>`
        + `<td>${tbN} 原子</td><td>${cwN} 原子</td><td>${agN}</td></tr>`,
      );
    }
    parts.push('</tbody></table>');
    parts.push('<p class="hint">点①或②可在右侧查看对应轨道的预览块着色。</p>');
  }
  if (step === 'fusion' && data.detail?.diff_vs_current) {
    const diff = data.detail.diff_vs_current;
    parts.push(
      `<p><strong>与现网</strong>：现网 ${diff.current_block_count} 块 → 融合 ${diff.fused_block_count} 块</p>`,
    );
    const dup = diff.duplicate_atoms_in_current || {};
    const dupKeys = Object.keys(dup);
    if (dupKeys.length) {
      parts.push(`<p class="hint">现网重复绑定：${dupKeys.map((k) => `${k}→${dup[k].join(',')}`).join('；')}</p>`);
    }
    parts.push(renderUnassignedFollowupHtml(
      data.detail.unassigned_followup || data.detail.fusion?.unassigned_followup,
      null,
    ));
    const blTb = data.detail.borderline_textbook;
    const blCw = data.detail.borderline_courseware;
    if ((blTb?.count || 0) + (blCw?.count || 0) > 0) {
      parts.push(`<p><strong>边界已分配</strong>：教材轨 ${blTb?.count || 0} · 课件轨 ${blCw?.count || 0}（得分 &lt; ${blTb?.ceiling || 0.22}）</p>`);
    }
  }
  box.innerHTML = parts.join('');
  syncDualTrackStepButtons();
}

async function runDualTrackStep(step) {
  if (!LESSON_UID) return;
  if (step === 'live') {
    dualTrackPreview.step = 'live';
    dualTrackPreview.active = false;
    dualTrackPreview.blocks = [];
    dualTrackPreview.modeLabel = '';
    dualTrackPreview.lastCompare = null;
    renderDualTrackResult({ step: 'live' });
    renderBlocks();
    renderTextbook();
    toast('已恢复现网块（数据库）');
    return;
  }
  state._dualTrackBusy = true;
  syncDualTrackStepButtons();
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/dual-track/step`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ step }),
      },
    );
    const data = await readJson(r);
    if (!r.ok || data.ok === false) {
      if (r.status === 404) {
        throw new Error('双轨实验接口未找到（HTTP 404）。请重启开发服务器：.\\dev.ps1');
      }
      throw new Error(data.error || '双轨实验失败');
    }
    dualTrackPreview.step = step;
    dualTrackPreview.modeLabel = data.mode_label || '';
    if (step === 'compare') {
      dualTrackPreview.lastCompare = data.detail?.compare || null;
    } else if (data.preview_blocks) {
      dualTrackPreview.active = true;
      dualTrackPreview.blocks = data.preview_blocks;
    }
    renderDualTrackResult(data);
    renderBlocks();
    renderTextbook();
    scrollDualTrackResultsIntoView(step);
    toast(data.mode_label || '已更新预览');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    state._dualTrackBusy = false;
    syncDualTrackStepButtons();
  }
}

document.querySelectorAll('.dual-track-step-btn').forEach((btn) => {
  btn.addEventListener('click', () => runDualTrackStep(btn.dataset.dualStep));
});

document.getElementById('dual-track-panel')?.addEventListener('toggle', async (ev) => {
  if (ev.target.open) state = { ...state, _dualTrackPanelOpen: true };
  if (!ev.target.open || dualTrackPreview._metaLoaded) return;
  dualTrackPreview._metaLoaded = true;
  try {
    const r = await fetch(
      `${API}/lessons/${encodeURIComponent(LESSON_UID)}/dual-track/step`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ step: 'meta' }),
      },
    );
    const data = await readJson(r);
    if (r.ok && data.granularity_note) {
      const intro = document.getElementById('dual-track-intro');
      if (intro) {
        intro.innerHTML = `${escHtml(data.production_note || '')}<br>${escHtml(data.granularity_note)}`;
      }
    }
  } catch {
    /* ignore */
  }
});

load().catch((e) => {
  document.getElementById('page-sub').innerHTML =
    `<span class="annotate-meta-chip">${escHtml(e.message || String(e))}</span>`;
});
