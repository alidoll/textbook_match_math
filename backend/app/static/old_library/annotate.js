const LESSON_UID = window.ANNOTATE_LESSON_UID;
const API = window.ANNOTATE_API_PREFIX || '/api/old-library';

function pipelineProfile() {
  return state?.pipeline_profile || {};
}

function isPilotPipeline() {
  return !!pipelineProfile().enabled;
}

function ocrPhaseStats() {
  return state?.ocr_phase || {};
}

function ocrImagePhaseComplete() {
  const po = ocrPhaseStats();
  if (po.image_ocr_complete) return true;
  const tbOk = po.textbook_image_ocr_complete
    || (po.textbook_text_ocr_complete && !po.lesson_has_images);
  const slideOk = !po.slide_count || po.slide_image_ocr_complete;
  return !!(tbOk && slideOk);
}

function ocrTextPhaseComplete() {
  const po = ocrPhaseStats();
  const tbOk = !!po.textbook_text_ocr_complete || !!po.text_ocr_complete;
  const slideOk = !po.slide_count || !!po.slide_text_ocr_complete;
  return tbOk && slideOk;
}

let state = null;
let currentPageIndex = 1;
const selectedSlides = new Set();
const selectedAtoms = new Set();
/** @type {{ page_index: number, atoms: object[], blocks: object[] } | null} */
let undoSnapshot = null;

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

const INTAKE_FOCUS_KEY = 'old-library-intake-focus';
const INTAKE_RELOAD_CHANNEL = 'old-library-intake-reload';

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

  if ((window.UI_SHELL || '') === 'textbook_library') {
    const url = (window.INTAKE_RETURN_URL || '').trim();
    if (url.startsWith('/textbook-library/')) {
      window.location.href = url;
      return;
    }
    window.location.href = '/textbook-library/';
    return;
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

async function readJson(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`服务器返回异常（HTTP ${r.status}）`);
  }
}

function atomsOnPage(pageIndex) {
  return (state?.atoms || []).filter((a) => a.page_index === pageIndex);
}

function blockOccupancy() {
  const atomOwners = new Map();
  const slideOwners = new Map();
  for (const b of state?.blocks || []) {
    for (const code of b.atom_codes || []) {
      atomOwners.set(code, b);
    }
    for (const slide of b.course_slide_indices || []) {
      slideOwners.set(Number(slide), b);
    }
  }
  return { atomOwners, slideOwners };
}

function atomOwnerBlock(atomCode) {
  return blockOccupancy().atomOwners.get(atomCode) || null;
}

function slideOwnerBlock(slideIndex) {
  return blockOccupancy().slideOwners.get(Number(slideIndex)) || null;
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function sortedSlideIndices() {
  return (state?.courseware_slides || []).map((s) => s.slide_index).sort((a, b) => a - b);
}

function getAdjacentSlideIndex(current, delta) {
  const sorted = sortedSlideIndices();
  const idx = sorted.indexOf(current);
  if (idx < 0) return null;
  const next = sorted[idx + delta];
  return next != null ? next : null;
}

function isSlideLocked(slideIndex) {
  const owner = slideOwnerBlock(slideIndex);
  if (!owner) return false;
  return !canPickSlide(slideIndex);
}

function formatBlockBindLabel(block) {
  if (!block) return '未知区块';
  const title = blockDisplayTitle(block);
  return `${block.block_code} · ${title}`;
}

function blockDisplayTitle(block) {
  if (!block) return '未命名';
  const name = (block.block_name || '').trim();
  if (name) return name;
  const stage = (block.stage_ref || '').trim();
  if (stage) return stage;
  return '未命名';
}

function isSlideCheckboxChecked(slideIndex) {
  const n = Number(slideIndex);
  if (selectedSlides.has(n)) return true;
  const owner = slideOwnerBlock(n);
  return !!(owner && isSlideLocked(n));
}

function showSlideBoundModal(slideIndex) {
  const n = Number(slideIndex);
  const owner = slideOwnerBlock(n);
  const modal = document.getElementById('slide-bound-modal');
  const body = document.getElementById('slide-bound-modal-body');
  const gotoBtn = document.getElementById('slide-bound-modal-goto');
  if (!modal || !body) return;
  const crumb = formatBlockBindLabel(owner);
  body.innerHTML = `<p>课件 <strong>P${n}</strong> 已绑定到「${escHtml(crumb)}」区块。</p>
    <p class="hint">如需修改绑定关系，请前往对应区块的编辑模式操作。</p>`;
  if (gotoBtn) {
    gotoBtn.hidden = !owner;
    gotoBtn.onclick = () => {
      hideSlideBoundModal();
      if (owner) startBlockEdit(owner);
    };
  }
  modal.hidden = false;
}

function hideSlideBoundModal() {
  const modal = document.getElementById('slide-bound-modal');
  if (modal) modal.hidden = true;
}

function initSlideBoundModal() {
  const modal = document.getElementById('slide-bound-modal');
  if (!modal) return;
  modal.querySelectorAll('[data-modal-close]').forEach((el) => {
    el.addEventListener('click', hideSlideBoundModal);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && modal && !modal.hidden) hideSlideBoundModal();
  });
}

function normSlideIndex(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

function ensurePreviewSlideIndex() {
  const slides = state?.courseware_slides || [];
  if (!slides.length) {
    previewSlideIndex = null;
    return;
  }
  const current = normSlideIndex(previewSlideIndex);
  const exists = current != null && slides.some((s) => normSlideIndex(s.slide_index) === current);
  previewSlideIndex = exists ? current : normSlideIndex(slides[0].slide_index);
}

function scrollThumbIntoView(slideIndex) {
  const n = normSlideIndex(slideIndex);
  if (n == null) return;

  const applyScroll = () => {
    const strip = document.getElementById('slides-list');
    const thumb = document.getElementById(`thumb-${n}`);
    if (!strip || !thumb) return false;

    const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
    const stripRect = strip.getBoundingClientRect();
    const thumbRect = thumb.getBoundingClientRect();
    const thumbLeftInStrip = thumbRect.left - stripRect.left + strip.scrollLeft;
    const target = thumbLeftInStrip - (strip.clientWidth - thumbRect.width) / 2;
    strip.scrollLeft = Math.max(0, Math.min(target, maxScroll));
    return true;
  };

  applyScroll();
  requestAnimationFrame(applyScroll);
  setTimeout(applyScroll, 0);
  setTimeout(applyScroll, 120);
  setTimeout(applyScroll, 320);
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
      const targetTop = bRect.top - vRect.top + viewport.scrollTop - viewport.clientHeight * 0.2;
      const targetLeft = bRect.left - vRect.left + viewport.scrollLeft - viewport.clientWidth * 0.15;
      viewport.scrollTo({
        top: Math.max(0, targetTop),
        left: Math.max(0, targetLeft),
        behavior: 'smooth',
      });
    } else {
      box.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
    }
  });
}

function firstSlideForBlock(block) {
  const slides = (block?.course_slide_indices || [])
    .map((v) => normSlideIndex(v))
    .filter((n) => n != null);
  return slides.length ? Math.min(...slides) : null;
}

function firstPageForBlockAtoms(block) {
  const atomCodes = block?.atom_codes || [];
  if (!atomCodes.length) return null;
  const atoms = state?.atoms || [];
  let minPage = null;
  for (const atomCode of atomCodes) {
    const atom = atoms.find((x) => x.atom_code === atomCode);
    if (atom?.page_index == null) continue;
    minPage = minPage == null ? atom.page_index : Math.min(minPage, atom.page_index);
  }
  return minPage;
}

/** 跳转到区块绑定的首个课件页与教材页（在 render 前调用） */
function navigateToBlockContent(block) {
  if (!block) return false;
  const slide = firstSlideForBlock(block);
  if (slide != null) previewSlideIndex = slide;
  const page = firstPageForBlockAtoms(block);
  if (page != null && page !== currentPageIndex) {
    currentPageIndex = page;
    if (!editingBlockCode) selectedAtoms.clear();
    return true;
  }
  return false;
}

function syncBlockFocusChrome() {
  const active = !!highlightBlockCode();
  document.getElementById('slides-list')?.classList.toggle('block-focus-mode', active);
  document.getElementById('slide-preview-main')?.classList.toggle('block-focus-mode', active);
  document.getElementById('atom-layer')?.classList.toggle('block-focus-mode', active);
  document.getElementById('tb-viewport')?.classList.toggle('block-focus-mode', active);
}

function afterBlockFocusRender() {
  syncBlockFocusChrome();
  if (previewSlideIndex != null) scrollThumbIntoView(previewSlideIndex);
  scrollHighlightedAtomsIntoView();
}

function previewSlideGo(delta) {
  const next = getAdjacentSlideIndex(previewSlideIndex, delta);
  if (next == null) return;
  previewSlideIndex = next;
  renderSlides();
  scrollThumbIntoView(next);
}

function toggleSlidePick(slideIndex) {
  if (!manualReviewEnabled()) return;
  const n = Number(slideIndex);
  if (!canPickSlide(n)) {
    showSlideBoundModal(n);
    return;
  }
  if (selectedSlides.has(n)) selectedSlides.delete(n);
  else selectedSlides.add(n);
  if (editingBlockCode) refreshBlockEditUi();
  else {
    renderSlides();
    syncBlockFormUi();
  }
}

function canPickAtom(atomCode) {
  const owner = atomOwnerBlock(atomCode);
  if (!owner) return true;
  return editingBlockCode != null && owner.block_code === editingBlockCode;
}

function canPickSlide(slideIndex) {
  const owner = slideOwnerBlock(slideIndex);
  if (!owner) return true;
  return editingBlockCode != null && owner.block_code === editingBlockCode;
}

function blockOwnerLabel(block) {
  if (!block) return '';
  return `${block.block_code}·${blockDisplayTitle(block)}`;
}

function pruneLockedSelections() {
  for (const code of Array.from(selectedAtoms)) {
    if (!canPickAtom(code)) selectedAtoms.delete(code);
  }
  for (const slide of Array.from(selectedSlides)) {
    if (!canPickSlide(slide)) selectedSlides.delete(slide);
  }
}

function atomLabel(a) {
  const text = (a.content || '').replace(/\s+/g, ' ').trim();
  const preview = text.length > 18 ? `${text.slice(0, 18)}…` : text;
  return preview ? `${a.atom_code}：${preview}` : a.atom_code;
}

function formatAtomSelectionSummary(codes) {
  const sorted = Array.from(codes).sort();
  const n = sorted.length;
  const details = sorted.map((code) => {
    const a = (state?.atoms || []).find((x) => x.atom_code === code);
    return a ? atomLabel(a) : code;
  });
  const title = details.join('\n');
  let text;
  if (n <= 5) {
    text = `已选 ${n} 个：${sorted.join('、')}`;
  } else {
    text = `已选 ${n} 个：${sorted.slice(0, 4).join('、')}…`;
  }
  return { text, title };
}

function pageHasNumberGaps(pageIndex) {
  const nums = atomsOnPage(pageIndex)
    .map((a) => {
      const m = String(a.atom_code).match(/-(\d{3})$/);
      return m ? Number(m[1]) : null;
    })
    .filter((n) => n != null)
    .sort((a, b) => a - b);
  if (nums.length < 2) return false;
  return nums[nums.length - 1] - nums[0] + 1 !== nums.length;
}

function syncAtomActionButtons() {
  const n = selectedAtoms.size;
  const label = document.getElementById('atom-selection-label');
  const mergeBtn = document.getElementById('merge-atoms-btn');
  const unmergeBtn = document.getElementById('unmerge-ocr-btn');
  const deleteBtn = document.getElementById('delete-atoms-btn');
  const clearBtn = document.getElementById('clear-atoms-btn');
  const undoBtn = document.getElementById('undo-atoms-btn');
  if (label) {
    if (!n) {
      label.textContent = pageHasNumberGaps(currentPageIndex)
        ? '编号不连续（中间有隐藏占位条），改完后请点「保存本页」'
        : '点击教材页上的框多选原子；再点一次可取消选中';
      label.removeAttribute('title');
    } else {
      const summary = formatAtomSelectionSummary(selectedAtoms);
      label.textContent = summary.text;
      label.title = summary.title;
    }
  }
  const canEdit = atomEditEnabled();
  if (mergeBtn) mergeBtn.disabled = !canEdit || n < 2;
  if (unmergeBtn) {
    const code = n === 1 ? Array.from(selectedAtoms)[0] : null;
    const atom = code ? (state?.atoms || []).find((x) => x.atom_code === code) : null;
    const pageHasBaseline = (state?.textbook_pages || []).some(
      (p) => p.page_index === currentPageIndex && p.has_ocr_baseline,
    );
    unmergeBtn.disabled = !canEdit || !atom || !pageHasBaseline || !atom.can_unmerge_ocr;
    unmergeBtn.title = atom?.can_unmerge_ocr
      ? `拆回约 ${atom.ocr_source_count} 个 OCR 碎块`
      : '选中一个误合并的原子后可拆回 OCR';
  }
  if (deleteBtn) deleteBtn.disabled = !canEdit || n < 1;
  if (clearBtn) clearBtn.hidden = n < 1;
  if (undoBtn) undoBtn.disabled = !undoSnapshot;
}

function captureUndoSnapshot() {
  undoSnapshot = {
    page_index: currentPageIndex,
    atoms: atomsOnPage(currentPageIndex).map((a) => ({
      atom_code: a.atom_code,
      page_index: a.page_index,
      atom_type: a.atom_type,
      bbox: { ...(a.bbox || {}) },
      content: a.content || '',
      ocr_text: a.ocr_text || a.content || '',
    })),
    blocks: (state?.blocks || []).map((b) => ({
      block_code: b.block_code,
      atom_codes: [...(b.atom_codes || [])],
    })),
  };
  syncAtomActionButtons();
}

function clearUndoSnapshot() {
  undoSnapshot = null;
  syncAtomActionButtons();
}

function textbookPageIndexes() {
  return (state?.textbook_pages || []).map((p) => p.page_index).sort((a, b) => a - b);
}

function currentTextbookPagePosition() {
  const pages = textbookPageIndexes();
  const idx = pages.indexOf(currentPageIndex);
  return { pages, idx, total: pages.length };
}

function gotoTextbookPage(delta) {
  const { pages, idx } = currentTextbookPagePosition();
  if (idx < 0) return;
  const newIdx = idx + delta;
  if (newIdx < 0 || newIdx >= pages.length) return;
  gotoTextbookPageIndex(pages[newIdx]);
}

function gotoTextbookPageIndex(pageIndex) {
  currentPageIndex = pageIndex;
  if (!editingBlockCode) {
    selectedAtoms.clear();
  }
  renderTextbook();
  renderHeader();
  renderTextbookPageNav();
  syncAtomActionButtons();
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
    info.textContent = `旧教材第 ${currentPageIndex} 页${pdfNote} · ${idx + 1}/${total}`;
  }
  if (prevBtn) prevBtn.disabled = idx <= 0;
  if (nextBtn) nextBtn.disabled = idx >= total - 1;
}

let previewSlideIndex = null;
/** @type {string | null} */
let editingBlockCode = null;
/** @type {string | null} */
let focusedBlockCode = null;
/** @type {string | null} 形如 B05:atom / B05:slide */
let blockAddPickerOpen = null;
/** @type {string} 编辑中的区块知识点名称 */
let editingBlockName = '';
/** @type {string} 编辑中的环节参考 */
let editingStageRef = '';
const STAGE_REF_CUSTOM = '__custom__';

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
  if (editingBlockCode) {
    editingBlockName = v;
  }
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
  let html = '<option value="">— 选择环节 —</option>';
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

function renderBlockMetaReadonlyHtml(block) {
  const stage = (block.stage_ref || '').trim();
  const name = (block.block_name || '').trim();
  const intent = (block.metadata_json?.teaching_intent || '').trim();
  let html =
    `<div class="block-meta-rows">` +
    `<div class="block-meta-row">` +
    `<span class="block-meta-label">环节参考</span>` +
    `<span class="block-meta-value">${stage ? escHtml(stage) : '<span class="block-meta-empty">—</span>'}</span>` +
    `</div>` +
    `<div class="block-meta-row">` +
    `<span class="block-meta-label">区块名称</span>` +
    `<span class="block-meta-value block-item-name">${name ? escHtml(name) : '<span class="block-meta-empty">未命名</span>'}</span>` +
    `</div>`;
  if (intent) {
    html +=
      `<div class="block-meta-row block-meta-row-intent">` +
      `<span class="block-meta-label">教学意图</span>` +
      `<span class="block-meta-value block-item-intent">${escHtml(intent)}</span>` +
      `</div>`;
  }
  html += `</div>`;
  return html;
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
    `<span class="block-meta-label">环节参考</span>` +
    `<div class="block-stage-edit-col">` +
    `<select class="block-stage-inline" data-block-stage-select="${block.block_code}">` +
    renderBlockStageOptionsHtml(editingStageRef) +
    `</select>` +
    `<input type="text" class="block-stage-custom" data-block-stage-custom="${block.block_code}" ` +
    `value="${escHtml(isCustom ? stage : '')}" placeholder="输入自设环节" ${isCustom ? '' : 'hidden'} />` +
    `</div>` +
    `</div>` +
    `<div class="block-meta-row block-meta-row-name">` +
    `<span class="block-meta-label">区块名称</span>` +
    `<div class="block-name-edit-col">` +
    `<input type="text" class="block-name-inline" data-block-name-input="${block.block_code}" ` +
    `value="${escHtml(editingBlockName)}" placeholder="如：题目推拉游戏" />` +
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

function blockDisplayBindings(block) {
  const editing = editingBlockCode === block.block_code;
  if (editing) {
    const atoms = Array.from(selectedAtoms).sort();
    const slides = Array.from(selectedSlides).map((n) => Number(n)).filter((n) => n > 0).sort((a, b) => a - b);
    return { atoms, slides, editing: true };
  }
  const atoms = [...(block.atom_codes || [])];
  const slides = [...(block.course_slide_indices || [])].map(Number).filter((n) => n > 0).sort((a, b) => a - b);
  return { atoms, slides, editing: false };
}

function addableAtomsForEdit() {
  const occ = blockOccupancy();
  const out = [];
  for (const a of state?.atoms || []) {
    const code = a.atom_code;
    if (!code || selectedAtoms.has(code)) continue;
    const owner = occ.atomOwners.get(code);
    if (!owner) {
      out.push(code);
      continue;
    }
    if (editingBlockCode && owner.block_code === editingBlockCode) {
      out.push(code);
    }
  }
  return out.sort();
}

function addableSlidesForEdit() {
  return (state?.courseware_slides || [])
    .map((s) => normSlideIndex(s.slide_index))
    .filter((n) => n != null && !selectedSlides.has(n) && canPickSlide(n))
    .sort((a, b) => a - b);
}

function refreshBlockEditUi() {
  renderBlocks();
  renderSlides();
  if (editingBlockCode) paintAtomLayer();
  syncBlockFormUi();
}

function renderBlockBindingsHtml(block) {
  const { atoms, slides, editing } = blockDisplayBindings(block);
  const chipReadonly = (text) =>
    `<span class="block-chip block-chip-readonly">${escHtml(text)}</span>`;
  const chipRemovable = (text, kind, value) =>
    `<span class="block-chip">` +
    `<span class="block-chip-text">${escHtml(text)}</span>` +
    `<button type="button" class="block-chip-remove" data-rm-${kind}="${escHtml(String(value))}" ` +
    `aria-label="移除 ${escHtml(text)}">×</button></span>`;

  const atomChips = atoms.length
    ? atoms.map((c) => (editing ? chipRemovable(c, 'atom', c) : chipReadonly(c))).join('')
    : (editing ? '' : '<span class="block-bind-empty">—</span>');

  const slideChips = slides.length
    ? slides.map((n) => (editing ? chipRemovable(String(n), 'slide', n) : chipReadonly(String(n)))).join('')
    : (editing ? '' : '<span class="block-bind-empty">—</span>');

  let atomAdd = '';
  let slideAdd = '';
  if (editing) {
    const atomPickerKey = `${block.block_code}:atom`;
    const slidePickerKey = `${block.block_code}:slide`;
    const addableAtoms = addableAtomsForEdit();
    const addableSlides = addableSlidesForEdit();
    atomAdd =
      `<button type="button" class="block-chip-add" data-add-picker="atom" data-block="${block.block_code}" ` +
      `title="添加原子" aria-label="添加原子">+</button>` +
      (blockAddPickerOpen === atomPickerKey
        ? `<div class="block-add-menu">` +
          (addableAtoms.length
            ? addableAtoms.map((c) =>
              `<button type="button" class="block-add-opt" data-pick-atom="${escHtml(c)}">${escHtml(c)}</button>`,
            ).join('')
            : '<span class="block-add-empty">暂无可添加原子（可在左侧点选）</span>') +
          `</div>`
        : '');
    slideAdd =
      `<button type="button" class="block-chip-add" data-add-picker="slide" data-block="${block.block_code}" ` +
      `title="添加课件页" aria-label="添加课件页">+</button>` +
      (blockAddPickerOpen === slidePickerKey
        ? `<div class="block-add-menu">` +
          (addableSlides.length
            ? addableSlides.map((n) =>
              `<button type="button" class="block-add-opt" data-pick-slide="${n}">P${n}</button>`,
            ).join('')
            : '<span class="block-add-empty">暂无可添加课件（可在左侧勾选）</span>') +
          `</div>`
        : '');
  }

  return (
    `<div class="block-bindings" data-block-bindings="${block.block_code}">` +
    `<div class="block-bind-row">` +
    `<span class="block-bind-label">原子</span>` +
    `<div class="block-bind-chips">${atomChips}${atomAdd}</div>` +
    `</div>` +
    `<div class="block-bind-row">` +
    `<span class="block-bind-label">课件</span>` +
    `<div class="block-bind-chips">${slideChips}${slideAdd}</div>` +
    `</div>` +
    `</div>`
  );
}

function bindBlockBindingEvents(root) {
  root.querySelectorAll('[data-rm-atom]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      selectedAtoms.delete(btn.dataset.rmAtom);
      refreshBlockEditUi();
    };
  });
  root.querySelectorAll('[data-rm-slide]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      selectedSlides.delete(Number(btn.dataset.rmSlide));
      refreshBlockEditUi();
    };
  });
  root.querySelectorAll('[data-add-picker]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const key = `${btn.dataset.block}:${btn.dataset.addPicker}`;
      blockAddPickerOpen = blockAddPickerOpen === key ? null : key;
      renderBlocks();
    };
  });
  root.querySelectorAll('[data-pick-atom]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      selectedAtoms.add(btn.dataset.pickAtom);
      blockAddPickerOpen = null;
      refreshBlockEditUi();
    };
  });
  root.querySelectorAll('[data-pick-slide]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      selectedSlides.add(Number(btn.dataset.pickSlide));
      blockAddPickerOpen = null;
      refreshBlockEditUi();
    };
  });
}

function highlightBlockCode() {
  return editingBlockCode || focusedBlockCode;
}

function isSlideInHighlightedBlock(slideIndex) {
  const code = highlightBlockCode();
  if (!code) return false;
  const owner = slideOwnerBlock(slideIndex);
  return owner?.block_code === code;
}

function isAtomInHighlightedBlock(atomCode) {
  const code = highlightBlockCode();
  if (!code) return false;
  const owner = atomOwnerBlock(atomCode);
  return owner?.block_code === code;
}

function focusBlock(block) {
  if (!block) return;
  focusedBlockCode = block.block_code;
  navigateToBlockContent(block);
  renderSlides();
  renderTextbook();
  renderTextbookPageNav();
  renderHeader();
  renderBlocks();
  afterBlockFocusRender();
}

function clearBlockFocus() {
  focusedBlockCode = null;
  renderSlides();
  renderTextbook();
  renderTextbookPageNav();
  renderHeader();
  renderBlocks();
  afterBlockFocusRender();
}

function clearBlockEditMode() {
  editingBlockCode = null;
  focusedBlockCode = null;
  editingBlockName = '';
  editingStageRef = '';
  blockAddPickerOpen = null;
  syncBlockFormUi();
}

function startBlockEdit(block) {
  if (blocksLocked()) {
    toast('本课区块已锁定，请先点「解锁区块」后再编辑');
    return;
  }
  editingBlockCode = block.block_code;
  focusedBlockCode = block.block_code;
  blockAddPickerOpen = null;
  editingBlockName = (block.block_name || '').trim();
  editingStageRef = (block.stage_ref || '').trim();
  const nameInp = document.getElementById('block-name');
  if (nameInp) nameInp.value = editingBlockName;
  selectedAtoms.clear();
  selectedSlides.clear();
  for (const code of block.atom_codes || []) selectedAtoms.add(code);
  for (const slide of block.course_slide_indices || []) {
    selectedSlides.add(Number(slide));
  }
  navigateToBlockContent(block);
  syncBlockFormUi();
  renderBlockStageRefOptions();
  renderSlides();
  renderTextbook();
  renderBlocks();
  renderTextbookPageNav();
  renderHeader();
  afterBlockFocusRender();
  toast(`正在编辑 ${block.block_code}，修改后点「保存修改」`);
}

function blocksLocked() {
  return !!(state?.lesson?.blocks_locked);
}

/** AI 建块已有结果且未锁定 → 进入人工核对阶段 */
function manualReviewEnabled() {
  return (state?.stats?.block_count || 0) > 0 && !blocksLocked();
}

/** 原子选择/合并/删除等操作：有原子即可，不强制要求已建块。 */
function atomEditEnabled() {
  const hasUsableAtoms = (state?.atoms || []).some((a) => {
    const t = (a.content || a.ocr_text || '').trim();
    if (!t || t.startsWith('[未拆分') || t.startsWith('[整页未识别') || t === '[整页图像]') return false;
    const code = a.atom_code || '';
    return !code.includes('-GAP-') && !code.endsWith('-FULL-001');
  });
  return hasUsableAtoms && !blocksLocked();
}

function blocksMissingAtomBinding() {
  const blocks = state?.blocks || [];
  if (!blocks.length) return false;
  const hasUsableAtoms = (state?.atoms || []).some((a) => {
    const t = (a.content || a.ocr_text || '').trim();
    if (!t || t.startsWith('[未拆分') || t.startsWith('[整页未识别') || t === '[整页图像]') return false;
    const code = a.atom_code || '';
    return !code.includes('-GAP-') && !code.endsWith('-FULL-001');
  });
  if (!hasUsableAtoms) return false;
  return blocks.every((b) => !(b.atom_codes || []).length);
}

function syncBlockFormUi() {
  const locked = blocksLocked();
  const manual = manualReviewEnabled();
  const blocks = state?.blocks || [];
  const btn = document.getElementById('create-block-btn');
  const cancelBtn = document.getElementById('cancel-block-edit-btn');
  const hint = document.getElementById('block-form-hint');
  const navHint = document.getElementById('blocks-nav-hint');
  const formWrap = document.getElementById('block-form-wrap');
  const lockBtn = document.getElementById('lock-blocks-btn');
  const unlockBtn = document.getElementById('unlock-blocks-btn');
  const lockedBadge = document.getElementById('blocks-locked-badge');
  const slideN = selectedSlides.size;
  const atomN = selectedAtoms.size;
  if (lockedBadge) lockedBadge.hidden = !locked;
  if (formWrap) formWrap.hidden = locked || !manual;
  const stageSel = document.getElementById('block-stage-ref');
  const stageCustom = document.getElementById('block-stage-custom');
  const nameInp = document.getElementById('block-name');
  if (nameInp) nameInp.hidden = !!editingBlockCode;
  if (stageSel) stageSel.hidden = !!editingBlockCode;
  if (stageCustom) stageCustom.hidden = !!editingBlockCode || stageSel?.value !== STAGE_REF_CUSTOM;
  if (lockBtn) lockBtn.hidden = locked || !blocks.length;
  if (unlockBtn) unlockBtn.hidden = !locked;
  if (btn) {
    if (locked) {
      btn.hidden = true;
    } else if (editingBlockCode) {
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
        ? `已锁定 ${blocks.length} 个区块，不可编辑。需调整时点「解锁区块」。`
        : '尚无区块';
    } else if (!manual) {
      navHint.textContent = '请先点顶部 ① OCR → ② 整理 → ③ 建块（或「一键全流程」）；建块后可在此核对、编辑区块';
    } else if (editingBlockCode) {
      const stage = getBlockFormStageRef();
      const name = getBlockFormName();
      const parts = [];
      if (stage) parts.push(`环节「${stage}」`);
      if (name) parts.push(`名称「${name}」`);
      const label = parts.length ? parts.join(' · ') : '请填写环节或名称';
      navHint.textContent = `编辑 ${editingBlockCode} · ${label} · 课件 ${slideN} 页 · 原子 ${atomN} 个`;
    } else if (blocksMissingAtomBinding()) {
      navHint.textContent = '部分区块未绑定原子，可点区块「编辑」调整绑定';
    } else if (slideN || atomN) {
      navHint.textContent = `待创建 · 已选课件 ${slideN} 页 · 原子 ${atomN} 个`;
    } else {
      navHint.textContent = '核对无误后点顶部「保存」锁定本课';
    }
  }
  if (cancelBtn) cancelBtn.hidden = locked || !manual || !editingBlockCode;
  const deleteAllBtn = document.getElementById('delete-all-blocks-btn');
  if (deleteAllBtn) {
    deleteAllBtn.hidden = locked || !blocks.length || !!editingBlockCode;
  }
  if (hint) {
    hint.hidden = locked || !manual;
    if (!locked) {
      hint.textContent = editingBlockCode
        ? `编辑 ${editingBlockCode}：分别设置环节参考与区块名称；绿框=保留，橙虚线=将移除。`
        : '选择环节参考、填写区块名称（知识点）；勾选课件与原子后创建。';
    }
  }
}

function renderSlidePreview() {
  const main = document.getElementById('slide-preview-main');
  if (!main) return;
  const slides = state?.courseware_slides || [];
  if (!slides.length) {
    previewSlideIndex = null;
    main.classList.remove('has-slide');
    main.innerHTML = '<p class="hint">暂无课件，请返回课时接入页上传 ZIP</p>';
    return;
  }
  if (
    previewSlideIndex == null
    || !slides.some((s) => normSlideIndex(s.slide_index) === normSlideIndex(previewSlideIndex))
  ) {
    previewSlideIndex = normSlideIndex(slides[0].slide_index);
  }
  ensurePreviewSlideIndex();
  const slide = slides.find((s) => normSlideIndex(s.slide_index) === normSlideIndex(previewSlideIndex));
  if (!slide?.url) {
    main.classList.remove('has-slide');
    main.innerHTML = `<p class="hint">课件 P${previewSlideIndex} 无图</p>`;
    return;
  }
  const page = slide.slide_index;
  const prevPg = getAdjacentSlideIndex(page, -1);
  const nextPg = getAdjacentSlideIndex(page, 1);
  const locked = isSlideLocked(page);
  const canPick = canPickSlide(page);
  const picked = isSlideCheckboxChecked(page);
  const highlighted = isSlideInHighlightedBlock(page);
  const pickCls = `preview-cw-pick-wrap${picked || highlighted ? ' is-picked' : ''}${locked ? ' is-locked' : ''}`;
  const chkChk = picked ? 'checked' : '';
  const chkDisabled = canPick ? '' : 'disabled';
  const previewDeny = locked
    ? '<button type="button" class="preview-cw-deny-hit" aria-label="该课件页已被绑定"></button>'
    : '';
  const navBtns = `
    <button type="button" class="preview-nav-arrow preview-nav-prev" ${prevPg == null ? 'disabled' : ''} aria-label="上一页">
      <span class="preview-nav-arr">◀</span><span class="preview-nav-lbl">上一页</span></button>
    <button type="button" class="preview-nav-arrow preview-nav-next" ${nextPg == null ? 'disabled' : ''} aria-label="下一页">
      <span class="preview-nav-arr">▶</span><span class="preview-nav-lbl">下一页</span></button>`;

  main.classList.add('has-slide');
  main.innerHTML = `
    <div class="preview-big-inner">
      ${navBtns}
      <div class="${pickCls}">
        <button type="button" class="preview-cw-lock-btn" title="已绑定，点击查看" aria-label="已绑定">🔒</button>
        <input type="checkbox" id="preview-slide-pick-cb" data-slide-pick="${page}" ${chkChk} ${chkDisabled}
          aria-label="选中本页课件">
        <label for="preview-slide-pick-cb">选中本页课件</label>
        ${previewDeny}
      </div>
      <div class="slide-preview-label">课件 P${page}</div>
      <img src="${slide.url}" alt="P${page}">
    </div>`;

  const prevBtn = main.querySelector('.preview-nav-prev');
  const nextBtn = main.querySelector('.preview-nav-next');
  if (prevBtn && prevPg != null) prevBtn.onclick = () => previewSlideGo(-1);
  if (nextBtn && nextPg != null) nextBtn.onclick = () => previewSlideGo(1);

  const pickCb = main.querySelector('[data-slide-pick]');
  if (pickCb) {
    pickCb.addEventListener('change', () => toggleSlidePick(page));
  }
  const lockBtn = main.querySelector('.preview-cw-lock-btn');
  if (lockBtn) {
    lockBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      showSlideBoundModal(page);
    });
  }
  const denyBtn = main.querySelector('.preview-cw-deny-hit');
  if (denyBtn) {
    denyBtn.addEventListener('click', (ev) => {
      ev.preventDefault();
      showSlideBoundModal(page);
    });
  }
  const pickLabel = main.querySelector('.preview-cw-pick-wrap label');
  if (pickLabel && canPick) {
    pickLabel.addEventListener('click', (ev) => {
      if (ev.target === pickCb) return;
      ev.preventDefault();
      toggleSlidePick(page);
    });
  }
  const bigImg = main.querySelector('.preview-big-inner img');
  if (bigImg) {
    bigImg.classList.toggle('slide-pickable', canPick);
    bigImg.onclick = canPick
      ? () => toggleSlidePick(page)
      : () => showSlideBoundModal(page);
  }
}

function renderSlides() {
  renderSlidePreview();
  const root = document.getElementById('slides-list');
  const slides = state?.courseware_slides || [];
  syncBlockFocusChrome();
  if (!slides.length) {
    root.innerHTML = '';
    return;
  }
  const focusActive = !!highlightBlockCode();
  root.innerHTML = slides.map((s) => {
    const n = normSlideIndex(s.slide_index);
    if (n == null) return '';
    const previewing = normSlideIndex(previewSlideIndex) === n;
    const owner = slideOwnerBlock(n);
    const bound = !!owner;
    const locked = isSlideLocked(n);
    const picked = isSlideCheckboxChecked(n);
    const highlighted = isSlideInHighlightedBlock(n);
    const canPick = canPickSlide(n);
    const classes = [
      'thumb-item',
      previewing ? 'previewing' : '',
      bound ? 'bound' : '',
      locked ? 'locked' : '',
      highlighted ? 'editing-bound' : '',
      focusActive && !highlighted ? 'dimmed' : '',
      picked || highlighted ? 'picked' : '',
    ].filter(Boolean).join(' ');
    const img = s.url
      ? `<img src="${s.url}" alt="P${n}" loading="lazy">`
      : '<div class="hint">无图</div>';
    const cbChk = isSlideCheckboxChecked(n) ? 'checked' : '';
    const cbDisabled = canPick ? '' : 'disabled';
    return `<div class="${classes}" id="thumb-${n}">
      <div class="thumb-inner" data-slide-preview="${n}" title="点击放大预览">
        ${img}
        <span class="thumb-bound-badge" title="已绑定到区块">✓</span>
      </div>
      <div class="thumb-pick-wrap">
        <input type="checkbox" class="thumb-cb" data-thumb-pick="${n}" ${cbChk} ${cbDisabled}
          title="${locked ? '该课件页已被绑定' : '勾选本页课件'}" aria-label="选中课件 P${n}">
        <button type="button" class="thumb-lock-btn" data-slide-lock="${n}" title="已绑定，点击查看">🔒</button>
      </div>
      <div class="thumb-label">P${n}</div>
    </div>`;
  }).filter(Boolean).join('');

  root.querySelectorAll('[data-slide-preview]').forEach((el) => {
    el.onclick = () => {
      const n = Number(el.dataset.slidePreview);
      previewSlideIndex = n;
      renderSlides();
      scrollThumbIntoView(n);
    };
  });
  root.querySelectorAll('[data-thumb-pick]').forEach((inp) => {
    inp.addEventListener('change', (ev) => {
      ev.stopPropagation();
      const n = Number(inp.dataset.thumbPick);
      if (!canPickSlide(n)) {
        inp.checked = isSlideCheckboxChecked(n);
        showSlideBoundModal(n);
        return;
      }
      if (inp.checked) selectedSlides.add(n);
      else selectedSlides.delete(n);
      if (editingBlockCode) refreshBlockEditUi();
      else {
        renderSlides();
        syncBlockFormUi();
      }
    });
  });
  root.querySelectorAll('[data-slide-lock]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      showSlideBoundModal(Number(btn.dataset.slideLock));
    });
  });

  if (previewSlideIndex != null) {
    scrollThumbIntoView(previewSlideIndex);
  }
}

function atomBoxSortKey(atom) {
  const owner = atomOwnerBlock(atom.atom_code);
  const highlightingOwn = isAtomInHighlightedBlock(atom.atom_code);
  const locked = owner && !highlightingOwn;
  if (locked) return 0;
  if (selectedAtoms.has(atom.atom_code) || highlightingOwn) return 2;
  return 1;
}

function toggleAtomPick(atomCode) {
  if (!atomEditEnabled()) return;
  const code = String(atomCode || '').trim();
  if (!code) return;
  const owner = atomOwnerBlock(code);
  if (owner && !(editingBlockCode && owner.block_code === editingBlockCode)) {
    const slides = owner.course_slide_indices || [];
    if (slides.length) {
      previewSlideIndex = Number(slides[0]);
      renderSlides();
    }
    toast(`原子 ${code} 已在区块 ${blockOwnerLabel(owner)}，请先编辑该区块`);
    return;
  }
  if (selectedAtoms.has(code)) selectedAtoms.delete(code);
  else selectedAtoms.add(code);
  if (editingBlockCode) refreshBlockEditUi();
  else {
    paintAtomLayer();
    syncAtomActionButtons();
    syncBlockFormUi();
  }
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
  layer.innerHTML = pageAtoms.map((a) => {
    const b = a.bbox || {};
    const owner = atomOwnerBlock(a.atom_code);
    const highlightingOwn = isAtomInHighlightedBlock(a.atom_code);
    const locked = owner && !highlightingOwn ? ' locked' : '';
    const sel = (selectedAtoms.has(a.atom_code) || (highlightingOwn && !editingBlockCode))
      ? ' selected'
      : '';
    const pendingRemove = editingBlockCode && highlightingOwn && !selectedAtoms.has(a.atom_code)
      ? ' pending-remove'
      : '';
    const editingOwnCls = highlightingOwn ? ' editing-own' : '';
    const preview = (a.content || '').replace(/\s+/g, ' ').trim().slice(0, 12);
    const ownerNote = owner ? ` · 已在 ${blockOwnerLabel(owner)}` : '';
    const unmergeNote = a.can_unmerge_ocr ? ' · 双击拆回 OCR' : '';
    const title = `${a.atom_code}${preview ? ` ${preview}` : ''}${ownerNote}${unmergeNote}`;
    const unmergeCls = a.can_unmerge_ocr ? ' can-unmerge' : '';
    return `<div class="atom-box${sel}${locked}${pendingRemove}${editingOwnCls}${unmergeCls}" data-atom="${a.atom_code}" title="${title.replace(/"/g, '&quot;')}" style="left:${b.x_start * 100}%;top:${b.y_start * 100}%;width:${(b.x_end - b.x_start) * 100}%;height:${(b.y_end - b.y_start) * 100}%"><span class="atom-tag">${a.atom_code}</span></div>`;
  }).join('');
  syncAtomActionButtons();
  applyAtomTagVisibility();
  if (highlightBlockCode()) scrollHighlightedAtomsIntoView();
}

function renderTextbook() {
  const page = (state?.textbook_pages || []).find((p) => p.page_index === currentPageIndex);
  const img = document.getElementById('tb-image');
  const layer = document.getElementById('atom-layer');
  if (!page?.url) {
    if (img) img.hidden = true;
    if (layer) layer.innerHTML = '<p class="hint">本页无教材图</p>';
    return;
  }
  img.hidden = false;
  const url = page.url;
  const onReady = () => paintAtomLayer();
  img.onload = onReady;
  if (img.getAttribute('src') !== url) {
    img.src = url;
  } else if (img.complete) {
    onReady();
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
    if (prevEdit && d.code_remap?.[prevEdit]) {
      editingBlockCode = d.code_remap[prevEdit];
      syncBlockFormUi();
    }
    renderBlocks();
    renderSlides();
    renderTextbook();
    toast(delta < 0 ? '已上移' : '已下移');
  } catch (e) {
    toast(e.message || String(e));
  }
}

function renderBlocks() {
  const root = document.getElementById('blocks-list');
  const blocks = state?.blocks || [];
  const locked = blocksLocked();
  const manual = manualReviewEnabled();
  const countEl = document.getElementById('blocks-count');
  if (countEl) {
    countEl.textContent = blocks.length ? String(blocks.length) : '';
    countEl.hidden = !blocks.length;
  }
  if (!blocks.length) {
    root.innerHTML = '<li class="blocks-empty">尚无区块，请先点顶部 ③ 建块或「一键全流程」</li>';
    return;
  }
  root.innerHTML = blocks.map((b, idx) => {
    const editing = editingBlockCode === b.block_code;
    const focused = !editing && focusedBlockCode === b.block_code;
    const upDisabled = idx <= 0 || !manual;
    const downDisabled = idx >= blocks.length - 1 || !manual;
    const rowLocked = locked || !manual;
    return `
    <li class="${editing ? 'block-editing' : ''}${focused ? ' block-focused' : ''}${rowLocked ? ' block-row-locked' : ''}" data-block-code="${b.block_code}">
      ${manual ? `<div class="block-card-move-btns">
        <button type="button" title="上移" data-move-block="${b.block_code}" data-move-delta="-1" ${upDisabled ? 'disabled' : ''}>▲</button>
        <button type="button" title="下移" data-move-block="${b.block_code}" data-move-delta="1" ${downDisabled ? 'disabled' : ''}>▼</button>
      </div>` : ''}
      <div class="block-item-main" data-focus-block="${b.block_code}" title="点击查看本区块绑定的课件与原子">
        <div class="block-item-title"><strong>${b.block_code}</strong></div>
        ${editing ? renderBlockMetaEditHtml(b) : renderBlockMetaReadonlyHtml(b)}
        ${renderBlockBindingsHtml(b)}
      </div>
      ${manual ? `<div class="block-actions">
        <button type="button" class="btn-link" data-edit-block="${b.block_code}">${editing ? '编辑中…' : '编辑'}</button>
        <button type="button" class="btn-link danger" data-del-block="${b.block_code}">删除</button>
      </div>` : ''}
    </li>`;
  }).join('');
  root.querySelectorAll('[data-move-block]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      if (btn.disabled) return;
      reorderBlock(btn.dataset.moveBlock, Number(btn.dataset.moveDelta));
    };
  });
  root.querySelectorAll('[data-focus-block]').forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest('button, input, select, textarea, [data-edit-block], [data-del-block], [data-move-block]')) return;
      const code = el.dataset.focusBlock;
      const block = (state?.blocks || []).find((x) => x.block_code === code);
      if (!block) return;
      if (editingBlockCode) return;
      if (focusedBlockCode === code) {
        clearBlockFocus();
        return;
      }
      focusBlock(block);
    };
  });
  root.querySelectorAll('[data-edit-block]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      if (locked || !manual) return;
      const code = btn.dataset.editBlock;
      const block = (state?.blocks || []).find((x) => x.block_code === code);
      if (!block) return;
      if (editingBlockCode === code) return;
      startBlockEdit(block);
    };
  });
  root.querySelectorAll('[data-del-block]').forEach((btn) => {
    btn.onclick = async (ev) => {
      ev.stopPropagation();
      if (blocksLocked()) {
        toast('本课区块已锁定');
        return;
      }
      if (!confirm(`删除区块 ${btn.dataset.delBlock}？`)) return;
      const deleted = btn.dataset.delBlock;
      const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(deleted)}`, { method: 'DELETE' });
      const d = await readJson(r);
      if (!r.ok || !d.ok) throw new Error(d.error || '删除失败');
      state = d;
      if (editingBlockCode === deleted) {
        editingBlockCode = null;
        focusedBlockCode = null;
        editingBlockName = '';
        editingStageRef = '';
        selectedAtoms.clear();
        selectedSlides.clear();
        document.getElementById('block-name').value = '';
      }
      if (focusedBlockCode === deleted) focusedBlockCode = null;
      renderAll();
      syncBlockFormUi();
      toast('已删除区块');
    };
  });
  bindBlockBindingEvents(root);
  bindBlockMetaEditEvents(root);
}

function renderHeader() {
  const les = state?.lesson || {};
  const st = state?.stats || {};
  const pageAtoms = atomsOnPage(currentPageIndex).length;
  const pageHidden = state?.page_hidden_placeholder_counts?.[currentPageIndex] || 0;
  const lessonAtoms = st.atom_count || 0;
  const chips = [
    les.volume_code,
    `课件 ${st.slide_count || 0} 张`,
    `教材 ${st.page_count || 0} 页`,
    `本页 ${pageAtoms} 个原子`,
    pageHidden ? `隐藏占位 ${pageHidden}` : '',
    `全课 ${lessonAtoms} 个原子`,
    `区块 ${st.block_count || 0}`,
  ].filter(Boolean);
  document.getElementById('page-sub').innerHTML = chips
    .map((c) => `<span class="annotate-meta-chip">${escHtml(c)}</span>`)
    .join('');
  const colTitle = document.querySelector('#textbook-col .col-head h2');
  if (colTitle) {
    colTitle.textContent = `教材页 + 原子（本页 ${pageAtoms}）`;
  }
}

function renderAll() {
  if (blocksLocked()) {
    editingBlockCode = null;
    editingBlockName = '';
    editingStageRef = '';
    selectedAtoms.clear();
    selectedSlides.clear();
    const nameInp = document.getElementById('block-name');
    if (nameInp) nameInp.value = '';
  }
  pruneLockedSelections();
  renderHeader();
  renderTextbookPageNav();
  renderSlides();
  renderTextbook();
  renderBlockStageRefOptions();
  renderBlocks();
  syncBlockFormUi();
  syncBlockFocusChrome();
  syncPipelineProfileUi();
  syncPrematchReview();
  syncHeaderToolbar();
}

let prematchReviewTab = 'splits';

function prematchReviewData() {
  return state?.prematch_review || {};
}

function renderPrematchReviewBody(tab) {
  const body = document.getElementById('pipeline-review-body');
  if (!body) return;
  const review = prematchReviewData();
  const key = tab || prematchReviewTab;
  let rows = [];
  if (key === 'splits') {
    rows = review.splits || [];
    if (!rows.length && review.pending_plan?.splits?.length) {
      body.innerHTML = (review.pending_plan.splits || []).map((s) => (
        `<div class="pipeline-review-row"><strong>${escHtml(s.source_code)}</strong> 待拆`
        + ` → ${(s.line_hits || []).map((h) => `P${h.slide_index}`).join(', ')}</div>`
      )).join('');
      return;
    }
    body.innerHTML = rows.length
      ? rows.map((r) => (
        `<div class="pipeline-review-row"><strong>${escHtml(r.atom_code)}</strong> `
        + `${escHtml((r.content || '').slice(0, 48))} `
        + `<span class="hint">→ P${r.primary_slide || '?'}</span></div>`
      )).join('')
      : '<p class="pipeline-review-empty">暂无拆分记录</p>';
    return;
  }
  if (key === 'refs') {
    rows = review.image_refs || [];
    body.innerHTML = rows.length
      ? rows.map((r) => (
        `<div class="pipeline-review-row"><strong>${escHtml(r.atom_code)}</strong> `
        + `主 P${r.primary_slide} · 引用 P${(r.referenced_slides || []).join(', P')}</div>`
      )).join('')
      : '<p class="pipeline-review-empty">暂无插图引用</p>';
    return;
  }
  rows = review.image_groups || [];
  body.innerHTML = rows.length
    ? rows.map((g) => (
      `<div class="pipeline-review-row">${escHtml((g.atom_codes || []).join(', '))}</div>`
    )).join('')
    : '<p class="pipeline-review-empty">暂无并列图组</p>';
}

function syncPrematchReview() {
  const wrap = document.getElementById('pipeline-review-wrap');
  const statusEl = document.getElementById('prematch-review-status');
  if (!wrap) return;
  const pilot = isPilotPipeline();
  wrap.hidden = !pilot;
  if (!pilot) return;
  const review = prematchReviewData();
  const done = !!review.done;
  const report = review.report || {};
  if (statusEl) {
    statusEl.textContent = done
      ? `已完成 · 拆 ${report.split_count || 0} · 引用 ${report.image_ref_count || 0}`
      : '待执行（点 ③ 预匹配 或 一键全流程）';
  }
  renderPrematchReviewBody(prematchReviewTab);
}

function syncPipelineProfileUi() {
  const profile = pipelineProfile();
  const pilot = !!profile.enabled;
  const legacyOcr = document.getElementById('substep-ocr');
  const textOcr = document.getElementById('substep-ocr-text');
  const imageOcr = document.getElementById('substep-ocr-image');
  const curateBtn = document.getElementById('substep-curate');
  const prematchBtn = document.getElementById('substep-prematch');
  const seedBtn = document.getElementById('substep-seed');
  const badge = document.getElementById('pipeline-pilot-badge');
  const fullBtn = document.getElementById('full-pipeline-btn');
  if (legacyOcr) legacyOcr.hidden = pilot;
  if (textOcr) textOcr.hidden = !pilot;
  if (imageOcr) imageOcr.hidden = !pilot;
  if (curateBtn) curateBtn.hidden = pilot && profile.skip_curate;
  if (prematchBtn) prematchBtn.hidden = !pilot || profile.trust_llm_atoms;
  if (seedBtn && pilot) {
    seedBtn.textContent = '④ 建块';
    seedBtn.title = profile.trust_llm_atoms
      ? 'AI 建块（需先完成 OCR）'
      : 'AI 建块（需先完成 OCR + 课件预匹配）';
  }
  if (seedBtn && pilot && profile.skip_curate) {
    // keep ④ label
  } else if (seedBtn && pilot) {
    seedBtn.textContent = profile.skip_curate ? '④ 建块' : seedBtn.textContent;
  }
  if (badge) {
    if (pilot) {
      badge.hidden = false;
      badge.textContent = profile.label ? `试点：${profile.label}` : '试点：豆包 OCR · 跳过整理';
    } else {
      badge.hidden = true;
      badge.textContent = '';
    }
  }
  if (fullBtn && pilot) {
    fullBtn.title = profile.trust_llm_atoms
      ? '豆包文字 OCR → 图片 OCR → 建块（跳过版面整理）'
      : '豆包文字 OCR → 图片 OCR → 课件预匹配 → AI 建块（试点跳过版面整理）';
  }
  const phaseDetail = document.getElementById('ocr-phase-detail');
  if (phaseDetail) phaseDetail.hidden = !pilot;
}

function ocrSubProgressRatio(done, total, complete) {
  if (complete) return 1;
  const t = Number(total) || 0;
  const d = Number(done) || 0;
  if (t <= 0) return 1;
  return Math.min(1, d / t);
}

function setOcrPhaseRow(rowId, valueId, { text, status }) {
  const row = document.getElementById(rowId);
  const val = document.getElementById(valueId);
  if (val) val.textContent = text;
  if (row) {
    row.classList.remove('is-done', 'is-partial', 'is-pending');
    row.classList.add(status || 'is-pending');
  }
}

function syncOcrPhaseDetail() {
  const wrap = document.getElementById('ocr-phase-detail');
  if (!wrap || wrap.hidden) return;

  const phase = ocrPhaseStats();
  const busy = !!(state?._lessonExtractBusy || state?._bootstrapBusy);
  const pageCount = phase.page_count || 0;
  const slideCount = phase.slide_count || 0;

  const tbTextDone = !!phase.textbook_text_ocr_complete;
  const cwTextDone = !!phase.slide_text_ocr_complete || slideCount === 0;
  const tbImgDone = !!phase.textbook_image_ocr_complete;
  const cwImgDone = !!phase.slide_image_ocr_complete || slideCount === 0;

  setOcrPhaseRow('ocr-phase-tb-text-row', 'ocr-phase-tb-text', {
    text: pageCount ? `${phase.text_pages_done ?? 0}/${pageCount} 页${tbTextDone ? ' ✓' : ''}` : '—',
    status: tbTextDone ? 'is-done' : ((phase.text_pages_done || 0) > 0 ? 'is-partial' : 'is-pending'),
  });
  setOcrPhaseRow('ocr-phase-cw-text-row', 'ocr-phase-cw-text', {
    text: slideCount
      ? `${phase.slides_text_done ?? 0}/${slideCount} 张${cwTextDone ? ' ✓' : ''}`
      : '无课件',
    status: slideCount === 0 ? 'is-done' : (cwTextDone ? 'is-done' : ((phase.slides_text_done || 0) > 0 ? 'is-partial' : 'is-pending')),
  });

  let tbImgText;
  if (!pageCount) {
    tbImgText = '—';
  } else if (tbImgDone && !phase.lesson_has_images && (phase.image_atom_count || 0) === 0) {
    tbImgText = '无插图 ✓';
  } else {
    tbImgText = `${phase.image_atom_count ?? 0} 个 · ${phase.image_pages_done ?? 0}/${pageCount} 页${tbImgDone ? ' ✓' : ''}`;
  }
  setOcrPhaseRow('ocr-phase-tb-image-row', 'ocr-phase-tb-image', {
    text: tbImgText,
    status: tbImgDone ? 'is-done' : ((phase.image_atom_count || phase.image_pages_done || 0) > 0 ? 'is-partial' : 'is-pending'),
  });

  const regions = phase.slide_image_regions ?? 0;
  setOcrPhaseRow('ocr-phase-cw-image-row', 'ocr-phase-cw-image', {
    text: slideCount
      ? `${phase.slides_layout_done ?? 0}/${slideCount} 张 · ${regions} 区${cwImgDone ? ' ✓' : ''}`
      : '无课件',
    status: slideCount === 0 ? 'is-done' : (cwImgDone ? 'is-done' : ((phase.slides_layout_done || 0) > 0 ? 'is-partial' : 'is-pending')),
  });

  const warnEl = document.getElementById('ocr-phase-warnings');
  const warnings = state?._lastOcrWarnings || [];
  if (warnEl) {
    if (warnings.length && !busy) {
      warnEl.hidden = false;
      const preview = warnings.slice(0, 3).join(' · ');
      warnEl.textContent = warnings.length > 3
        ? `${preview} …（共 ${warnings.length} 条，见高级调试或重跑）`
        : preview;
    } else {
      warnEl.hidden = true;
      warnEl.textContent = '';
    }
  }
}

function pilotOcrOverallPercent() {
  const phase = ocrPhaseStats();
  const ratios = [
    ocrSubProgressRatio(phase.text_pages_done, phase.page_count, phase.textbook_text_ocr_complete),
    ocrSubProgressRatio(phase.slides_text_done, phase.slide_count, phase.slide_text_ocr_complete),
    ocrSubProgressRatio(phase.image_pages_done, phase.page_count, phase.textbook_image_ocr_complete),
    ocrSubProgressRatio(phase.slides_layout_done, phase.slide_count, phase.slide_image_ocr_complete),
  ];
  return Math.round((ratios.reduce((a, b) => a + b, 0) / 4) * 100);
}

/** 试点总进度：OCR 占 70%，建块占 30%（避免 OCR 完成就显示 100%） */
function pilotPipelineOverallPercent() {
  const phase = ocrPhaseStats();
  const blockCount = state?.stats?.block_count || 0;
  if (blockCount > 0 || blocksLocked() || manualReviewEnabled()) return 100;

  const profile = pipelineProfile();
  if (profile.trust_llm_atoms) {
    const textRatio = (
      ocrSubProgressRatio(phase.text_pages_done, phase.page_count, phase.textbook_text_ocr_complete)
      + ocrSubProgressRatio(phase.slides_text_done, phase.slide_count, phase.slide_text_ocr_complete)
    ) / 2;
    const imgRatio = (
      ocrSubProgressRatio(phase.image_pages_done, phase.page_count, phase.textbook_image_ocr_complete)
      + ocrSubProgressRatio(phase.slides_layout_done, phase.slide_count, phase.slide_image_ocr_complete)
    ) / 2;
    return Math.round(textRatio * 40 + imgRatio * 30);
  }

  const prematchDone = !!(state?.prematch_review?.done);
  const textRatio = (
    ocrSubProgressRatio(phase.text_pages_done, phase.page_count, phase.textbook_text_ocr_complete)
    + ocrSubProgressRatio(phase.slides_text_done, phase.slide_count, phase.slide_text_ocr_complete)
  ) / 2;
  const imgRatio = (
    ocrSubProgressRatio(phase.image_pages_done, phase.page_count, phase.textbook_image_ocr_complete)
    + ocrSubProgressRatio(phase.slides_layout_done, phase.slide_count, phase.slide_image_ocr_complete)
  ) / 2;
  let pct = textRatio * 32 + imgRatio * 26;
  if (prematchDone) pct += 20;
  return Math.round(pct);
}

function pilotOcrSummaryLabel() {
  const phase = ocrPhaseStats();
  if (phase.text_ocr_complete && phase.image_ocr_complete) {
    return (state?.stats?.block_count || 0) > 0 ? 'OCR 已完成 · 待核对保存' : 'OCR 已完成 · 待建块';
  }
  if (!phase.textbook_text_ocr_complete || !phase.slide_text_ocr_complete) {
    return '① 文字 OCR 未完成';
  }
  return '② 图片 OCR 未完成';
}

function formatOcrRunToast(base, warnings) {
  const ws = (warnings || []).filter(Boolean);
  if (!ws.length) return base;
  const head = ws.slice(0, 2).join('；');
  const tail = ws.length > 2 ? `（另有 ${ws.length - 2} 条警告）` : '';
  return `${base}${tail ? ` ${tail}` : ''} · ${head}`;
}

function syncWorkflowPhase() {
  const locked = blocksLocked();
  const manual = manualReviewEnabled();
  const blockCount = state?.stats?.block_count || 0;
  const busy = !!(state?._lessonExtractBusy || state?._lessonCurateBusy
    || state?._atomToolbarBusy || state?._bootstrapBusy);

  const atomHint = document.getElementById('atom-pipeline-hint');
  const atomActions = document.getElementById('atom-actions');
  const saveAtomsBtn = document.getElementById('save-atoms-btn');
  if (atomHint) atomHint.hidden = manual || locked;
  if (atomActions) atomActions.hidden = !manual;
  if (saveAtomsBtn) saveAtomsBtn.hidden = !manual;

  if (!manual && !locked) {
    selectedAtoms.clear();
    selectedSlides.clear();
    editingBlockCode = null;
    editingBlockName = '';
    editingStageRef = '';
    blockAddPickerOpen = null;
  }

  const stepPipeline = document.getElementById('step-group-pipeline');
  const stepBlocks = document.getElementById('step-group-blocks');
  [stepPipeline, stepBlocks].forEach((el) => el?.classList.remove('is-current', 'is-done'));
  if (locked) {
    stepPipeline?.classList.add('is-done');
    stepBlocks?.classList.add('is-done');
  } else if (manual) {
    stepPipeline?.classList.add('is-done');
    stepBlocks?.classList.add('is-current');
  } else if (blockCount > 0) {
    stepPipeline?.classList.add('is-done');
    stepBlocks?.classList.add('is-current');
  } else {
    stepPipeline?.classList.add('is-current');
  }

  renderPipelineSubtrack();

  const statusEl = document.getElementById('build-step-status');
  if (statusEl && !busy) {
    if (locked) {
      statusEl.textContent = `已保存锁定 · ${blockCount} 个区块`;
    } else if (manual) {
      statusEl.textContent = '③ 人工核对：可修正原子与区块，无误后点「保存」';
    } else if (blockCount > 0) {
      statusEl.textContent = 'AI 已建块，请核对后点「保存」锁定';
    } else {
      statusEl.textContent = isPilotPipeline()
        ? (pipelineProfile().trust_llm_atoms
          ? '点 ① 文字OCR → ② 图片OCR → ③ 建块，或「一键全流程」'
          : '点 ① 文字OCR → ② 图片OCR → ③ 预匹配 → ④ 建块，或「一键全流程」')
        : '点 ① OCR → ② 整理 → ③ 建块分步核对，或「一键全流程」';
    }
  }
}

/** @type {ReturnType<typeof setInterval> | null} */
let pipelineProgressTimer = null;

function pipelineSubstepsForMode(mode) {
  if (isPilotPipeline()) {
    if (mode === 'seed_only') {
      return [{ id: 'substep-seed', label: '建块', start: 0, end: 100 }];
    }
    const profile = pipelineProfile();
    if (profile.trust_llm_atoms) {
      return [
        { id: 'substep-ocr-text', label: '文字 OCR', start: 0, end: 40 },
        { id: 'substep-ocr-image', label: '图片 OCR', start: 40, end: 70 },
        { id: 'substep-seed', label: '建块', start: 70, end: 100 },
      ];
    }
    return [
      { id: 'substep-ocr-text', label: '文字 OCR', start: 0, end: 32 },
      { id: 'substep-ocr-image', label: '图片 OCR', start: 32, end: 58 },
      { id: 'substep-prematch', label: '课件预匹配', start: 58, end: 78 },
      { id: 'substep-seed', label: 'AI 建块', start: 78, end: 100 },
    ];
  }
  if (mode === 'seed_only') {
    return [{ id: 'substep-seed', label: 'AI 建块', start: 0, end: 100 }];
  }
  if (mode === 'curate_and_seed') {
    return [
      { id: 'substep-curate', label: 'AI 整理', start: 0, end: 45 },
      { id: 'substep-seed', label: 'AI 建块', start: 45, end: 100 },
    ];
  }
  return [
    { id: 'substep-ocr', label: 'OCR 全课', start: 0, end: 38 },
    { id: 'substep-curate', label: 'AI 整理', start: 38, end: 72 },
    { id: 'substep-seed', label: 'AI 建块', start: 72, end: 100 },
  ];
}

function renderPipelineSubtrack() {
  const locked = blocksLocked();
  const manual = manualReviewEnabled();
  const blockCount = state?.stats?.block_count || 0;
  const busy = !!state?._bootstrapBusy;
  const progress = state?._pipelineProgress;
  const readiness = state?.bootstrap_readiness || {};
  const mode = state?.bootstrap_readiness?.recommended_mode || 'full';
  const steps = pipelineSubstepsForMode(mode);
  const activeKey = progress?.activeId || null;
  const percent = progress?.percent ?? 0;
  const pageCount = readiness.page_count || 0;
  const ocrDone = pageCount > 0 && (readiness.pages_with_ocr || 0) >= pageCount;
  const phase = ocrPhaseStats();
  const textDone = !!phase.text_ocr_complete;
  const imageDone = ocrImagePhaseComplete();
  const prematchDone = !!(state?.prematch_review?.done);
  const curateDone = pageCount > 0 && (
    (readiness.curated_pages || 0) >= pageCount
    || readiness.recommended_skip_curate
  );

  steps.forEach((step) => {
    const el = document.getElementById(step.id);
    if (!el) return;
    el.classList.remove('is-current', 'is-done', 'is-pending');
    if (busy && activeKey === step.id) {
      el.classList.add('is-current');
    } else if (percent >= step.end || (manual && step.id === 'substep-seed') || locked) {
      el.classList.add('is-done');
    } else if (!busy && step.id === 'substep-seed' && blockCount > 0) {
      el.classList.add('is-done');
    } else if (!busy && step.id === 'substep-ocr' && ocrDone) {
      el.classList.add('is-done');
    } else if (!busy && step.id === 'substep-ocr-text' && textDone) {
      el.classList.add('is-done');
    } else if (!busy && step.id === 'substep-ocr-image' && imageDone) {
      el.classList.add('is-done');
    } else if (!busy && step.id === 'substep-curate' && curateDone) {
      el.classList.add('is-done');
    } else if (!busy && step.id === 'substep-prematch' && prematchDone) {
      el.classList.add('is-done');
    } else {
      el.classList.add('is-pending');
    }
  });

  ['substep-ocr', 'substep-ocr-text', 'substep-ocr-image', 'substep-curate', 'substep-prematch', 'substep-seed'].forEach((id) => {
    if (steps.some((s) => s.id === id)) return;
    document.getElementById(id)?.classList.add('is-pending');
  });
}

function syncBuildProgress() {
  const progressWrap = document.getElementById('build-step-progress-wrap');
  const progressFill = document.getElementById('build-step-progress-fill');
  const progressLabel = document.getElementById('build-step-progress-label');
  if (!progressWrap || !progressFill || !progressLabel) return;

  const locked = blocksLocked();
  const manual = manualReviewEnabled();
  const blockCount = state?.stats?.block_count || 0;
  const busy = !!state?._bootstrapBusy;
  const extractBusy = !!state?._lessonExtractBusy;
  const progress = state?._pipelineProgress;
  const pilot = isPilotPipeline();

  let percent = 0;
  let label = '';
  if (busy && progress) {
    percent = progress.percent;
    label = progress.label || '';
  } else if (locked) {
    percent = 100;
    label = `已保存锁定 · ${blockCount} 个区块`;
  } else if (manual || blockCount > 0) {
    percent = 100;
    label = manual ? '等待人工核对并保存' : 'AI 已建块，等待保存';
  } else if (pilot) {
    percent = pilotPipelineOverallPercent();
    label = extractBusy
      ? (state._lessonExtractBusy || 'OCR 进行中…')
      : pilotOcrSummaryLabel();
  } else {
    percent = 0;
    label = '尚未开始';
  }

  const phase = ocrPhaseStats();
  const showProgress = busy || manual || blockCount > 0 || locked
    || (pilot && ((phase.page_count || 0) > 0 || (phase.slide_count || 0) > 0));
  progressWrap.hidden = !showProgress;
  progressFill.style.width = `${percent}%`;
  progressWrap.querySelector('[role="progressbar"]')?.setAttribute('aria-valuenow', String(percent));
  progressLabel.textContent = `${percent}% · ${label}`;
  syncOcrPhaseDetail();
}

function stopPipelineProgress(finalPercent, finalLabel) {
  if (pipelineProgressTimer) {
    clearInterval(pipelineProgressTimer);
    pipelineProgressTimer = null;
  }
  if (finalPercent != null) {
    state = {
      ...state,
      _pipelineProgress: {
        percent: finalPercent,
        label: finalLabel || '',
        activeId: null,
      },
    };
  } else {
    state = { ...state, _pipelineProgress: null };
  }
}

function startPipelineProgress(mode) {
  if (pipelineProgressTimer) {
    clearInterval(pipelineProgressTimer);
    pipelineProgressTimer = null;
  }
  const steps = pipelineSubstepsForMode(mode);
  let stepIdx = 0;
  let percent = steps[0]?.start ?? 0;

  const tick = () => {
    const step = steps[stepIdx];
    if (!step) return;
    const cap = Math.max(step.end - 1, step.start);
    if (percent < cap) {
      percent = Math.min(cap, percent + 1);
    } else if (stepIdx < steps.length - 1) {
      stepIdx += 1;
    }
    const active = steps[stepIdx];
    state = {
      ...state,
      _pipelineProgress: {
        percent,
        label: `${active.label}…`,
        activeId: active.id,
      },
    };
    syncBuildProgress();
    renderPipelineSubtrack();
  };

  tick();
  pipelineProgressTimer = setInterval(tick, 1200);
}

const PIPELINE_SUBSTEP_BTN_IDS = [
  'substep-ocr', 'substep-ocr-text', 'substep-ocr-image', 'substep-curate', 'substep-prematch', 'substep-seed',
];

function syncHeaderToolbar() {
  const locked = blocksLocked();
  const busy = !!(state?._lessonExtractBusy || state?._lessonCurateBusy || state?._atomToolbarBusy);
  const pipelineBusy = !!state?._bootstrapBusy;
  const disablePipeline = locked || busy || pipelineBusy;
  PIPELINE_SUBSTEP_BTN_IDS.forEach((id) => {
    const btn = document.getElementById(id);
    if (!btn || btn.hidden) return;
    btn.disabled = disablePipeline;
  });
  ['extract-lesson-btn', 'ai-curate-lesson-btn', 'ai-bootstrap-btn'].forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = disablePipeline;
  });
  const pipelineBtn = document.getElementById('full-pipeline-btn');
  if (pipelineBtn) pipelineBtn.disabled = disablePipeline;
  const lockBtn = document.getElementById('lock-blocks-btn');
  if (lockBtn) lockBtn.hidden = locked || !(state?.stats?.block_count || 0);
  const busyHint = state?._lessonExtractBusy || state?._lessonCurateBusy
    || state?._atomToolbarBusy || state?._bootstrapBusy || '';
  syncWorkflowPhase();
  syncBuildProgress();
  const statusEl = document.getElementById('build-step-status');
  if (statusEl && busyHint) statusEl.textContent = busyHint;
}

async function load() {
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/annotate`);
  const d = await readJson(r);
  if (!r.ok || !d.ok) throw new Error(d.error || '加载失败');
  state = d;
  previewSlideIndex = null;
  renderAll();
}

/** 单页 OCR（API 仍可用；页头未展示，供调试或后续接回） */
async function extractCurrentPageAtoms() {
  const btn = document.getElementById('extract-btn');
  if (!confirm('将覆盖本页全部原子（含手动合并/删除结果）。误操作可用「撤销上一步」；整页重来请确认。')) {
    return;
  }
  const label = 'OCR 本页';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '识别中…';
  }
  state = { ...state, _atomToolbarBusy: 'OCR 本页…' };
  syncHeaderToolbar();
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/extract-page`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ page_index: currentPageIndex }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '提取失败');
    state = { ...d, _atomToolbarBusy: null };
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    toast(`已提取 ${d.extracted || 0} 个原子`);
  } catch (e) {
    state = { ...state, _atomToolbarBusy: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

async function extractWholeLesson(options = {}) {
  const { triggerBtnId = 'extract-lesson-btn' } = options;
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const pages = [...(state?.textbook_pages || [])].sort(
    (a, b) => Number(a.page_index) - Number(b.page_index),
  );
  if (!pages.length) {
    toast('本课无教材页');
    return;
  }
  if (!confirm(
    `将对本课 ${pages.length} 页教材全部执行 OCR，每页覆盖已有原子（含手动合并/删除结果）。`
    + '误操作可用「撤销上一步」。继续？',
  )) {
    return;
  }
  const btn = document.getElementById(triggerBtnId);
  const label = btn?.dataset?.defaultLabel || btn?.textContent || '① OCR';
  if (btn) {
    btn.disabled = true;
    if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
    btn.textContent = '识别中…';
  }
  state = { ...state, _lessonExtractBusy: `OCR 全课 0/${pages.length} 页…` };
  syncHeaderToolbar();
  try {
    for (let i = 0; i < pages.length; i += 1) {
      const pi = Number(pages[i].page_index);
      const progress = `OCR 全课 ${i + 1}/${pages.length} 页…`;
      state = { ...state, _lessonExtractBusy: progress };
      syncHeaderToolbar();
      if (btn) btn.textContent = `识别 ${i + 1}/${pages.length}…`;
      const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/extract-page`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page_index: pi }),
      });
      const d = await readJson(r);
      if (!r.ok || !d.ok) throw new Error(d.error || `第 ${pi} 页 OCR 失败`);
      state = { ...d, _lessonExtractBusy: progress };
    }
    selectedAtoms.clear();
    clearUndoSnapshot();
    state = { ...state, _lessonExtractBusy: null };
    renderAll();
    toast(`全课 OCR 完成：${pages.length} 页`);
  } catch (e) {
    state = { ...state, _lessonExtractBusy: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

document.getElementById('extract-lesson-btn')?.addEventListener('click', () => extractWholeLesson());
document.getElementById('substep-ocr')?.addEventListener('click', () => extractWholeLesson({ triggerBtnId: 'substep-ocr' }));

async function ocrTextLessonPilot(options = {}) {
  const { triggerBtnId = 'substep-ocr-text', skipConfirm = false } = options;
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const pageCount = state?.ocr_phase?.page_count || state?.stats?.page_count || 0;
  const slideCount = state?.ocr_phase?.slide_count || state?.stats?.slide_count || 0;
  if (!skipConfirm && !confirm(
    `对本课执行豆包文字 OCR？\n· 教材 ${pageCount} 页 → text/title 原子\n· 课件 ${slideCount} 张 → 识字写入库\n与双轨共用缓存。继续？`,
  )) return;
  const btn = document.getElementById(triggerBtnId);
  const label = btn?.dataset?.defaultLabel || btn?.textContent || '① 文字OCR';
  if (btn) {
    btn.disabled = true;
    if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
    btn.textContent = '识别中…';
  }
  state = { ...state, _lessonExtractBusy: '豆包文字 OCR…' };
  syncHeaderToolbar();
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/ocr-text-lesson`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '文字 OCR 失败');
    const warnings = d.ocr_run?.warnings || [];
    state = { ...d, _lessonExtractBusy: null, _lastOcrWarnings: warnings };
    renderAll();
    const run = d.ocr_run || {};
    const tb = run.textbook || {};
    const cw = run.courseware || {};
    toast(formatOcrRunToast(
      `文字 OCR：教材 ${tb.pages_done ?? '?'}/${tb.pages_total ?? '?'} 页`
      + ` · 课件 ${cw.slides_with_text ?? '?'}/${cw.slides_total ?? '?'} 张有字`,
      warnings,
    ));
  } catch (e) {
    state = { ...state, _lessonExtractBusy: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

async function ocrImageLessonPilot(options = {}) {
  const { triggerBtnId = 'substep-ocr-image', skipConfirm = false } = options;
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  if (!ocrPhaseStats().text_ocr_complete) {
    toast('请先完成 ① 文字OCR');
    return;
  }
  if (!skipConfirm && !confirm(
    `对本课执行图片/插图 OCR？\n· 教材：OpenCV+豆包版面 → image 原子\n· 课件：豆包识别每页插图/图表区域\n与双轨共用缓存。继续？`,
  )) return;
  const btn = document.getElementById(triggerBtnId);
  const label = btn?.dataset?.defaultLabel || btn?.textContent || '② 图片OCR';
  if (btn) {
    btn.disabled = true;
    if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
    btn.textContent = '识别中…';
  }
  state = { ...state, _lessonExtractBusy: '图片 OCR…' };
  syncHeaderToolbar();
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/ocr-image-lesson`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '图片 OCR 失败');
    const warnings = d.ocr_run?.warnings || [];
    state = { ...d, _lessonExtractBusy: null, _lastOcrWarnings: warnings };
    renderAll();
    const stats = d.ocr_run?.stats || d.ocr_phase || {};
    const run = d.ocr_run || {};
    const tb = run.textbook || {};
    const cw = run.courseware || {};
    toast(formatOcrRunToast(
      `图片 OCR：教材 ${stats.image_atom_count ?? tb.pages_done ?? '?'} 个插图原子`
      + ` · 课件 ${cw.slides_layout_done ?? '?'}/${cw.slides_total ?? '?'} 张`
      + `（${cw.image_regions ?? 0} 区）`,
      warnings,
    ));
  } catch (e) {
    state = { ...state, _lessonExtractBusy: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

document.getElementById('substep-ocr-text')?.addEventListener('click', () => ocrTextLessonPilot());
document.getElementById('substep-ocr-image')?.addEventListener('click', () => ocrImageLessonPilot());

/** 单页 AI 整理（API 仍可用；页头未展示） */
async function aiCurateCurrentPage() {
  const pageMeta = (state?.textbook_pages || []).find(
    (p) => p.page_index === currentPageIndex,
  );
  const skipOcr = pageMeta
    && pageMeta.has_ocr_baseline
    && (pageMeta.atom_count || 0) > 0;
  const confirmMsg = skipOcr
    ? '本页已完成 OCR，将仅执行 AI 整理并写入数据库。\n误操作可用「撤销上一步」。继续？'
    : '将 OCR 本页并由 AI 建议合并/删除原子，然后写入数据库并重编号。\n'
      + '误操作可用「撤销上一步」。继续？';
  if (!confirm(confirmMsg)) {
    return;
  }
  const btn = document.getElementById('ai-curate-page-btn');
  const label = 'AI 整理本页';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '整理中…';
  }
  state = { ...state, _atomToolbarBusy: skipOcr ? 'AI 整理本页…' : 'OCR + 整理本页…' };
  syncHeaderToolbar();
  toast(skipOcr ? '正在 AI 整理本页…' : '正在 OCR + AI 整理本页…');
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/ai-curate-page`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page_index: currentPageIndex,
        extract_first: !skipOcr,
        replace_page: true,
      }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '整理失败');
    state = { ...d, _atomToolbarBusy: null };
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    const m = (d.merged || []).length;
    const del = (d.deleted || []).length;
    const failWarns = (d.warnings || []).filter((w) => /合并.*失败/.test(w));
    if (failWarns.length) {
      toast(`整理异常：${failWarns[0]}`, 8000);
    } else if (m === 0 && (d.plan?.merge_groups || []).length > 0) {
      toast(`计划有 ${(d.plan.merge_groups).length} 组合并但未写入，请查看 warnings`, 8000);
    } else {
      const rv = d.rules_version ? ` [${d.rules_version}]` : '';
      const repaired = (d.repair?.unmerged || []).length + (d.repair?.split || []).length;
      const repairNote = repaired ? `，修复 ${repaired} 处粘连/误并` : '';
      toast(d.warnings?.length
        ? `本页已整理：合并 ${m} 组、删除 ${del} 个${repairNote}${rv}（${d.warnings.length} 条提示）`
        : `本页已整理：合并 ${m} 组、删除 ${del} 个${repairNote}${rv}`);
    }
  } catch (e) {
    state = { ...state, _atomToolbarBusy: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

async function aiCurateWholeLesson(options = {}) {
  const {
    curateOnly = false,
    triggerBtnId = 'ai-curate-lesson-btn',
  } = options;
  const readiness = state?.bootstrap_readiness || {};
  const skipOcr = curateOnly
    || readiness.recommended_extract_first === false
    || readiness.recommended_mode === 'seed_only'
    || readiness.recommended_mode === 'curate_and_seed';
  if (curateOnly && (readiness.pages_with_ocr || 0) <= 0) {
    toast('请先完成 ① OCR');
    return;
  }
  const confirmMsg = curateOnly || skipOcr
    ? '将仅执行 AI 整理全课（不建块）。继续？'
    : '对本课全部教材页执行 OCR + AI 整理，耗时较长。继续？';
  if (!confirm(confirmMsg)) return;
  const btn = document.getElementById(triggerBtnId);
  const label = btn?.dataset?.defaultLabel || btn?.textContent || '② 整理';
  if (btn) {
    btn.disabled = true;
    if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
    btn.textContent = '整理中…';
  }
  state = { ...state, _lessonCurateBusy: skipOcr ? 'AI 整理全课…' : 'OCR + 整理全课…' };
  syncHeaderToolbar();
  toast(skipOcr ? '正在 AI 整理全课…' : '正在 OCR + 整理全课教材原子…');
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/ai-curate-lesson`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ extract_first: !skipOcr }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '整理失败');
    state = { ...d, _lessonCurateBusy: null };
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    toast(`全课整理完成：${d.curated_count || 0}/${d.page_count || 0} 页${
      d.pages?.[0]?.rules_version ? ` [${d.pages[0].rules_version}]` : ''
    }`);
  } catch (e) {
    state = { ...state, _lessonCurateBusy: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

async function runPipelineStepCurate() {
  await aiCurateWholeLesson({ curateOnly: true, triggerBtnId: 'substep-curate' });
}

async function runPipelineStepPrematch(options = {}) {
  const { skipConfirm = false } = options;
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const phase = ocrPhaseStats();
  if (!phase.text_ocr_complete) {
    toast('请先完成 ① 文字OCR');
    return;
  }
  if (!skipConfirm && !confirm('课件预匹配：按 slide OCR 拆分/标注教材原子，不建块。继续？')) return;
  const btn = document.getElementById('substep-prematch');
  const label = btn?.dataset?.defaultLabel || btn?.textContent || '③ 预匹配';
  if (btn) {
    btn.disabled = true;
    if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
    btn.textContent = '预匹配中…';
  }
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/prematch-lesson`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ renumber: true }),
    });
    const d = await readJson(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || `预匹配失败（HTTP ${r.status}）`);
    state = d;
    renderAll();
    const n = (d.split || []).length;
    toast(`课件预匹配完成${n ? `：拆分 ${n} 项` : ''}`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

async function runPipelineStepSeed() {
  if (isPilotPipeline()) {
    const phase = ocrPhaseStats();
    if (!phase.text_ocr_complete || !ocrImagePhaseComplete()) {
      toast('请先完成 ① 文字OCR 与 ② 图片OCR');
      return;
    }
    if (!pipelineProfile().trust_llm_atoms && !prematchReviewData().done) {
      toast('请先完成 ③ 课件预匹配（或点一键全流程）');
      return;
    }
  }
  await aiBootstrapLesson({
    triggerBtnId: 'substep-seed',
    extractFirst: false,
    skipCurate: true,
    progressMode: 'seed_only',
    confirmPrefix: isPilotPipeline()
      ? '将执行 AI 建块（含豆包教学意图），跳过整理。'
      : '将仅执行 AI 建块（跳过 OCR 与整理）。',
  });
}

function bootstrapCreatedBlockCount(payload) {
  const d = payload || {};
  const seedMeta = d.bootstrap?.block_seed || d.block_seed || d.blocks || {};
  if (typeof seedMeta.created_count === 'number') return seedMeta.created_count;
  return (d.stats?.block_count ?? (Array.isArray(d.blocks) ? d.blocks.length : 0)) || 0;
}

function bootstrapSeedSkipped(payload) {
  const d = payload || {};
  const seedMeta = d.bootstrap?.block_seed || d.block_seed || {};
  return seedMeta.skipped === true;
}

async function runPilotOcrChain({ triggerBtnId = 'full-pipeline-btn' } = {}) {
  const phase = ocrPhaseStats();
  if (!phase.text_ocr_complete) {
    await ocrTextLessonPilot({ triggerBtnId, skipConfirm: true });
  }
  if (!ocrImagePhaseComplete()) {
    await ocrImageLessonPilot({ triggerBtnId, skipConfirm: true });
  }
}

async function aiBootstrapLesson(options = {}) {
  const {
    skipConfirm = false,
    replaceExisting = true,
    triggerBtnId = 'ai-bootstrap-btn',
    extractFirst = null,
    skipCurate = null,
    progressMode = null,
    confirmPrefix = null,
  } = options;
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const slides = state?.courseware_slides || [];
  if (!slides.length) {
    toast('请先上传课件 ZIP');
    return;
  }
  const readiness = state?.bootstrap_readiness || {};
  const mode = progressMode || readiness.recommended_mode || 'full';
  const existing = (state?.blocks || []).length;
  if (!skipConfirm) {
    let confirmMsg = confirmPrefix
      || readiness.summary
      || '将执行：全课 OCR → AI 整理原子 → AI 建块，结果写入数据库。';
    if (!confirmPrefix) {
      if (mode === 'seed_only') {
        confirmMsg = readiness.summary || '本课已完成 OCR 与 AI 整理，将直接 AI 建块。';
      } else if (mode === 'curate_and_seed') {
        confirmMsg = readiness.summary || '本课已有 OCR，将跳过 OCR，执行 AI 整理 + 建块。';
      }
    }
    if (existing && replaceExisting) {
      confirmMsg += `\n已有 ${existing} 个区块将被清空重建。`;
    } else if (existing) {
      confirmMsg += `\n已有 ${existing} 个区块，将保留现有区块。`;
    } else if (!confirmPrefix) {
      confirmMsg += ' 结果写入数据库。';
    }
    confirmMsg += ' 继续？';
    if (!confirm(confirmMsg)) return;
  }
  const btn = document.getElementById(triggerBtnId);
  const label = btn?.dataset?.defaultLabel || btn?.textContent || '③ 建块';
  if (btn) {
    btn.disabled = true;
    if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
  }
  state = { ...state, _bootstrapBusy: '一键全流程进行中…' };
  startPipelineProgress(mode);
  syncHeaderToolbar();
  const toastHint = mode === 'seed_only'
    ? 'AI 建块进行中…'
    : mode === 'curate_and_seed'
      ? 'AI 整理 + 建块进行中…'
      : '一键全流程进行中（可能 1–3 分钟）…';
  toast(toastHint);
  try {
    const body = {
      replace_existing: replaceExisting,
    };
    if (extractFirst !== null) body.extract_first = extractFirst;
    if (skipCurate !== null) body.skip_curate = skipCurate;
    if (extractFirst === null) {
      // 试点分步 OCR 后须强制只建块，避免重复跑整课 OCR 且因长请求超时导致建块未执行
      body.extract_first = isPilotPipeline() ? false : (readiness.recommended_extract_first ?? true);
    }
    if (skipCurate === null) {
      body.skip_curate = isPilotPipeline()
        ? true
        : (readiness.recommended_skip_curate ?? false);
    }
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/ai-bootstrap`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await readJson(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || `一键建块失败（HTTP ${r.status}）`);
    const created = bootstrapCreatedBlockCount(d);
    const seedSkipped = bootstrapSeedSkipped(d);
    if (pipelineProgressTimer) {
      clearInterval(pipelineProgressTimer);
      pipelineProgressTimer = null;
    }
    if (seedSkipped || created <= 0) {
      const warn = (d.bootstrap?.warnings || d.warnings || []).filter(Boolean).slice(0, 2).join(' · ');
      state = {
        ...d,
        _bootstrapBusy: null,
        _pipelineProgress: {
          percent: isPilotPipeline() ? pilotPipelineOverallPercent() : 70,
          label: 'OCR 已完成 · 建块未产出',
          activeId: null,
        },
      };
      editingBlockCode = null;
      renderAll();
      toast(warn
        ? `建块未生成区块：${warn}`
        : '建块未生成区块，请点 ③ 建块重试，或检查 .env 中 LLM 配置');
      return;
    }
    state = {
      ...d,
      _bootstrapBusy: null,
      _pipelineProgress: { percent: 100, label: '全流程完成', activeId: null },
    };
    editingBlockCode = null;
    selectedAtoms.clear();
    selectedSlides.clear();
    renderAll();
    const n = created;
    toast(d.warnings?.length
      ? `一键建块完成，已建 ${n} 个区块（有提示）`
      : `一键建块完成，已建 ${n} 个区块`);
  } catch (e) {
    stopPipelineProgress(null);
    state = { ...state, _bootstrapBusy: null, _pipelineProgress: null };
    renderAll();
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = label;
    }
    syncHeaderToolbar();
  }
}

async function runFullPipeline() {
  if (isPilotPipeline()) {
    const profile = pipelineProfile();
    const phase = ocrPhaseStats();
    const needOcr = !phase.text_ocr_complete || !ocrImagePhaseComplete();
    const needPrematch = !profile.trust_llm_atoms && !prematchReviewData().done;
    let confirmMsg = profile.trust_llm_atoms
      ? '将执行：豆包文字 OCR → 图片 OCR → AI 建块，跳过版面整理与课件预匹配。'
      : '将执行：豆包文字 OCR → 图片 OCR → 课件预匹配 → AI 建块，跳过版面整理。';
    if (!needOcr && !needPrematch) {
      confirmMsg = 'OCR 与预匹配已完成，将直接 AI 建块。';
    } else if (!needOcr) {
      confirmMsg = 'OCR 已完成，将执行课件预匹配 → AI 建块。';
    }
    if (!confirm(`${confirmMsg}\n（分步执行，避免 OCR 完成后建块因超时未落库）\n继续？`)) return;

    const btn = document.getElementById('full-pipeline-btn');
    const label = btn?.dataset?.defaultLabel || btn?.textContent || '一键全流程';
    if (btn) {
      btn.disabled = true;
      if (!btn.dataset.defaultLabel) btn.dataset.defaultLabel = label;
    }
    state = { ...state, _bootstrapBusy: '一键全流程进行中…' };
    startPipelineProgress(needOcr ? (readinessRecommendedMode()) : 'seed_only');
    syncHeaderToolbar();
    toast('一键全流程：分步 OCR → 建块…');

    try {
      if (needOcr) {
        await runPilotOcrChain({ triggerBtnId: 'full-pipeline-btn' });
      }
      if (needPrematch) {
        await runPipelineStepPrematch({ skipConfirm: true });
      }
      await aiBootstrapLesson({
        skipConfirm: true,
        replaceExisting: true,
        triggerBtnId: 'full-pipeline-btn',
        skipCurate: true,
        extractFirst: false,
        progressMode: 'seed_only',
      });
    } catch (e) {
      stopPipelineProgress(null);
      state = { ...state, _bootstrapBusy: null, _pipelineProgress: null };
      renderAll();
      toast(e.message || String(e));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = label;
      }
      syncHeaderToolbar();
    }
    return;
  }
  await aiBootstrapLesson({
    skipConfirm: false,
    replaceExisting: true,
    triggerBtnId: 'full-pipeline-btn',
  });
}

function readinessRecommendedMode() {
  return state?.bootstrap_readiness?.recommended_mode || 'full';
}

document.getElementById('ai-curate-lesson-btn')?.addEventListener('click', () => aiCurateWholeLesson());
document.getElementById('substep-curate')?.addEventListener('click', () => runPipelineStepCurate());
document.getElementById('substep-prematch')?.addEventListener('click', () => runPipelineStepPrematch());
document.getElementById('ai-bootstrap-btn')?.addEventListener('click', () => aiBootstrapLesson());
document.getElementById('substep-seed')?.addEventListener('click', () => runPipelineStepSeed());
document.getElementById('full-pipeline-btn')?.addEventListener('click', () => runFullPipeline());

document.querySelectorAll('.pipeline-review-tab').forEach((tabBtn) => {
  tabBtn.addEventListener('click', () => {
    document.querySelectorAll('.pipeline-review-tab').forEach((b) => b.classList.remove('is-active'));
    tabBtn.classList.add('is-active');
    prematchReviewTab = tabBtn.dataset.tab || 'splits';
    renderPrematchReviewBody(prematchReviewTab);
  });
});

async function submitBlockForm() {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  if (!manualReviewEnabled()) {
    toast('请先完成一键全流程');
    return;
  }
  const name = getBlockFormName();
  const stageRef = getBlockFormStageRef();
  if (!name && !stageRef) {
    toast('请选择环节参考或填写区块名称');
    return;
  }
  const payload = {
    block_name: name,
    stage_ref: stageRef,
    atom_codes: Array.from(selectedAtoms),
    course_slide_indices: Array.from(selectedSlides),
  };
  const isEdit = !!editingBlockCode;
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
    if (!r.ok || !d.ok) throw new Error(d.error || (isEdit ? '保存失败' : '创建失败'));
    state = d;
    const savedCode = editingBlockCode;
    editingBlockCode = null;
    focusedBlockCode = null;
    editingBlockName = '';
    editingStageRef = '';
    selectedAtoms.clear();
    selectedSlides.clear();
    document.getElementById('block-name').value = '';
    syncStageRefControls('', {});
    renderAll();
    toast(savedCode ? `已保存 ${savedCode}` : '区块已创建');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (activeBtn) activeBtn.disabled = false;
  }
}

async function deleteAllBlocks() {
  if (blocksLocked()) {
    toast('本课区块已锁定');
    return;
  }
  const blocks = state?.blocks || [];
  if (!blocks.length) {
    toast('尚无区块');
    return;
  }
  const codes = blocks.map((b) => b.block_code).join('、');
  if (
    !confirm(
      `将删除本课全部 ${blocks.length} 个区块（${codes}）。\n\n`
      + '核对 OCR/整理时若误点了「AI 建块」，可由此清空后重新核对。'
      + '此操作不可撤销，确定继续？',
    )
  ) {
    return;
  }
  try {
    const snapshot = [...blocks];
    for (const b of snapshot) {
      const r = await fetch(
        `${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/${encodeURIComponent(b.block_code)}`,
        { method: 'DELETE' },
      );
      const d = await readJson(r);
      if (!r.ok || !d.ok) throw new Error(d.error || `删除 ${b.block_code} 失败`);
      state = d;
    }
    const n = snapshot.length;
    editingBlockCode = null;
    focusedBlockCode = null;
    editingBlockName = '';
    editingStageRef = '';
    selectedAtoms.clear();
    selectedSlides.clear();
    document.getElementById('block-name').value = '';
    renderAll();
    syncBlockFormUi();
    requestIntakeFocus();
    toast(`已清空 ${n} 个区块`);
  } catch (e) {
    toast(e.message || String(e));
  }
}

document.getElementById('create-block-btn').onclick = () => submitBlockForm();
document.getElementById('delete-all-blocks-btn').onclick = () => deleteAllBlocks();

async function lockLessonBlocks() {
  const blocks = state?.blocks || [];
  if (!blocks.length) {
    toast('尚无区块，请先创建后再保存锁定');
    return;
  }
  if (blocksLocked()) return;
  if (!confirm(`锁定本课全部 ${blocks.length} 个区块？锁定后不可编辑、删除或调序，防止误操作。`)) {
    return;
  }
  const btn = document.getElementById('lock-blocks-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/lock`, {
      method: 'POST',
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '锁定失败');
    state = d;
    editingBlockCode = null;
    focusedBlockCode = null;
    selectedAtoms.clear();
    selectedSlides.clear();
    renderAll();
    toast(d.already_locked ? '区块已是锁定状态' : `已锁定 ${d.block_count || blocks.length} 个区块`);
    requestIntakeFocus();
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function unlockLessonBlocks() {
  if (!blocksLocked()) return;
  if (!confirm('解除锁定后可继续编辑、删除和调序区块。确定解锁？')) return;
  const btn = document.getElementById('unlock-blocks-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/blocks/unlock`, {
      method: 'POST',
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '解锁失败');
    state = d;
    renderAll();
    toast(d.already_unlocked ? '区块未锁定' : '已解锁，可继续编辑区块');
    requestIntakeFocus();
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.getElementById('lock-blocks-btn')?.addEventListener('click', () => lockLessonBlocks());
document.getElementById('unlock-blocks-btn')?.addEventListener('click', () => unlockLessonBlocks());

document.getElementById('cancel-block-edit-btn')?.addEventListener('click', () => {
  editingBlockCode = null;
  focusedBlockCode = null;
  editingBlockName = '';
  editingStageRef = '';
  blockAddPickerOpen = null;
  selectedAtoms.clear();
  selectedSlides.clear();
  document.getElementById('block-name').value = '';
  syncStageRefControls('', {});
  renderAll();
  toast('已取消编辑');
});

document.getElementById('block-stage-custom')?.addEventListener('input', (ev) => {
  if (editingBlockCode) return;
  editingStageRef = ev.target.value;
});

document.getElementById('clear-atoms-btn')?.addEventListener('click', () => {
  selectedAtoms.clear();
  renderTextbook();
  syncAtomActionButtons();
});

document.getElementById('delete-atoms-btn')?.addEventListener('click', async () => {
  const codes = Array.from(selectedAtoms);
  if (!codes.length) return;
  if (!confirm(`删除 ${codes.length} 个原子？删错可点「撤销上一步」。`)) return;
  captureUndoSnapshot();
  const btn = document.getElementById('delete-atoms-btn');
  btn.disabled = true;
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
    syncAtomActionButtons();
    toast(`已删除 ${codes.length} 个原子`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    syncAtomActionButtons();
  }
});

document.getElementById('merge-atoms-btn')?.addEventListener('click', async () => {
  const codes = Array.from(selectedAtoms);
  if (codes.length < 2) return;
  if (!confirm(`将合并以下 ${codes.length} 个原子（合并后只保留 1 个，其余删除）：\n\n${codes.map((code) => {
    const a = (state?.atoms || []).find((x) => x.atom_code === code);
    return a ? atomLabel(a) : code;
  }).join('\n')}`)) return;
  captureUndoSnapshot();
  const btn = document.getElementById('merge-atoms-btn');
  btn.disabled = true;
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
    syncAtomActionButtons();
    toast(`已合并为 ${d.atom_code}`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    syncAtomActionButtons();
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

document.getElementById('tb-prev-btn')?.addEventListener('click', () => gotoTextbookPage(-1));
document.getElementById('tb-next-btn')?.addEventListener('click', () => gotoTextbookPage(1));

async function saveCurrentTextbookPage() {
  const count = atomsOnPage(currentPageIndex).length;
  if (!count) {
    toast('本页没有原子');
    return;
  }
  if (
    !confirm(
      `将删除本页数据库中的全部旧原子（含隐藏占位条），`
      + `仅保存当前 ${count} 个框，并按从上到下、从左到右重编号为 `
      + `A${String(currentPageIndex).padStart(3, '0')}-001、002…\n`
      + '已建区块中的原子引用会同步更新。继续？',
    )
  ) {
    return;
  }
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
    selectedAtoms.clear();
    clearUndoSnapshot();
    renderAll();
    const saved = d.saved ?? 0;
    const removed = d.removed ?? 0;
    toast(`已保存 ${saved} 个原子（清除旧记录 ${removed} 条）`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.getElementById('save-atoms-btn')?.addEventListener('click', () => saveCurrentTextbookPage());

document.getElementById('undo-atoms-btn')?.addEventListener('click', async () => {
  if (!undoSnapshot) return;
  const snap = undoSnapshot;
  if (
    snap.page_index !== currentPageIndex
    && !confirm(`快照来自第 ${snap.page_index} 页，当前在第 ${currentPageIndex} 页。仍要恢复吗？`)
  ) {
    return;
  }
  const btn = document.getElementById('undo-atoms-btn');
  btn.disabled = true;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/atoms/restore-page`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page_index: snap.page_index,
        atoms: snap.atoms,
        blocks: snap.blocks,
      }),
    });
    const d = await readJson(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '恢复失败');
    state = d;
    selectedAtoms.clear();
    clearUndoSnapshot();
    if (snap.page_index !== currentPageIndex) {
      currentPageIndex = snap.page_index;
    }
    renderAll();
    toast(`已恢复第 ${snap.page_index} 页（${snap.atoms.length} 个原子）`);
  } catch (e) {
    toast(e.message || String(e));
    syncAtomActionButtons();
  }
});

initSlideBoundModal();
document.getElementById('atom-layer')?.addEventListener('click', (ev) => {
  const box = ev.target.closest('.atom-box');
  if (!box?.dataset?.atom) return;
  ev.preventDefault();
  ev.stopPropagation();
  toggleAtomPick(box.dataset.atom);
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
document.getElementById('hide-atom-tags-checkbox')?.addEventListener('change', (ev) => {
  hideAtomTags = Boolean(ev.target.checked);
  try {
    localStorage.setItem('annotate.hideAtomTags', hideAtomTags ? '1' : '0');
  } catch {
    /* ignore */
  }
  applyAtomTagVisibility();
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
})();
document.getElementById('annotate-close-btn')?.addEventListener('click', closeAnnotateOrBack);
load().catch((e) => {
  document.getElementById('page-sub').textContent = e.message || String(e);
});
