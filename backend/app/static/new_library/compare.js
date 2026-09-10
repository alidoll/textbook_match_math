const LESSON_UID = window.COMPARE_LESSON_UID;
const API = window.COMPARE_API_PREFIX || '/api/new-library';

let state = null;
let selectedMatchId = null;
let viewMode = 'pair';
let showcaseRunning = false;
let showcaseAnimToken = 0;

function getShowcaseVariant() {
  const p = new URLSearchParams(window.location.search).get('showcase');
  if (p === 'classic' || p === 'doubao' || p === 'hybrid' || p === 'v4') return p;
  return 'v4';
}
window.getShowcaseVariant = getShowcaseVariant;

function prefersReducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches;
}

function showcaseStats() {
  const oldN = (state?.old_blocks || []).length;
  const newN = (state?.new_blocks || []).length;
  const matches = state?.matches || [];
  const matchN = matches.length;
  const paired = matches.filter((m) => m.old_block_id && m.new_block_id).length;
  const judged = matches.filter((m) => {
    const code = (m.reuse_action || '').trim();
    return code && code !== 'pending';
  }).length;
  return { oldN, newN, matchN, paired, judged };
}

function animateCountUp(el, target, formatter, duration = 520) {
  if (!el) return Promise.resolve();
  if (prefersReducedMotion() || target <= 0) {
    el.textContent = formatter(target);
    return Promise.resolve();
  }
  const start = performance.now();
  return new Promise((resolve) => {
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - (1 - t) ** 3;
      const val = Math.round(target * eased);
      el.textContent = formatter(val);
      if (t < 1) requestAnimationFrame(tick);
      else resolve();
    };
    requestAnimationFrame(tick);
  });
}

function matchLineStatusClass(m) {
  if (!m?.old_block_id || !m?.new_block_id) return 'match-line-orphan';
  const code = (m.reuse_action || '').trim();
  if (code && code !== 'pending') return 'match-line-judged';
  const ai = (m.ai_reuse_action || '').trim();
  if (ai && ai !== 'pending') return 'match-line-partial';
  return 'match-line-pending';
}

function scrollBlockIntoView(card) {
  if (!card) return;
  card.scrollIntoView({ block: 'nearest', behavior: prefersReducedMotion() ? 'auto' : 'smooth' });
}

function calcPullHold(blockCount, staggerMs = 200) {
  if (blockCount <= 0) return 2200;
  const lastCardStart = Math.max(0, blockCount - 1) * staggerMs;
  return Math.max(2400, lastCardStart + 680 + 280);
}

function setShowcaseDemoProgress(pct, labelText) {
  const fill = document.getElementById('showcase-progress-fill');
  const label = document.getElementById('showcase-progress-label');
  const wrap = document.getElementById('showcase-progress-wrap');
  if (fill) {
    fill.style.transition = 'width 0.12s linear';
    fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  }
  if (label && labelText) label.textContent = labelText;
  if (wrap) wrap.classList.toggle('is-complete', pct >= 100);
}

function updateShowcaseProgressBar() {
  const fill = document.getElementById('showcase-progress-fill');
  const label = document.getElementById('showcase-progress-label');
  const wrap = document.getElementById('showcase-progress-wrap');
  if (fill) fill.style.transition = 'width 0.65s cubic-bezier(0.34, 1.2, 0.64, 1)';
  const { paired, matchN } = showcaseStats();
  const pct = matchN ? Math.round((paired / matchN) * 100) : (paired ? 100 : 0);
  if (fill) fill.style.width = `${pct}%`;
  if (label) label.textContent = matchN ? `完整配对 ${paired} / ${matchN}` : `完整配对 ${paired}`;
  if (wrap) wrap.classList.toggle('is-complete', matchN > 0 && paired >= matchN);
}

function updateGoWorkbenchHighlight() {
  const btn = document.getElementById('btn-go-workbench');
  if (!btn || viewMode !== 'pair') return;
  const { paired, matchN, oldN, newN } = showcaseStats();
  const hasBlocks = oldN > 0 || newN > 0;
  const ready = hasBlocks && (matchN === 0 || paired > 0);
  btn.classList.toggle('showcase-cta-ready', ready);
  btn.classList.toggle('showcase-cta-complete', matchN > 0 && paired >= matchN);
}
let dragState = null;
let atomsByKey = {};
let feedbackContext = null;
let selectedPageIndex = null;
let queuePageFilter = '';
let judgePanelCollapsed = false;
let selectedOrphan = null;

const PAGE_LEVEL_CLASS = {
  mostly_reuse: 'page-level-mostly_reuse',
  partial_adjust: 'page-level-partial_adjust',
  major_rebuild: 'page-level-major_rebuild',
};

const QA_TYPE_LABEL = {
  duplicate_anchor: '旧块重复锚定',
  non_contiguous_multi_anchor: '多锚定不连续',
  duplicate_pair: '重复配对行',
  one_to_one: '一对一冲突',
  order_inversion: '顺序颠倒',
  low_confidence: '匹配置信度低',
};

const QA_SEVERITY_LABEL = { high: '高', medium: '中', low: '低' };

const ACTION_LEGEND = [
  { code: 'reuse_as_is', label: '直接沿用' },
  { code: 'optimize', label: '优化调整' },
  { code: 'reference', label: '参考重制' },
  { code: 'new_build', label: '全新制作' },
  { code: 'remove', label: '完全删除' },
  { code: 'pending', label: '待判定' },
];

function atomKey(side, code) {
  return `${side}:${code}`;
}

function rebuildAtomIndex() {
  atomsByKey = {};
  (state?.float_atoms || []).forEach((a) => {
    if (!a?.atom_code) return;
    const side = a.book_side || 'old';
    atomsByKey[atomKey(side, a.atom_code)] = a;
  });
}

function setState(data) {
  state = data;
  rebuildAtomIndex();
}

function blockOptionLabel(block, side) {
  const excerpt = blockExcerpt(block, side);
  const base = `${block.block_id} ${block.block_name || ''}`;
  return excerpt ? `${base} — ${excerpt}` : base;
}

function previewFromBlockSelects() {
  const oldCode = document.getElementById('add-old-block')?.value;
  const newCode = document.getElementById('add-new-block')?.value;
  const oldBlock = (state?.old_blocks || []).find((b) => b.block_id === oldCode);
  const newBlock = (state?.new_blocks || []).find((b) => b.block_id === newCode);
  renderComparePreview(oldBlock || null, newBlock || null);
}

function blockExcerpt(block, side) {
  const codes = block?.atom_codes || [];
  if (!codes.length) return '';
  return codes.slice(0, 2).map((id) => {
    const a = atomsByKey[atomKey(side, id)];
    const t = (a?.text || a?.content || '').trim();
    return t ? t.slice(0, 24) : id;
  }).filter(Boolean).join(' · ');
}

function atomTextList(block, side, max = 8) {
  const codes = block?.atom_codes || [];
  if (!codes.length) {
    const pgs = side === 'old' ? block?.old_tb_pgs : block?.new_tb_pgs;
    if (pgs?.length) {
      return `<div class="atom-text-muted">本块未绑原子，教材页：P${pgs.join('、P')}</div>`;
    }
    return '<div class="atom-text-muted">（无原子）</div>';
  }
  const rows = codes.slice(0, max).map((id) => {
    const a = atomsByKey[atomKey(side, id)];
    const t = (a?.text || a?.content || id).trim();
    return `<div><code>${escHtml(id)}</code> ${escHtml(t.slice(0, 80))}${t.length > 80 ? '…' : ''}</div>`;
  }).join('');
  const more = codes.length > max ? `<div class="atom-text-muted">…共 ${codes.length} 个原子</div>` : '';
  return rows + more;
}

function atomsOnBlockPage(block, side, pageIndex, peerBlock = null) {
  const codes = new Set(block?.atom_codes || []);
  let atoms = (state?.float_atoms || []).filter(
    (a) => a.book_side === side
      && Number(a.page_index) === Number(pageIndex)
      && codes.has(a.atom_code),
  );
  if (!atoms.length && side === 'new' && peerBlock) {
    const oldPg = (peerBlock.old_tb_pgs || [])[0] ?? pageIndex;
    const oldAtoms = (state?.float_atoms || []).filter(
      (a) => a.book_side === 'old'
        && Number(a.page_index) === Number(oldPg)
        && (peerBlock.atom_codes || []).includes(a.atom_code),
    );
    const snippet = (oldAtoms[0]?.text || peerBlock.block_name || '')
      .replace(/\s+/g, '')
      .slice(8, 28);
    if (snippet.length >= 6) {
      atoms = (state?.float_atoms || []).filter(
        (a) => a.book_side === 'new'
          && Number(a.page_index) === Number(pageIndex)
          && (a.text || '').replace(/\s+/g, '').includes(snippet.slice(0, 10)),
      );
    }
  }
  return atoms;
}

function pageHighlightLayer(block, side, pageIndex, peerBlock = null) {
  const atoms = atomsOnBlockPage(block, side, pageIndex, peerBlock);
  if (!atoms.length) return '';
  return atoms.map((a) => {
    const xs = Number(a.x_start) * 100;
    const ys = Number(a.y_start) * 100;
    const w = (Number(a.x_end) - Number(a.x_start)) * 100;
    const h = (Number(a.y_end) - Number(a.y_start)) * 100;
    const label = (a.text || a.atom_code || '').slice(0, 10);
    return `<div class="compare-atom-box ${side}" style="left:${xs}%;top:${ys}%;width:${w}%;height:${h}%" title="${escHtml(a.text || a.atom_code)}"><span>${escHtml(label)}</span></div>`;
  }).join('');
}

function textbookPanelsForBlock(block, side, pages, peerBlock = null) {
  if (!block) return '';
  const pgKey = side === 'old' ? 'old_tb_pgs' : 'new_tb_pgs';
  const pgs = [...new Set((block[pgKey] || []).map(Number).filter((n) => !Number.isNaN(n)))].sort((a, b) => a - b);
  if (!pgs.length) return textbookPanelHtml(block, side, null, pages, peerBlock);
  return pgs.map((pg) => textbookPanelHtml(block, side, pg, pages, peerBlock)).join('');
}

function textbookPanelHtml(block, side, pageIndex, pages, peerBlock = null) {
  const url = pageIndex ? pageUrl(pages, pageIndex) : '';
  const sideLabel = side === 'old' ? '旧教材' : '新教材';
  const name = block ? `${block.block_id} ${block.block_name || ''}` : '—';
  const pgLabel = pageIndex ? ` P${pageIndex}` : '';
  const atoms = block ? atomTextList(block, side, 24) : '';
  if (!block) {
    return `<div class="compare-preview-panel ${side}">
      <div class="cp-head">${sideLabel} — · ${escHtml(name)}</div>
      <div class="empty-cell">未选择区块</div>
    </div>`;
  }
  if (!pageIndex || !url) {
    return `<div class="compare-preview-panel ${side}">
      <div class="cp-head">${sideLabel}${pgLabel} · ${escHtml(name)}</div>
      <div class="compare-no-page-img">${pageIndex ? `教材 P${pageIndex} 暂无页图` : '未绑定教材页'}</div>
      <div class="atom-text-list atom-text-full">${atoms || '<div class="atom-text-muted">（无原子）</div>'}</div>
    </div>`;
  }
  const layer = pageHighlightLayer(block, side, pageIndex, peerBlock);
  return `<div class="compare-preview-panel ${side}">
    <div class="cp-head">${sideLabel}${pgLabel} · ${escHtml(name)}</div>
    <div class="compare-page-viewport">
      <img src="${url}" alt="${sideLabel} P${pageIndex}" loading="lazy">
      <div class="compare-atom-layer">${layer}</div>
    </div>
    <div class="atom-text-list atom-text-full">${atoms}</div>
  </div>`;
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 5000);
}

function escHtml(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function isConfirmed() {
  return Boolean(state?.compare_confirmed_at);
}

function formatCwRange(cwPgs) {
  if (!cwPgs || !cwPgs.length) return '—';
  const nums = cwPgs.map(Number).filter((n) => !Number.isNaN(n)).sort((a, b) => a - b);
  if (!nums.length) return '—';
  if (nums.length === 1) return `#${nums[0]}`;
  let consecutive = true;
  for (let i = 1; i < nums.length; i += 1) {
    if (nums[i] !== nums[i - 1] + 1) { consecutive = false; break; }
  }
  return consecutive ? `#${nums[0]}-${nums[nums.length - 1]}` : `#${nums.join(',')}`;
}

function matchSortKey(m) {
  const oldBlock = (state.old_blocks || []).find((b) => b.block_id === m.old_block_id);
  if (oldBlock && (oldBlock.cw_pgs || []).length) {
    return Math.min(...oldBlock.cw_pgs.map(Number));
  }
  const newBlock = (state.new_blocks || []).find((b) => b.block_id === m.new_block_id);
  if (newBlock && (newBlock.new_tb_pgs || []).length) {
    return 900 + Math.min(...newBlock.new_tb_pgs.map(Number));
  }
  return 9999;
}

function sortedMatches() {
  return [...(state.matches || [])].sort((a, b) => {
    const d = matchSortKey(a) - matchSortKey(b);
    if (d !== 0) return d;
    return String(a.match_id).localeCompare(String(b.match_id));
  });
}

function blockCellHtml(block, side) {
  if (!block) return '<span class="text-muted">—</span>';
  const pgs = (side === 'old' ? block.old_tb_pgs : block.new_tb_pgs || []).map((p) => `P${p}`).join('、') || '—';
  const cw = side === 'old' && (block.cw_pgs || []).length ? formatCwRange(block.cw_pgs) : '';
  const excerpt = blockExcerpt(block, side);
  return `<div class="block-label">${escHtml(block.block_id)} ${escHtml(block.block_name || '')}</div>`
    + `<div class="block-sub">${side === 'old' ? '教材' : '新教材'} ${pgs}${cw ? ` · 课件 ${cw}` : ''}</div>`
    + (excerpt ? `<div class="block-excerpt">${escHtml(excerpt)}</div>` : '');
}

/** 新区块 metadata 锚定是否指向本行旧块 */
function isAnchoredPair(oldBlock, newBlock) {
  if (!oldBlock || !newBlock) return false;
  const refs = (newBlock.metadata?.anchor_old_refs) || [];
  if (!refs.length) return false;
  const oldCodes = new Set([oldBlock.block_id, oldBlock.block_code].filter(Boolean));
  return refs.some((r) => {
    const code = String(r.old_block_code || r.block_id || '').trim();
    return oldCodes.has(code);
  });
}

function blockColClass(oldBlock, newBlock) {
  return isAnchoredPair(oldBlock, newBlock) ? ' block-anchored' : ' block-unmatched';
}

function aiShortLabel(code, label) {
  if (!code && !label) return '—';
  if (code && label) return label;
  return label || code || '—';
}

function aiSourceBadge(m) {
  const src = m.ai_source || '';
  if (src === 'llm_api' || src === 'llm_bailian') {
    const model = m.ai_model ? ` ${m.ai_model}` : '';
    return `<span class="ai-source-tag llm">AI${escHtml(model)}</span>`;
  }
  if (src === 'rules') {
    return '<span class="ai-source-tag rules">规则</span>';
  }
  return '';
}

function aiNoteHtml(m) {
  const note = (m.ai_teacher_note || '').trim();
  if (!note) {
    const badge = aiSourceBadge(m);
    return badge ? badge : '<span class="ai-empty">—</span>';
  }
  const short = note.length > 48 ? `${note.slice(0, 48)}…` : note;
  return `<div class="ai-note-text" title="${escHtml(note)}">${escHtml(short)}</div>${aiSourceBadge(m)}`;
}

function hasAiSuggestion(m) {
  return Boolean(
    (m.ai_reuse_action || '').trim()
    || (m.ai_change_type || '').trim()
    || (m.ai_teacher_note || '').trim(),
  );
}

function optionList(options, selected, emptyLabel, showCode = false) {
  const rows = [`<option value="">${emptyLabel}</option>`];
  (options || []).forEach((o) => {
    const sel = selected === o.code ? ' selected' : '';
    const label = showCode && o.code ? `${o.code} ${o.label}` : o.label;
    rows.push(`<option value="${escHtml(o.code)}"${sel}>${escHtml(label)}</option>`);
  });
  return rows.join('');
}

function effectiveActionForMatch(m) {
  if (!m) return 'pending';
  const code = (m.reuse_action || m.ai_reuse_action || '').trim();
  return code || 'pending';
}

function actionClass(code) {
  return `action-${code || 'pending'}`;
}

function actionLabel(code) {
  if (!code || code === 'pending') return '待判定';
  const opt = (state?.reuse_action_options || []).find((o) => o.code === code);
  return opt?.label || code;
}

function matchForNewBlock(blockId) {
  return (state?.matches || []).find((m) => m.new_block_id === blockId);
}

function matchForOldBlock(blockId) {
  return (state?.matches || []).find((m) => m.old_block_id === blockId);
}

function updateHeroSummary() {
  const el = document.getElementById('hero-status');
  if (!el || !state) return;
  const matches = state.matches || [];
  const matchedOld = new Set(matches.map((m) => m.old_block_id).filter(Boolean));
  const matchedNew = new Set(matches.map((m) => m.new_block_id).filter(Boolean));
  const orphanNew = (state.new_blocks || []).filter((b) => !matchedNew.has(b.block_id)).length;
  const orphanOld = (state.old_blocks || []).filter((b) => !matchedOld.has(b.block_id)).length;
  const pendingReuse = matches.filter((m) => !m.reuse_action).length;
  const parts = [];
  if (state.compare_confirmed_at) {
    el.className = 'compare-chrome-status is-confirmed';
    parts.push(`教研已确认 · ${String(state.compare_confirmed_at).slice(0, 10)}`);
  } else {
    el.className = 'compare-chrome-status is-draft';
    parts.push('草稿');
    parts.push(`已配 ${matches.length}`);
    if (pendingReuse) parts.push(`待判定 ${pendingReuse}`);
    if (orphanNew || orphanOld) parts.push(`未配对 新${orphanNew}/旧${orphanOld}`);
  }
  el.textContent = parts.join(' · ');
}

function updateStatusBanner() {
  updateHeroSummary();
}

function updateProgressBadge() {
  updateHeroSummary();
}

function updateConfirmControls() {
  const btn = document.getElementById('btn-confirm');
  const unlock = document.getElementById('btn-unlock');
  if (!btn) return;
  if (isConfirmed()) {
    btn.hidden = true;
    if (unlock) {
      unlock.hidden = false;
      unlock.disabled = false;
    }
  } else {
    btn.hidden = false;
    btn.disabled = false;
    btn.textContent = '教研已确认';
    if (unlock) unlock.hidden = true;
  }
}

function updateQueueNavButtons() {
  const items = filteredQueueItems();
  const prev = document.getElementById('btn-prev-block');
  const next = document.getElementById('btn-next-block');
  if (!prev || !next) return;
  const idx = findCurrentQueueIndex(items);
  prev.disabled = !items.length || idx <= 0;
  next.disabled = !items.length || (idx >= 0 && idx >= items.length - 1);
}

function updateToolbarState() {
  updateConfirmControls();
  syncViewToolbar();
  const readonly = isConfirmed();
  const pairBtns = ['btn-apply-anchor', 'btn-add-match', 'btn-showcase-replay'];
  const auditBtns = ['btn-auto-suggest', 'btn-apply-all-ai'];
  pairBtns.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = readonly;
    if (readonly) el.title = '已教研确认，请先点「解锁编辑」';
  });
  auditBtns.forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.disabled = readonly;
    if (readonly) el.title = '已教研确认，请先点「解锁编辑」';
  });
  const applyAll = document.getElementById('btn-apply-all-ai');
  if (applyAll && !readonly) {
    const hasAi = (state?.matches || []).some((m) => hasAiSuggestion(m));
    applyAll.disabled = !hasAi;
    applyAll.title = hasAi
      ? '将 AI 建议填入所有空的教研判定栏'
      : '请先生成 AI 建议后再采纳';
  }
  updateQueueNavButtons();
  updateGoWorkbenchHighlight();
}

function annotateReturnQuery() {
  const ret = `/new-library/lessons/${encodeURIComponent(LESSON_UID)}/compare`;
  return `from=compare&return=${encodeURIComponent(ret)}`;
}

function updatePipelineLinks(les) {
  const q = annotateReturnQuery();
  const newNav = document.getElementById('link-annotate-nav');
  const oldNav = document.getElementById('link-old-annotate-nav');
  if (newNav) {
    newNav.href = `/new-library/lessons/${encodeURIComponent(LESSON_UID)}/annotate?${q}`;
  }
  if (oldNav && les.old_lesson_uid) {
    oldNav.href = `/old-library/lessons/${encodeURIComponent(les.old_lesson_uid)}/annotate?${q}`;
    oldNav.hidden = false;
  } else if (oldNav) {
    oldNav.hidden = true;
  }
}

function pageUrl(pages, pg) {
  const row = (pages || []).find((p) => Number(p.pg ?? p.page_index) === Number(pg));
  return row?.url || '';
}

function renderComparePreview(oldBlock, newBlock, panelId, hintId) {
  const panel = document.getElementById(panelId || 'review-preview');
  const hint = document.getElementById(hintId || 'review-preview-hint');
  if (!panel) return;
  panel.classList.remove('canvas-stacked');
  panel.classList.add('canvas-side');
  if (!oldBlock && !newBlock) {
    panel._compareBridgeCleanup?.();
    panel._compareBridgeCleanup = null;
    panel.innerHTML = panelId === 'pair-preview'
      ? '<div class="pair-preview-empty">选择匹配对以查看新旧教材页与旧课件缩略图</div>'
      : '<div class="empty-cell">点击表中一行查看对照</div>';
    return;
  }
  const title = [];
  if (oldBlock) title.push(`${oldBlock.block_id} ${oldBlock.block_name || ''}`);
  if (newBlock) title.push(`${newBlock.block_id} ${newBlock.block_name || ''}`);
  if (hint) hint.textContent = title.join(' ↔ ') || '对照预览';

  const cwHtml = oldBlock && (oldBlock.cw_pgs || []).length
    ? `<div class="compare-preview-panel cw-panel" style="grid-column:1/-1;">
        <div class="cp-head">旧课件 ${(oldBlock.cw_pgs || []).map((p) => `P${p}`).join('、')}</div>
        <div class="cw-strip">${(oldBlock.cw_pgs || []).map((p) => {
          const c = (state.cw_pages || []).find((x) => Number(x.pg) === Number(p));
          return c?.url
            ? `<img src="${c.url}" alt="课件 P${p}" loading="lazy" title="课件 P${p}">`
            : `<span class="cw-thumb-missing">P${p}</span>`;
        }).join('')}</div>
      </div>`
    : '';

  panel._compareBridgeCleanup?.();
  panel._compareBridgeCleanup = null;

  panel.innerHTML = `
    <div class="compare-preview-grid${oldBlock && newBlock ? ' is-paired' : ''}">
      ${cwHtml}
      ${textbookPanelsForBlock(oldBlock, 'old', state.old_pages, newBlock)}
      ${textbookPanelsForBlock(newBlock, 'new', state.new_pages, oldBlock)}
    </div>`;
  const grid = panel.querySelector('.compare-preview-grid');
  const shouldAutoScroll = panelId === 'wb-preview' && oldBlock && newBlock;
  if (grid) ensureCompareBridgeObserver(grid, oldBlock, newBlock, panel);
  const finalizePreview = () => scheduleCompareBridgeRefresh(oldBlock, newBlock, panel, {
    scrollIntoView: shouldAutoScroll,
  });
  panel.querySelectorAll('.compare-page-viewport img').forEach((img) => {
    img.addEventListener('load', () => {
      const layer = img.nextElementSibling;
      if (layer) layer.style.height = `${img.clientHeight}px`;
      finalizePreview();
    });
    if (img.complete) finalizePreview();
  });
  if (!panel.querySelector('.compare-page-viewport img')) finalizePreview();
}

function scrollHighlightsInViewport(viewport, side) {
  if (!viewport) return;
  const boxes = [...viewport.querySelectorAll(`.compare-atom-box.${side}`)];
  const motion = prefersReducedMotion() ? 'auto' : 'smooth';
  if (boxes.length) {
    const layer = viewport.querySelector('.compare-atom-layer');
    const baseTop = layer ? layer.offsetTop : 0;
    let minTop = Infinity;
    let maxBottom = 0;
    boxes.forEach((box) => {
      const top = baseTop + box.offsetTop;
      const bottom = top + box.offsetHeight;
      minTop = Math.min(minTop, top);
      maxBottom = Math.max(maxBottom, bottom);
    });
    const center = (minTop + maxBottom) / 2;
    viewport.scrollTo({
      top: Math.max(0, center - viewport.clientHeight * 0.4),
      behavior: motion,
    });
    return;
  }
  const img = viewport.querySelector('img');
  if (!img) return;
  const imgTop = img.offsetTop;
  const imgCenter = imgTop + img.offsetHeight / 2;
  viewport.scrollTo({
    top: Math.max(0, imgCenter - viewport.clientHeight * 0.4),
    behavior: motion,
  });
}

function scrollCompareHighlightsIntoView(panelEl) {
  if (!panelEl) return;
  const grid = panelEl.querySelector('.compare-preview-grid');
  if (!grid) return;
  scrollHighlightsInViewport(
    grid.querySelector('.compare-preview-panel.old .compare-page-viewport'),
    'old',
  );
  scrollHighlightsInViewport(
    grid.querySelector('.compare-preview-panel.new .compare-page-viewport'),
    'new',
  );
}

function scheduleCompareBridgeRefresh(oldBlock, newBlock, panel, { scrollIntoView = false } = {}) {
  const grid = panel?.querySelector('.compare-preview-grid');
  if (!grid) return;
  const run = () => {
    if (scrollIntoView && panel.id === 'wb-preview' && oldBlock && newBlock) {
      scrollCompareHighlightsIntoView(panel);
    }
    updateCompareBridge(oldBlock, newBlock, panel);
  };
  requestAnimationFrame(() => requestAnimationFrame(run));
}

function sidePanelImg(grid, side) {
  const panel = grid.querySelector(`.compare-preview-panel.${side}`);
  return panel?.querySelector('.compare-page-viewport img') || null;
}

/** 高亮区矩形（grid 内坐标）；优先原子框，否则退回教材页图片区域（不含标题栏） */
function domHighlightRect(grid, side) {
  const gridRect = grid.getBoundingClientRect();
  const boxes = [...grid.querySelectorAll(`.compare-preview-panel.${side} .compare-atom-box.${side}`)];
  if (boxes.length) {
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    boxes.forEach((box) => {
      const r = box.getBoundingClientRect();
      left = Math.min(left, r.left);
      top = Math.min(top, r.top);
      right = Math.max(right, r.right);
      bottom = Math.max(bottom, r.bottom);
    });
    return {
      left: left - gridRect.left,
      top: top - gridRect.top,
      right: right - gridRect.left,
      bottom: bottom - gridRect.top,
      fromAtoms: true,
    };
  }
  const img = sidePanelImg(grid, side);
  if (img && img.clientWidth > 0 && img.clientHeight > 0) {
    const r = img.getBoundingClientRect();
    return {
      left: r.left - gridRect.left,
      top: r.top - gridRect.top,
      right: r.right - gridRect.left,
      bottom: r.bottom - gridRect.top,
      fromAtoms: false,
    };
  }
  return null;
}

/** 旧侧有高亮、新侧无时：按旧高亮在旧页图中的相对位置，映射到新页图 */
function mapHighlightCornerToNewImg(grid, oldRect, corner) {
  const oldImg = sidePanelImg(grid, 'old');
  const newImg = sidePanelImg(grid, 'new');
  const gridRect = grid.getBoundingClientRect();
  if (!oldImg || !newImg || !oldRect) return null;
  const oR = oldImg.getBoundingClientRect();
  const nR = newImg.getBoundingClientRect();
  if (oR.width <= 0 || oR.height <= 0 || nR.width <= 0 || nR.height <= 0) return null;
  const relL = (oldRect.left + gridRect.left - oR.left) / oR.width;
  const relT = (oldRect.top + gridRect.top - oR.top) / oR.height;
  const relR = (oldRect.right + gridRect.left - oR.left) / oR.width;
  const relB = (oldRect.bottom + gridRect.top - oR.top) / oR.height;
  const x = corner === 'tl'
    ? nR.left + relL * nR.width
    : nR.left + relR * nR.width;
  const y = corner === 'tl'
    ? nR.top + relT * nR.height
    : nR.top + relB * nR.height;
  return { x: x - gridRect.left, y: y - gridRect.top };
}

function compareBridgeEndpoints(grid) {
  const oldRect = domHighlightRect(grid, 'old');
  const newRect = domHighlightRect(grid, 'new');
  if (!oldRect) return null;
  const oldHasAtoms = oldRect.fromAtoms;
  const newHasAtoms = Boolean(newRect?.fromAtoms);

  const from = { x: oldRect.right, y: oldRect.bottom };

  let to;
  if (newHasAtoms && newRect) {
    to = { x: newRect.left, y: newRect.top };
  } else if (oldHasAtoms) {
    to = mapHighlightCornerToNewImg(grid, oldRect, 'tl');
  } else if (newRect) {
    to = { x: newRect.left, y: newRect.top };
  }
  if (!to) return null;
  return { from, to, inferredNew: oldHasAtoms && !newHasAtoms };
}

function ensureCompareBridgeObserver(grid, oldBlock, newBlock, panel) {
  if (panel._compareBridgeCleanup) {
    panel._compareBridgeCleanup();
    panel._compareBridgeCleanup = null;
  }
  let rafId = 0;
  const refresh = () => {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(() => {
      rafId = 0;
      updateCompareBridge(oldBlock, newBlock, panel);
    });
  };
  grid._compareBridgeObs = new ResizeObserver(refresh);
  grid._compareBridgeObs.observe(grid);
  grid.querySelectorAll('.compare-preview-panel.old, .compare-preview-panel.new').forEach((el) => {
    grid._compareBridgeObs.observe(el);
  });
  const viewports = [...grid.querySelectorAll('.compare-page-viewport')];
  viewports.forEach((vp) => vp.addEventListener('scroll', refresh, { passive: true }));
  panel._compareBridgeCleanup = () => {
    grid._compareBridgeObs?.disconnect();
    viewports.forEach((vp) => vp.removeEventListener('scroll', refresh));
    if (rafId) cancelAnimationFrame(rafId);
  };
}

function updateCompareBridge(oldBlock, newBlock, panel) {
  if (!panel) return;
  panel.querySelector('.compare-pair-bridge')?.remove();
  const grid = panel.querySelector('.compare-preview-grid');
  if (!grid || !oldBlock || !newBlock) return;

  const endpoints = compareBridgeEndpoints(grid);
  if (!endpoints) return;
  const { from, to, inferredNew } = endpoints;

  const uid = panel.id || 'preview';
  const gradId = `pair-bridge-grad-${uid}`;
  const arrowId = `pair-bridge-arrow-${uid}`;
  const midX = (from.x + to.x) / 2;
  const midY = (from.y + to.y) / 2;

  const bridge = document.createElement('div');
  bridge.className = 'compare-pair-bridge';
  bridge.innerHTML = `
    <svg class="compare-pair-bridge-svg" aria-hidden="true">
      <defs>
        <linearGradient id="${gradId}" gradientUnits="userSpaceOnUse"
          x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}">
          <stop offset="0%" stop-color="#ea580c"/>
          <stop offset="55%" stop-color="#a855f7"/>
          <stop offset="100%" stop-color="#0d9488"/>
        </linearGradient>
        <marker id="${arrowId}" markerWidth="8" markerHeight="8" refX="7" refY="4"
          orient="auto" markerUnits="strokeWidth">
          <path d="M0,0 L8,4 L0,8 Z" fill="#0d9488"/>
        </marker>
      </defs>
      <line class="compare-pair-bridge-glow"
        x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}"/>
      <line class="compare-pair-bridge-line"
        x1="${from.x}" y1="${from.y}" x2="${to.x}" y2="${to.y}"
        stroke="url(#${gradId})" marker-end="url(#${arrowId})"/>
      <circle class="compare-pair-bridge-dot old" cx="${from.x}" cy="${from.y}" r="5"/>
      <circle class="compare-pair-bridge-dot new${inferredNew ? ' inferred' : ''}" cx="${to.x}" cy="${to.y}" r="5"/>
    </svg>
    <span class="compare-pair-bridge-label"
      style="left:${midX}px;top:${midY}px">${escHtml(oldBlock.block_id)} → ${escHtml(newBlock.block_id)}</span>`;
  grid.appendChild(bridge);
}

function clearMatchSelection() {
  selectedMatchId = null;
  selectedOrphan = null;
  renderReviewTable();
  refreshPairView();
  document.querySelectorAll('.block-card').forEach((c) => c.classList.remove('selected'));
  renderComparePreview(null, null);
  renderComparePreview(null, null, 'pair-preview', 'pair-preview-hint');
  renderComparePreview(null, null, 'wb-preview', 'wb-preview-hint');
  const detail = document.getElementById('detail-content');
  if (detail) {
    detail.innerHTML = '<div class="pair-detail-empty">选中一条匹配后填写判定</div>';
  }
  const wbDetail = document.getElementById('wb-detail-content');
  if (wbDetail) {
    wbDetail.innerHTML = '<div class="pair-detail-empty">选中一条匹配后填写判定</div>';
  }
  if (viewMode === 'pair') requestAnimationFrame(drawAllMatchLines);
  if (viewMode === 'workbench') refreshWorkbenchView();
  updateQueueNavButtons();
}

function selectMatch(matchId) {
  if (selectedMatchId === matchId) {
    clearMatchSelection();
    toast('已取消选中');
    updateQueueNavButtons();
    return;
  }
  selectedMatchId = matchId;
  selectedOrphan = null;
  renderReviewTable();
  refreshPairView();
  const m = (state.matches || []).find((x) => x.match_id === matchId);
  if (!m) return;
  const oldBlock = (state.old_blocks || []).find((b) => b.block_id === m.old_block_id);
  const newBlock = (state.new_blocks || []).find((b) => b.block_id === m.new_block_id);
  highlightMatchVisuals(m);
  renderComparePreview(oldBlock, newBlock);
  renderComparePreview(oldBlock, newBlock, 'pair-preview', 'pair-preview-hint');
  renderComparePreview(oldBlock, newBlock, 'wb-preview', 'wb-preview-hint');
  if (viewMode !== 'pair') renderMatchDetail(m);
  if (viewMode === 'workbench') refreshWorkbenchView();
  else if (viewMode === 'pair') requestAnimationFrame(drawAllMatchLines);
  updateQueueNavButtons();
}

function renderReviewTable() {
  const tbody = document.getElementById('review-table-body');
  if (!tbody || !state) return;
  const readonly = isConfirmed();
  const rows = sortedMatches();

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-cell">暂无配对 · 从锚定导入、添加配对，或回新侧建块写锚定</td></tr>';
    updateStatusBanner();
    updateProgressBadge();
    updateToolbarState();
    return;
  }

  const makeMatchRow = (m, rowIdx) => {
    const oldBlock = (state.old_blocks || []).find((b) => b.block_id === m.old_block_id);
    const newBlock = (state.new_blocks || []).find((b) => b.block_id === m.new_block_id);
    const cw = oldBlock ? formatCwRange(oldBlock.cw_pgs) : '—';
    const missing = !m.reuse_action || !String(m.teacher_note || '').trim();
    const sel = selectedMatchId === m.match_id ? ' selected' : '';
    const missCls = missing && !readonly ? ' row-missing' : '';
    const stripeCls = rowIdx % 2 === 0 ? ' row-even' : ' row-odd';
    const dis = readonly ? ' disabled' : '';
    const blockBg = blockColClass(oldBlock, newBlock);
    const orphanCls = (!m.old_block_id || !m.new_block_id) ? ' review-row--orphan' : '';
    const aiBtn = !readonly && hasAiSuggestion(m)
      ? `<button type="button" class="btn btn-sm btn-outline apply-ai-btn" data-id="${m.match_id}" title="采纳 AI 建议到教研列">AI</button> `
      : '';
    const pairBtn = !readonly && (!m.old_block_id || !m.new_block_id)
      ? `<button type="button" class="btn btn-sm btn-outline pair-orphan-btn" data-old="${m.old_block_id || ''}" data-new="${m.new_block_id || ''}" title="补全另一侧区块">配对</button> `
      : '';
    return `<tr class="review-row${orphanCls}${sel}${missCls}${stripeCls}" data-match-id="${m.match_id}">
      <td class="mono col-cw" onclick="selectMatch('${m.match_id}')">${escHtml(cw)}</td>
      <td class="col-block${blockBg}" onclick="selectMatch('${m.match_id}')">${blockCellHtml(oldBlock, 'old')}</td>
      <td class="col-block${blockBg}" onclick="selectMatch('${m.match_id}')">${blockCellHtml(newBlock, 'new')}</td>
      <td class="col-teacher-sm"><select class="review-action"${dis} data-field="reuse_action" data-id="${m.match_id}">${optionList(state.reuse_action_options, m.reuse_action, '— 请选择 —')}</select></td>
      <td class="col-teacher-sm"><select class="review-change"${dis} data-field="change_type" data-id="${m.match_id}">${optionList(state.change_type_options, m.change_type, '—', true)}</select></td>
      <td class="col-teacher-note"><textarea class="review-note"${dis} data-field="teacher_note" data-id="${m.match_id}" placeholder="写清：旧课件哪页→新教材哪块→怎么改…">${escHtml(m.teacher_note || '')}</textarea></td>
      <td class="col-act">${readonly ? '' : `${aiBtn}${pairBtn}<button type="button" class="btn btn-sm btn-outline del-match" data-id="${m.match_id}">删</button>`}</td>
    </tr>`;
  };

  const paired = rows.filter((m) => m.old_block_id && m.new_block_id);
  const oldOnly = rows.filter((m) => m.old_block_id && !m.new_block_id);
  const newOnly = rows.filter((m) => !m.old_block_id && m.new_block_id);

  let rowIdx = 0;
  let html = paired.map((m) => makeMatchRow(m, rowIdx++)).join('');

  if (oldOnly.length) {
    html += `<tr class="review-row-section-header"><td colspan="7">旧侧未匹配区块（${oldOnly.length}）</td></tr>`;
    html += oldOnly.map((m) => makeMatchRow(m, rowIdx++)).join('');
  }
  if (newOnly.length) {
    html += `<tr class="review-row-section-header"><td colspan="7">新侧未匹配区块（${newOnly.length}）</td></tr>`;
    html += newOnly.map((m) => makeMatchRow(m, rowIdx++)).join('');
  }

  tbody.innerHTML = html;

  tbody.querySelectorAll('.review-action, .review-change').forEach((el) => {
    el.addEventListener('change', () => updateMatchField(el.dataset.id, el.dataset.field, el.value));
  });
  tbody.querySelectorAll('.review-note').forEach((el) => {
    el.addEventListener('blur', () => updateMatchField(el.dataset.id, el.dataset.field, el.value));
  });
  tbody.querySelectorAll('.apply-ai-btn').forEach((el) => {
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      applyAiSuggestion(el.dataset.id, false);
    });
  });
  tbody.querySelectorAll('.del-match').forEach((el) => {
    el.addEventListener('click', (ev) => {
      ev.stopPropagation();
      deleteMatch(el.dataset.id);
    });
  });
  tbody.querySelectorAll('.pair-orphan-btn').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openPairOrphanMatch(btn.dataset.old || null, btn.dataset.new || null);
    });
  });

  updateStatusBanner();
  updateProgressBadge();
  updateToolbarState();
}

function renderReuseReport() {
  const panel = document.getElementById('reuse-report-panel');
  const summaryEl = document.getElementById('reuse-report-summary');
  const ratioEl = document.getElementById('reuse-report-ratio');
  const report = state?.reuse_report;
  if (!panel || !summaryEl) return;
  if (!report?.summary) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  summaryEl.textContent = report.summary;
  if (ratioEl) {
    const pct = report.reuse_ratio != null ? `${Math.round(report.reuse_ratio * 100)}%` : '—';
    ratioEl.textContent = `可复用比例 ${pct}`;
  }
}

async function refreshReuseReport() {
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/export/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '汇总失败');
    return;
  }
  const dr = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/data`);
  const ws = await dr.json();
  if (dr.ok && ws.ok) setState(ws);
  renderReuseReport();
  toast('已更新复用报告与 export.json');
}

function downloadExportJson() {
  window.open(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/export.json`, '_blank');
}

function downloadExportXlsx() {
  window.open(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/export.xlsx`, '_blank');
}

async function downloadExportPptx() {
  const btn = document.getElementById('btn-export-pptx');
  if (btn) btn.disabled = true;
  toast('正在生成 PPTX（需 Node.js + scripts/pptx npm install）…');
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/export/pptx`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'student' }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || 'PPTX 生成失败');
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `课件_${LESSON_UID}.pptx`;
    a.click();
    URL.revokeObjectURL(url);
    toast('PPTX 已下载');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadCompareNav(les) {
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/nav`);
    const nav = await r.json();
    if (!r.ok || !nav.ok) return;

    const params = new URLSearchParams(window.location.search);
    const volCode = params.get('volume') || les.volume_code || nav.volume_code;
    const wbUrl = volCode
      ? `/new-library/compare-results?volume=${encodeURIComponent(volCode)}&status=pending`
      : '/new-library/compare-results?status=pending';

    const wb = document.getElementById('link-volume-workbench');
    if (wb) wb.href = wbUrl;
    const backCtx = document.getElementById('link-back-context');
    if (backCtx) backCtx.href = wbUrl;

    const next = document.getElementById('link-next-lesson');
    if (next && nav.next_lesson) {
      const nq = volCode ? `?volume=${encodeURIComponent(volCode)}` : '';
      next.href = `${nav.next_lesson.compare_url}${nq}`;
      next.textContent = '下一课 →';
      next.title = nav.next_lesson.lesson_label || '';
      next.hidden = false;
    } else if (next) {
      next.hidden = true;
    }
  } catch {
    /* nav is optional */
  }
}

async function loadData() {
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/data`);
  const data = await r.json();
  if (!r.ok || !data.ok) throw new Error(data.error || '加载失败');
  setState(data);
  await bootstrapCompareMatches();
  const les = state.lesson || data.lesson || {};
  const volEl = document.getElementById('hero-volume');
  if (volEl) volEl.textContent = les.volume_title || les.volume_code || '';
  document.getElementById('page-title').textContent = les.lesson_name || LESSON_UID;
  updatePipelineLinks(les);
  updateHeroSummary();
  updateToolbarState();
  await loadCompareNav(les);
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  renderReuseReport();
  setViewMode('pair');
  requestAnimationFrame(() => runShowcaseAnimation());
}

function autoSelectInitialMatch() {
  if (selectedMatchId) return;
  const matches = state?.matches || [];
  const paired = matches.filter((m) => m.old_block_id && m.new_block_id);
  if (paired.length) {
    selectMatch(paired[0].match_id);
    return;
  }
  const first = matches.find((m) => m.old_block_id || m.new_block_id) || matches[0];
  if (first?.match_id) selectMatch(first.match_id);
}

async function updateMatchField(matchId, field, value) {
  if (isConfirmed()) return;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(matchId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [field]: value }),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '保存失败');
    return;
  }
  setState(data);
  selectedMatchId = matchId;
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  const m = (data.matches || []).find((x) => x.match_id === matchId);
  if (m) renderMatchDetail(m);
}

async function deleteMatch(matchId) {
  if (!confirm('确定删除这条配对？')) return;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(matchId)}`, {
    method: 'DELETE',
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '删除失败');
    return;
  }
  setState(data);
  if (selectedMatchId === matchId) selectedMatchId = null;
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  renderComparePreview(null, null);
  renderComparePreview(null, null, 'pair-preview', 'pair-preview-hint');
  renderComparePreview(null, null, 'wb-preview', 'wb-preview-hint');
  const detail = document.getElementById('detail-content');
  if (detail) detail.innerHTML = '<div class="pair-detail-empty">选中一条匹配后填写判定</div>';
  const wbDetail = document.getElementById('wb-detail-content');
  if (wbDetail) wbDetail.innerHTML = '<div class="pair-detail-empty">选中一条匹配后填写判定</div>';
}

async function applyAnchors() {
  if (isConfirmed()) return;
  if (!confirm('从新区块锚定 metadata 导入配对？已有配对保留，仅补充缺失项。')) return;
  const btn = document.getElementById('btn-apply-anchor');
  if (btn) btn.disabled = true;
  try {
    const n = await applyAnchorsSilent();
    if (n <= 0 && !hasAnchorRefsOnNewBlocks()) {
      toast('新区块暂无锚定信息');
      return;
    }
    await ensureOrphanMatches();
    toast(n > 0 ? `已导入 ${n} 条锚定配对` : '锚定配对已是最新');
    renderReviewTable();
    refreshPairView();
    refreshWorkbenchView();
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openAddMatchDialog(presetOldId = null, presetNewId = null, existingMatchId = null) {
  if (isConfirmed()) {
    toast('对比已锁定，请先解锁');
    return;
  }
  const dlg = document.getElementById('add-match-dialog');
  const oldSel = document.getElementById('add-old-block');
  const newSel = document.getElementById('add-new-block');
  if (existingMatchId) dlg.dataset.editMatchId = existingMatchId;
  else delete dlg.dataset.editMatchId;
  oldSel.innerHTML = '<option value="">— 可选 —</option>'
    + (state.old_blocks || []).map((b) => `<option value="${b.block_id}">${escHtml(blockOptionLabel(b, 'old'))}</option>`).join('');
  newSel.innerHTML = '<option value="">— 可选 —</option>'
    + (state.new_blocks || []).map((b) => `<option value="${b.block_id}">${escHtml(blockOptionLabel(b, 'new'))}</option>`).join('');
  if (presetOldId) oldSel.value = presetOldId;
  if (presetNewId) newSel.value = presetNewId;
  oldSel.onchange = previewFromBlockSelects;
  newSel.onchange = previewFromBlockSelects;
  previewFromBlockSelects();
  dlg.showModal();
}

function openPairOrphanMatch(oldId, newId) {
  const oid = oldId || null;
  const nid = newId || null;
  const existing = (state?.matches || []).find((m) => (
    (oid && m.old_block_id === oid && !m.new_block_id)
    || (nid && m.new_block_id === nid && !m.old_block_id)
  ));
  if (existing) {
    openAddMatchDialog(existing.old_block_id, existing.new_block_id, existing.match_id);
    return;
  }
  openAddMatchDialog(oid, nid);
}

async function submitAddMatch(ev) {
  ev.preventDefault();
  const dlg = document.getElementById('add-match-dialog');
  const editMatchId = dlg?.dataset?.editMatchId || '';
  const oldCode = document.getElementById('add-old-block').value;
  const newCode = document.getElementById('add-new-block').value;
  if (!oldCode && !newCode) {
    toast('请至少选择一侧区块');
    return;
  }
  const payload = {
    old_block_id: oldCode || null,
    new_block_id: newCode || null,
  };
  const url = editMatchId
    ? `${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(editMatchId)}`
    : `${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches`;
  const r = await fetch(url, {
    method: editMatchId ? 'PATCH' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || (editMatchId ? '更新失败' : '创建失败'));
    return;
  }
  setState(data);
  dlg.close();
  delete dlg.dataset.editMatchId;
  const matchId = editMatchId || (data.matches || []).slice(-1)[0]?.match_id;
  if (matchId) selectedMatchId = matchId;
  toast(editMatchId ? '已更新配对' : '已创建配对');
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  if (matchId) selectMatch(matchId);
}

async function applyAiSuggestion(matchId, overwrite) {
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(matchId)}/apply-ai`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ overwrite: !!overwrite }),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '采纳失败');
    return;
  }
  setState(data);
  selectedMatchId = matchId;
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  toast('已采纳 AI 建议');
}

function countPairedMatches() {
  return (state?.matches || []).filter((m) => m.old_block_id && m.new_block_id).length;
}

function hasAnchorRefsOnNewBlocks() {
  return (state?.new_blocks || []).some((b) => (b.metadata?.anchor_old_refs || []).length > 0);
}

async function applyAnchorsSilent() {
  if (isConfirmed()) return 0;
  try {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/apply-anchors`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) {
      console.warn('apply-anchors failed', data.error || r.status);
      return 0;
    }
    setState(data);
    return data.created_count || 0;
  } catch (err) {
    console.warn('apply-anchors error', err);
    return 0;
  }
}

async function bootstrapCompareMatches() {
  if (isConfirmed()) return false;
  let changed = false;
  const n = await applyAnchorsSilent();
  if (n > 0) changed = true;
  if (await ensureOrphanMatches()) changed = true;
  return changed;
}

async function ensureOrphanMatches() {
  if (isConfirmed()) return false;
  const matchedOld = new Set((state?.matches || []).map((m) => m.old_block_id).filter(Boolean));
  const matchedNew = new Set((state?.matches || []).map((m) => m.new_block_id).filter(Boolean));
  const orphanOld = (state?.old_blocks || []).filter((b) => !matchedOld.has(b.block_id));
  const orphanNew = (state?.new_blocks || []).filter((b) => !matchedNew.has(b.block_id));
  if (!orphanOld.length && !orphanNew.length) return false;

  let changed = false;
  for (const b of orphanOld) {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_block_id: b.block_id, new_block_id: null }),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      setState(data);
      changed = true;
    }
  }
  for (const b of orphanNew) {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_block_id: null, new_block_id: b.block_id }),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      setState(data);
      changed = true;
    }
  }
  return changed;
}

async function createOrphanMatches() {
  return ensureOrphanMatches();
}

async function runAutoSuggest(overwrite) {
  const btn = document.getElementById('btn-auto-suggest');
  const hadSuggestions = (state?.matches || []).some((m) => hasAiSuggestion(m));
  if (btn) btn.disabled = true;
  toast('正在为未匹配区块建立配对并调用 AI 模型，请稍候…');
  try {
    // 先为 orphan 区块创建单侧 match
    await createOrphanMatches();

    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/auto-suggest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ overwrite: !!overwrite, use_llm: true }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) {
      toast(data.error || '生成失败');
      return;
    }
    setState(data);
    if (!data.suggested_count && hadSuggestions && !overwrite) {
      toast('未覆盖已有建议。若要改用 AI 建议，请确认「覆盖重新生成」');
      return;
    }

    // AI 建议直接 apply 到教研列（空栏才填，已填的保留）
    const toApply = (data.matches || []).filter((m) => hasAiSuggestion(m));
    for (const m of toApply) {
      const needApply = overwrite || !m.reuse_action || !m.change_type || !String(m.teacher_note || '').trim();
      if (!needApply) continue;
      const r2 = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(m.match_id)}/apply-ai`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overwrite: !!overwrite }),
      });
      const d2 = await r2.json();
      if (r2.ok && d2.ok) setState(d2);
    }

    const llmNote = data.llm_enabled && data.llm_used_count
      ? `（AI ${data.llm_used_count} 条）`
      : '（规则引擎）';
    toast(data.llm_warning || `已生成并填入 ${data.suggested_count || 0} 条建议${llmNote}，可直接编辑修改`);
    renderReviewTable();
    refreshPairView();
    refreshWorkbenchView();
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function applyAllAiSuggestions() {
  const rows = (state?.matches || []).filter((m) => hasAiSuggestion(m));
  if (!rows.length) {
    toast('请先生成 AI 建议');
    return;
  }
  let ok = 0;
  for (const m of rows) {
    const need = !m.reuse_action || !m.change_type || !String(m.teacher_note || '').trim();
    if (!need) continue;
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(m.match_id)}/apply-ai`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ overwrite: false }),
    });
    const data = await r.json();
    if (r.ok && data.ok) {
      setState(data);
      ok += 1;
    }
  }
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  toast(ok ? `已采纳 ${ok} 条空栏` : '教研栏均已填写，无需采纳');
}

const CW_HEAVY_KWS = ['典例', '练习', '尾页', '名称页', '课件名称', '练习提醒'];
const NEW_ONLY_KWS = ['安全警示', '指南车信箱', '评价量规', '反思评价'];

function isOldCwHeavyBlock(b) {
  const name = (b.block_name || '').trim();
  const atoms = (b.atom_codes || []).length;
  const cw = (b.cw_pgs || []).length;
  if (cw > 0 && atoms === 0) return true;
  if (cw > 0 && CW_HEAVY_KWS.some((kw) => name.includes(kw))) return true;
  return false;
}

function isNewOnlyBlock(b) {
  const name = (b.block_name || '').trim();
  if (NEW_ONLY_KWS.some((kw) => name.includes(kw))) return true;
  const refs = (b.metadata && b.metadata.anchor_old_refs) || [];
  if (!refs.length && /安全|信箱|量规|评价/.test(name)) return true;
  return false;
}

function syncViewTabs() {
  document.querySelectorAll('.view-tab').forEach((btn) => {
    const mode = btn.id === 'btn-view-workbench' ? 'workbench'
      : btn.id === 'btn-view-pair' ? 'pair'
        : btn.id === 'btn-view-review' ? 'review' : '';
    const on = viewMode === mode;
    btn.classList.toggle('active', on);
    btn.classList.toggle('btn-primary', on);
    btn.classList.toggle('btn-outline', !on);
  });
}

function syncViewToolbar() {
  document.querySelectorAll('.toolbar-stage').forEach((el) => {
    el.hidden = el.dataset.toolbarStage !== viewMode;
  });
}

function setViewMode(mode) {
  viewMode = mode;
  const workbench = document.getElementById('workbench-panel');
  const review = document.getElementById('review-panel');
  const pair = document.getElementById('pair-panel');
  if (workbench) workbench.hidden = mode !== 'workbench';
  if (review) review.hidden = mode !== 'review';
  if (pair) pair.hidden = mode !== 'pair';
  document.body.classList.toggle('pair-view-active', mode === 'pair');
  syncViewTabs();
  syncViewToolbar();
  if (mode === 'workbench') {
    refreshWorkbenchView();
    if (!selectedMatchId) autoSelectInitialMatch();
  } else if (mode === 'pair') {
    refreshPairView();
    if (!showcaseRunning) {
      requestAnimationFrame(() => setTimeout(() => drawAllMatchLines(false), 80));
    }
  } else if (mode === 'review') {
    renderReviewTable();
    renderReuseReport();
  }
}

function renderShowcaseBanner({ compact = false } = {}) {
  const { oldN, newN, matchN, paired } = showcaseStats();
  const les = state?.lesson || {};
  const oldMeta = document.getElementById('showcase-old-meta');
  const newMeta = document.getElementById('showcase-new-meta');
  const blockMeta = document.getElementById('showcase-block-meta');
  const matchMeta = document.getElementById('showcase-match-meta');
  if (compact) {
    if (oldMeta) oldMeta.textContent = oldN ? String(oldN) : '—';
    if (newMeta) newMeta.textContent = newN ? String(newN) : '—';
    if (blockMeta) blockMeta.textContent = '—';
    if (matchMeta) matchMeta.textContent = matchN ? `${paired}/${matchN}` : '—';
  } else {
    if (oldMeta) oldMeta.textContent = `${oldN} 个旧块${les.old_lesson_uid ? '' : '（无旧课）'}`;
    if (newMeta) newMeta.textContent = `${newN} 个新块`;
    if (blockMeta) blockMeta.textContent = `旧 ${oldN} · 新 ${newN}`;
    if (matchMeta) {
      matchMeta.textContent = matchN
        ? `已拉取 ${paired} 组${paired < matchN ? ` · 待补 ${matchN - paired}` : ''}`
        : '暂无配对记录';
    }
  }
  updateShowcaseProgressBar();
}

function getShowcasePanel() {
  return document.getElementById('pair-panel');
}

function waitShowcase(ms, token) {
  return new Promise((resolve) => {
    setTimeout(() => resolve(token === showcaseAnimToken), ms);
  });
}

function setShowcaseStep(index) {
  const steps = document.querySelectorAll('.showcase-step');
  const arrows = document.querySelectorAll('.showcase-step-arrow');
  steps.forEach((s, j) => {
    s.classList.toggle('is-active', j === index);
    s.classList.toggle('is-done', j < index);
  });
  arrows.forEach((a, j) => {
    a.classList.toggle('is-flowing', j < index);
    a.classList.toggle('is-done', j < index);
  });
}

function renderSimScreen(html) {
  const el = document.getElementById('showcase-sim-screen');
  if (el) el.innerHTML = html;
}

function demoFirstNewBlock() {
  return (state?.new_blocks || [])[0] || { block_id: 'N01', block_name: '新区块', new_tb_pgs: [1] };
}

function demoMatchCandidates(firstNew) {
  const oldBlocks = state?.old_blocks || [];
  const existing = (state?.matches || []).find((m) => m.new_block_id === firstNew?.block_id && m.old_block_id);
  if (existing) {
    const ob = oldBlocks.find((b) => b.block_id === existing.old_block_id);
    const rest = oldBlocks.filter((b) => b.block_id !== existing.old_block_id).slice(0, 2);
    const rows = [{ old: existing.old_block_id, name: ob?.block_name || '', score: 92 }];
    rest.forEach((b, i) => rows.push({ old: b.block_id, name: b.block_name || '', score: 48 - i * 8 }));
    return rows;
  }
  return oldBlocks.slice(0, 4).map((b, i) => ({
    old: b.block_id,
    name: b.block_name || '',
    score: Math.max(35, 88 - i * 14),
  }));
}

function simHtmlPullOld(oldN, progressMs = 2800) {
  return `<div class="sim-screen-inner sim-pull-old">
    <div class="sim-pulse-ring"></div>
    <p class="sim-kicker">STEP 01 · OLD LIBRARY</p>
    <h2 class="sim-hero-title">旧库拉取</h2>
    <p class="sim-hero-sub">从旧课件 annotate 加载本课区块</p>
    <div class="sim-big-counter"><span class="sim-counter-val" id="sim-counter-old">0</span><span class="sim-counter-unit">/ ${oldN} 块</span></div>
    <div class="sim-progress-track-lg"><div class="sim-progress-fill-lg sim-fill-old" style="--sim-progress-ms:${Math.round(progressMs)}ms"></div></div>
    <div class="sim-terminal"><div class="sim-term-line">▸ handshake old_library … OK</div><div class="sim-term-line sim-term-delay">▸ streaming blocks →</div></div>
  </div>`;
}

function simHtmlPullNew(newN, progressMs = 2800) {
  return `<div class="sim-screen-inner sim-pull-new">
    <div class="sim-pulse-ring sim-pulse-blue"></div>
    <p class="sim-kicker">STEP 02 · NEW LIBRARY</p>
    <h2 class="sim-hero-title">新库拉取</h2>
    <p class="sim-hero-sub">从新教材 annotate 加载本课区块</p>
    <div class="sim-big-counter"><span class="sim-counter-val" id="sim-counter-new">0</span><span class="sim-counter-unit">/ ${newN} 块</span></div>
    <div class="sim-progress-track-lg"><div class="sim-progress-fill-lg sim-fill-new" style="--sim-progress-ms:${Math.round(progressMs)}ms"></div></div>
    <div class="sim-terminal"><div class="sim-term-line">▸ handshake new_library … OK</div><div class="sim-term-line sim-term-delay">▸ streaming blocks →</div></div>
  </div>`;
}

function simHtmlScanNew(block) {
  const id = block?.block_id || 'N01';
  const name = (block?.block_name || block?.block_id || '').slice(0, 36);
  const pg = (block?.new_tb_pgs || [])[0] || 1;
  return `<div class="sim-screen-inner sim-scan-new">
    <p class="sim-kicker">STEP 03 · BLOCK SCAN</p>
    <h2 class="sim-hero-title">新区块扫描</h2>
    <div class="sim-target-card">
      <span class="sim-target-badge">TARGET</span>
      <span class="sim-target-id">${escHtml(id)}</span>
      <span class="sim-target-name">${escHtml(name)}</span>
    </div>
    <div class="sim-scanner-frame">
      <div class="sim-scan-grid"></div>
      <div class="sim-scan-beam"></div>
    </div>
    <ul class="sim-result-lines">
      <li class="sim-result-line">教材页 P${pg} · OCR 层已读取</li>
      <li class="sim-result-line">区块摘要 · ${escHtml(name.slice(0, 24))}${name.length > 24 ? '…' : ''}</li>
      <li class="sim-result-line">特征向量 · 768-dim · ready</li>
      <li class="sim-result-line sim-result-hit">扫描完成 · 可进入旧库检索</li>
    </ul>
  </div>`;
}

function simHtmlMatchOld(firstNew, candidates, { compact = false } = {}) {
  const nid = firstNew?.block_id || 'N01';
  const rows = compact ? candidates.slice(0, 3) : candidates;
  const top = rows[0];
  const innerClass = compact ? 'sim-screen-inner sim-match-old sim-match-compact' : 'sim-screen-inner sim-match-old';
  return `<div class="${innerClass}">
    <p class="sim-kicker">STEP 04 · FULL OLD SCAN</p>
    <h2 class="sim-hero-title">旧库全库匹配</h2>
    <p class="sim-hero-sub">以 <strong>${escHtml(nid)}</strong> 为探针，扫描 ${(state?.old_blocks || []).length} 个旧块</p>
    <div class="sim-radar-wrap">
      <div class="sim-radar"><div class="sim-radar-sweep"></div><div class="sim-radar-dot"></div></div>
    </div>
    <ul class="sim-match-list">
      ${rows.map((c, i) => `<li class="sim-match-item${i === 0 ? ' is-best' : ''}" style="animation-delay:${0.35 + i * 0.45}s">
        <span class="sim-match-id">${escHtml(c.old)}</span>
        <span class="sim-match-name">${escHtml((c.name || '').slice(0, 18))}</span>
        <span class="sim-match-score">${c.score}%</span>
      </li>`).join('')}
    </ul>
    ${!compact && top ? `<p class="sim-match-verdict">最佳候选 <strong>${escHtml(top.old)}</strong> ↔ ${escHtml(nid)}</p>` : ''}
  </div>`;
}

function simHtmlDone() {
  return `<div class="sim-screen-inner sim-done">
    <div class="sim-done-ring"></div>
    <h2 class="sim-hero-title">匹配完成</h2>
    <p class="sim-hero-sub sim-hero-dim sim-outro-hint">即将进入逐块审核</p>
  </div>`;
}

function stopMatchingVortex() {
  const vortex = document.getElementById('showcase-vortex');
  if (!vortex) return;
  vortex.classList.remove('is-active');
  vortex.innerHTML = '';
}

function highlightSideCard(side, blockId) {
  document.querySelectorAll('.block-card.sim-spotlight').forEach((c) => c.classList.remove('sim-spotlight'));
  if (!blockId) return;
  const sel = side === 'old' ? `[data-old-id="${blockId}"]` : `[data-new-id="${blockId}"]`;
  const card = document.querySelector(sel);
  if (card) {
    card.classList.add('sim-spotlight');
    scrollBlockIntoView(card);
  }
}

function animateSimCounter(elId, target, duration = 1200) {
  const el = document.getElementById(elId);
  if (!el || target <= 0) return Promise.resolve();
  if (prefersReducedMotion()) {
    el.textContent = String(target);
    return Promise.resolve();
  }
  const start = performance.now();
  return new Promise((resolve) => {
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      el.textContent = String(Math.round(target * (1 - (1 - t) ** 3)));
      if (t < 1) requestAnimationFrame(tick);
      else resolve();
    };
    requestAnimationFrame(tick);
  });
}

function prepareShowcaseLayout() {
  const layout = document.querySelector('.pair-showcase-layout');
  if (!layout) return;
  layout.classList.add('showcase-cards-hidden');
  layout.classList.remove('old-cards-in', 'new-cards-in', 'showcase-cards-done');
  getShowcasePanel()?.classList.remove('showcase-old-lit', 'showcase-new-lit', 'showcase-complete');
  updateShowcaseCompletePrompt(false);
  layout.querySelectorAll('.block-card').forEach((c) => {
    c.classList.remove('showcase-card-enter', 'showcase-card-visible');
    c.style.animationDelay = '';
  });
}

function revealShowcaseCards(side, staggerMs = 140) {
  const layout = document.querySelector('.pair-showcase-layout');
  if (!layout) return;
  const colId = side === 'old' ? 'old-blocks-container' : 'new-blocks-container';
  const col = document.getElementById(colId);
  const panel = getShowcasePanel();
  if (!col) return;
  layout.classList.add(side === 'old' ? 'old-cards-in' : 'new-cards-in');
  panel?.classList.add(side === 'old' ? 'showcase-old-lit' : 'showcase-new-lit');
  col.classList.add('showcase-col-reveal');
  const cards = col.querySelectorAll('.block-card');
  const stagger = prefersReducedMotion() ? 0 : staggerMs;
  cards.forEach((card, i) => {
    card.style.animationDelay = `${i * stagger}ms`;
    card.classList.add('showcase-card-enter');
  });
}

function finishShowcaseLayout() {
  const layout = document.querySelector('.pair-showcase-layout');
  if (!layout) return;
  layout.classList.remove('showcase-cards-hidden', 'old-cards-in', 'new-cards-in');
  layout.classList.add('showcase-cards-done');
  layout.querySelectorAll('.showcase-col-reveal').forEach((c) => c.classList.remove('showcase-col-reveal'));
}

function resetShowcaseSteps() {
  document.querySelectorAll('.showcase-step').forEach((el) => {
    el.classList.remove('is-active', 'is-done', 'showcase-step-enter');
  });
  document.querySelectorAll('.showcase-step-arrow').forEach((el) => {
    el.classList.remove('is-flowing', 'is-done');
  });
  getShowcasePanel()?.classList.remove('showcase-playing', 'showcase-complete');
  document.getElementById('pair-hub-glow')?.classList.remove('is-live');
  stopMatchingVortex();
}

function forceRevealAllShowcaseCards() {
  revealShowcaseCards('old', 0);
  revealShowcaseCards('new', 0);
  finishShowcaseLayout();
  getShowcasePanel()?.classList.add('showcase-old-lit', 'showcase-new-lit');
  stopMatchingVortex();
}

function updateShowcaseCompletePrompt(visible) {
  const prompt = document.getElementById('showcase-complete-prompt');
  const skip = document.getElementById('btn-showcase-skip');
  if (prompt) prompt.hidden = !visible;
  if (skip) skip.hidden = !!visible;
}

function hideShowcaseOverlayElements() {
  ['showcase-sim-screen', 'showcase-pair-arena', 'showcase-doubao-stage'].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.hidden = true;
      el.setAttribute('hidden', '');
      if (id === 'showcase-sim-screen') el.innerHTML = '';
    }
  });
  window.ShowcasePairArena?.reset?.();
  const hybridWrap = document.getElementById('hybrid-narrative-wrap');
  if (hybridWrap) hybridWrap.hidden = true;
  const hybridNar = document.getElementById('hybrid-narrative');
  if (hybridNar) hybridNar.innerHTML = '';
}

async function completeShowcaseOverview(token) {
  if (token !== undefined && token !== showcaseAnimToken) {
    showcaseRunning = false;
    return;
  }
  const panel = getShowcasePanel();
  hideShowcaseOverlayElements();

  await bootstrapCompareMatches();
  selectedMatchId = null;

  const { oldN, newN, matchN, paired } = showcaseStats();
  setShowcaseStep(4);
  forceRevealAllShowcaseCards();

  panel?.classList.remove(
    'showcase-playing', 'showcase-outro', 'showcase-mode-v4',
    'showcase-mode-hybrid', 'showcase-mode-doubao', 'is-speed-fast',
  );
  panel?.classList.add('showcase-complete', 'showcase-old-lit', 'showcase-new-lit');
  showcaseRunning = false;

  const oldMeta = document.getElementById('showcase-old-meta');
  const newMeta = document.getElementById('showcase-new-meta');
  const blockMeta = document.getElementById('showcase-block-meta');
  const matchMeta = document.getElementById('showcase-match-meta');
  if (oldMeta) oldMeta.textContent = oldN ? `${oldN} 个旧块` : '—';
  if (newMeta) newMeta.textContent = newN ? `${newN} 个新块` : '—';
  if (blockMeta) blockMeta.textContent = '扫描完成';
  if (matchMeta) matchMeta.textContent = matchN ? `${paired || matchN} 组` : '✓';

  setShowcaseDemoProgress(100, '配对展示完成');
  const tag = taglineEl();
  if (tag) {
    tag.hidden = false;
    tag.setAttribute('aria-hidden', 'false');
    tag.textContent = '配对已完成 · 浏览左右连线，准备好后进入逐块审核';
  }

  updateShowcaseCompletePrompt(true);
  updateGoWorkbenchHighlight();
  refreshPairView();
  requestAnimationFrame(() => drawAllMatchLines(true));
}

async function finishShowcaseDemo(token, { autoAudit = false, variant = 'classic', directJump = false, outroMs = 1600 } = {}) {
  if (token !== showcaseAnimToken) {
    showcaseRunning = false;
    return;
  }

  if (!autoAudit) {
    await completeShowcaseOverview(token);
    return;
  }

  const panel = getShowcasePanel();
  const { matchN, paired } = showcaseStats();

  if (directJump || variant === 'v4') {
    setShowcaseStep(4);
    renderSimScreen(simHtmlDone());
    if (taglineEl()) taglineEl().textContent = '匹配完成';
    setShowcaseDemoProgress(100, '新旧匹配已完成');
    const matchMeta = document.getElementById('showcase-match-meta');
    if (matchMeta) matchMeta.textContent = matchN ? String(matchN) : '✓';
    panel?.classList.add('showcase-outro');
    document.getElementById('showcase-pair-arena')?.setAttribute('hidden', '');
    showcaseRunning = false;
    if (!(await waitShowcase(outroMs, token))) return;
    if (viewMode === 'pair') {
      await bootstrapCompareMatches();
      selectedMatchId = null;
      panel?.classList.remove(
        'showcase-playing', 'showcase-outro', 'showcase-mode-v4',
        'showcase-mode-hybrid', 'showcase-mode-doubao', 'is-speed-fast',
      );
      setViewMode('workbench');
      autoSelectInitialMatch();
      updateShowcaseProgressBar();
    }
    return;
  }

  if (variant !== 'doubao' && variant !== 'hybrid') {
    setShowcaseStep(4);
    renderSimScreen(simHtmlDone());
  } else if (variant === 'hybrid') {
    const el = document.getElementById('hybrid-narrative');
    if (el) el.innerHTML = simHtmlDone();
  }
  if (taglineEl()) {
    taglineEl().textContent = variant === 'doubao'
      ? '演示完成 · 即将进入逐块审核'
      : '演示完成 · 即将进入逐块审核';
  }
  const oldMeta = document.getElementById('showcase-old-meta');
  const newMeta = document.getElementById('showcase-new-meta');
  const blockMeta = document.getElementById('showcase-block-meta');
  const matchMeta = document.getElementById('showcase-match-meta');
  if (oldMeta) oldMeta.textContent = `${oldN} 个旧块`;
  if (newMeta) newMeta.textContent = `${newN} 个新块`;
  if (blockMeta) blockMeta.textContent = '扫描完成';
  if (matchMeta) {
    matchMeta.textContent = matchN ? `候选 ${Math.min(paired || 1, matchN)} 组` : '已模拟';
  }
  forceRevealAllShowcaseCards();
  panel?.classList.remove('showcase-playing');
  panel?.classList.add('showcase-complete');
  updateShowcaseProgressBar();
  updateGoWorkbenchHighlight();
  showcaseRunning = false;
  if (!(await waitShowcase(outroMs, token))) return;
  if (viewMode === 'pair') {
    toast('演示完成，进入逐块审核');
    setViewMode('workbench');
    autoSelectInitialMatch();
  }
}

function taglineEl() {
  return document.getElementById('showcase-tagline');
}

async function runShowcaseAnimation() {
  const variant = typeof window.getShowcaseVariant === 'function' ? window.getShowcaseVariant() : 'v4';
  if (variant === 'v4' && typeof window.runShowcaseAnimationV4 === 'function') {
    return window.runShowcaseAnimationV4();
  }
  if (variant === 'doubao' && typeof window.runShowcaseAnimationDoubao === 'function') {
    return window.runShowcaseAnimationDoubao();
  }
  if (variant === 'hybrid' && typeof window.runShowcaseAnimationHybrid === 'function') {
    return window.runShowcaseAnimationHybrid();
  }
  if (viewMode !== 'pair') return;
  if (showcaseRunning) showcaseAnimToken += 1;
  showcaseRunning = true;
  const token = ++showcaseAnimToken;
  resetShowcaseSteps();
  prepareShowcaseLayout();
  const panel = getShowcasePanel();
  panel?.classList.add('showcase-playing');
  panel?.classList.remove('showcase-complete', 'showcase-old-lit', 'showcase-new-lit');
  stopMatchingVortex();
  renderShowcaseBanner();

  const reduced = prefersReducedMotion();
  const { oldN, newN } = showcaseStats();
  const firstNew = demoFirstNewBlock();
  const candidates = demoMatchCandidates(firstNew);
  const pullStagger = reduced ? 0 : 120;
  const oldPullHold = reduced ? 400 : calcPullHold(oldN, pullStagger);
  const newPullHold = reduced ? 400 : calcPullHold(newN, pullStagger);

  const fallbackMs = reduced ? 3000 : 18000;
  setTimeout(() => {
    if (token === showcaseAnimToken && showcaseRunning) {
      finishShowcaseDemo(token, { autoAudit: false });
    }
  }, fallbackMs);

  if (reduced) {
    revealShowcaseCards('old', 0);
    revealShowcaseCards('new', 0);
    await finishShowcaseDemo(token, { autoAudit: false });
    return;
  }

  // ① 旧库拉取
  setShowcaseStep(0);
  if (taglineEl()) taglineEl().textContent = '演示 · 正在从旧库拉取区块（左侧飞入）';
  renderSimScreen(simHtmlPullOld(oldN, oldPullHold));
  setShowcaseDemoProgress(5, '演示 · 旧库拉取');
  revealShowcaseCards('old', pullStagger);
  animateSimCounter('sim-counter-old', oldN, oldPullHold - 200);
  if (!(await waitShowcase(oldPullHold, token))) return;

  // ② 新库拉取
  setShowcaseStep(1);
  if (taglineEl()) taglineEl().textContent = '演示 · 正在从新库拉取区块（右侧飞入）';
  renderSimScreen(simHtmlPullNew(newN, newPullHold));
  setShowcaseDemoProgress(32, '演示 · 新库拉取');
  revealShowcaseCards('new', pullStagger);
  animateSimCounter('sim-counter-new', newN, newPullHold - 200);
  if (!(await waitShowcase(newPullHold, token))) return;

  // ③ 扫描第一个新区块
  setShowcaseStep(2);
  if (taglineEl()) taglineEl().textContent = `演示 · 扫描新区块 ${firstNew.block_id}`;
  renderSimScreen(simHtmlScanNew(firstNew));
  highlightSideCard('new', firstNew.block_id);
  const blockMeta = document.getElementById('showcase-block-meta');
  if (blockMeta) blockMeta.textContent = firstNew.block_id;
  if (!(await waitShowcase(reduced ? 800 : 2800, token))) return;

  // ④ 旧库全库匹配
  setShowcaseStep(3);
  if (taglineEl()) taglineEl().textContent = '演示 · 旧库全库扫描匹配中…';
  renderSimScreen(simHtmlMatchOld(firstNew, candidates));
  highlightSideCard('old', candidates[0]?.old);
  const matchMeta = document.getElementById('showcase-match-meta');
  if (matchMeta) matchMeta.textContent = `检索 ${(state?.old_blocks || []).length} 块`;
  if (!(await waitShowcase(3200, token))) return;

  await finishShowcaseDemo(token, { autoAudit: false });
}

function orphanBlockSortKey(block, side) {
  if (side === 'old' && (block.cw_pgs || []).length) {
    return Math.min(...block.cw_pgs.map(Number));
  }
  const pgs = side === 'old' ? block.old_tb_pgs : block.new_tb_pgs;
  if (pgs?.length) return 800 + Math.min(...pgs.map(Number));
  return 9999;
}

function buildQueueItems() {
  const items = [];
  const matchedOld = new Set();
  const matchedNew = new Set();
  sortedMatches().forEach((m) => {
    if (m.old_block_id) matchedOld.add(m.old_block_id);
    if (m.new_block_id) matchedNew.add(m.new_block_id);
    items.push({ kind: 'match', match: m, sortKey: matchSortKey(m) });
  });
  (state?.old_blocks || []).forEach((b) => {
    if (!matchedOld.has(b.block_id)) {
      items.push({ kind: 'old_only', block: b, sortKey: orphanBlockSortKey(b, 'old') });
    }
  });
  (state?.new_blocks || []).forEach((b) => {
    if (!matchedNew.has(b.block_id)) {
      items.push({ kind: 'new_only', block: b, sortKey: orphanBlockSortKey(b, 'new') });
    }
  });
  items.sort((a, b) => a.sortKey - b.sortKey || String(a.match?.match_id || a.block?.block_id).localeCompare(String(b.match?.match_id || b.block?.block_id)));
  return items;
}

function filteredQueueItems() {
  const pg = queuePageFilter ? Number(queuePageFilter) : null;
  if (!pg || Number.isNaN(pg)) return buildQueueItems();
  return buildQueueItems().filter((item) => {
    if (item.kind === 'match') {
      const nb = (state?.new_blocks || []).find((b) => b.block_id === item.match.new_block_id);
      return (nb?.new_tb_pgs || []).some((x) => Number(x) === pg);
    }
    if (item.kind === 'new_only') {
      return (item.block.new_tb_pgs || []).some((x) => Number(x) === pg);
    }
    return false;
  });
}

function renderAuditPageFilter() {
  const sel = document.getElementById('audit-page-filter');
  if (!sel) return;
  const levels = state?.page_levels || [];
  const cur = queuePageFilter || sel.value || '';
  const opts = ['<option value="">全部页</option>'];
  levels.forEach((row) => {
    const pg = row.page_index;
    const picked = String(pg) === String(cur) ? ' selected' : '';
    const lvl = row.level_label || '';
    opts.push(`<option value="${pg}"${picked}>P${pg}${lvl ? ` · ${escHtml(lvl)}` : ''}</option>`);
  });
  sel.innerHTML = opts.join('');
  sel.value = cur;
}

function renderAuditQueueLegend() {
  const el = document.getElementById('audit-queue-legend');
  if (!el) return;
  el.innerHTML = ACTION_LEGEND.map(
    (a) => `<span title="${escHtml(a.label)}"><i class="legend-${a.code}"></i>${escHtml(a.label)}</span>`,
  ).join('');
}

function queueItemPassesPageFilter(item) {
  return true;
}

function renderAuditQueueItem(item, qaBlocks, qaMatchIds) {
  let sel = '';
  if (item.kind === 'match' && item.match.match_id === selectedMatchId) sel = ' selected';
  else if (item.kind !== 'match' && selectedOrphan?.blockId === item.block.block_id) {
    const sideOk = (item.kind === 'old_only' && selectedOrphan.side === 'old')
      || (item.kind === 'new_only' && selectedOrphan.side === 'new');
    if (sideOk) sel = ' selected';
  }
  if (item.kind === 'match') {
    const m = item.match;
    const ob = (state?.old_blocks || []).find((b) => b.block_id === m.old_block_id);
    const nb = (state?.new_blocks || []).find((b) => b.block_id === m.new_block_id);
    const action = effectiveActionForMatch(m);
    const oldLabel = ob ? ob.block_id : '(无旧)';
    const newLabel = nb ? nb.block_id : '(无新)';
    const name = nb?.block_name || ob?.block_name || '';
    const pgs = (nb?.new_tb_pgs || ob?.old_tb_pgs || []).slice(0, 2).map((p) => `P${p}`).join(' ');
    const qa = qaBlocks.has(m.new_block_id || '') || qaBlocks.has(m.old_block_id || '') || qaMatchIds.has(m.match_id);
    const done = m.reuse_action && String(m.reuse_action).trim();
    return `<button type="button" class="audit-queue-item ${actionClass(action)}${sel}${qa ? ' qa-flagged' : ''}${done ? ' judged' : ''}"
      data-match-id="${escHtml(m.match_id)}" title="${escHtml(name || actionLabel(action))}">
      <span class="aq-action-bar"></span>
      <span class="aq-ids">${escHtml(oldLabel)} → ${escHtml(newLabel)}</span>
      <span class="aq-name">${escHtml(name)}</span>
      <span class="aq-meta">${escHtml(pgs)}${qa ? ' · !质检' : ''}${done ? '' : ' · 待判定'}</span>
    </button>`;
  }
  const b = item.block;
  const side = item.kind === 'old_only' ? 'old' : 'new';
  const qa = qaBlocks.has(b.block_id);
  return `<button type="button" class="audit-queue-item action-pending orphan-${side}${qa ? ' qa-flagged' : ''}"
    data-block-side="${side}" data-block-id="${escHtml(b.block_id)}" title="尚未建立匹配">
    <span class="aq-action-bar"></span>
    <span class="aq-ids">${side === 'old' ? escHtml(b.block_id) : escHtml(b.block_id)} · 未配对</span>
    <span class="aq-name">${escHtml(b.block_name || b.block_id)}</span>
    <span class="aq-meta">${side === 'old' ? '仅旧' : '仅新'}</span>
  </button>`;
}

function renderAuditQueue() {
  const list = document.getElementById('audit-queue-list');
  const countEl = document.getElementById('audit-queue-count');
  if (!list) return;
  renderAuditPageFilter();
  renderAuditQueueLegend();
  const { blockIds: qaBlocks, matchIds: qaMatchIds } = qaTargetSets();
  const items = filteredQueueItems().filter(queueItemPassesPageFilter);
  if (countEl) countEl.textContent = String(items.length);
  if (!items.length) {
    list.innerHTML = '<div class="pair-detail-empty">暂无区块。请先建块或添加配对。</div>';
    return;
  }
  list.innerHTML = items.map((item) => renderAuditQueueItem(item, qaBlocks, qaMatchIds)).join('');
  list.querySelectorAll('.audit-queue-item[data-match-id]').forEach((btn) => {
    btn.addEventListener('click', () => selectMatch(btn.dataset.matchId));
  });
  list.querySelectorAll('.audit-queue-item[data-block-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      if (btn.dataset.blockSide === 'old') selectOldBlock(btn.dataset.blockId);
      else selectNewBlock(btn.dataset.blockId);
    });
  });
  updateQueueNavButtons();
}

function renderAuditLessonBar() {
  const el = document.getElementById('audit-lesson-summary');
  if (!el) return;
  const grade = state?.lesson_grade;
  const qa = (state?.qa_flags || []).length;
  const total = (state?.matches || []).length;
  const judged = (state?.matches || []).filter((m) => (m.reuse_action || '').trim()).length;
  const pending = total - judged;
  const parts = [];
  if (grade?.label) parts.push(`课时定级：${grade.label}`);
  parts.push(`区块 ${judged}/${total} 已判定`);
  if (pending > 0) parts.push(`待判定 ${pending}`);
  if (qa) parts.push(`质检 ${qa} 项`);
  el.textContent = parts.join(' · ') || '完成配对后可查看课时定级';
}

function renderLessonDetailDialog() {
  renderWbLessonGradeCard();
  renderWbQaPanel();
  renderWbActionStats();
  const body = document.getElementById('lesson-detail-body');
  if (!body) return;
  const grade = document.getElementById('wb-lesson-grade-card')?.innerHTML || '';
  const qa = document.getElementById('wb-qa-panel');
  const qaHtml = qa && !qa.hidden ? qa.outerHTML : '';
  const stats = document.getElementById('wb-action-stats')?.innerHTML || '';
  body.innerHTML = `<div class="wb-lesson-grade-card">${grade}</div>${qaHtml}<div class="wb-action-stats">${stats}</div>`;
  body.querySelector('.wb-grade-feedback-btn')?.addEventListener('click', () => {
    openFeedbackDialog({ kind: 'lesson' });
  });
}

function openLessonDetailDialog() {
  renderLessonDetailDialog();
  document.getElementById('lesson-detail-dialog')?.showModal();
}

function syncJudgePanelUi() {
  const shell = document.getElementById('audit-shell');
  const btn = document.getElementById('btn-toggle-judge');
  if (shell) shell.classList.toggle('audit-judge-collapsed', judgePanelCollapsed);
  if (btn) {
    btn.textContent = judgePanelCollapsed ? '显示判定栏 ▶' : '隐藏判定栏 ◀';
    btn.title = judgePanelCollapsed ? '展开右侧判定面板' : '收起右侧判定面板，扩大对照区域';
  }
}

function findCurrentQueueIndex(items) {
  if (selectedMatchId) {
    return items.findIndex((i) => i.kind === 'match' && i.match.match_id === selectedMatchId);
  }
  if (selectedOrphan) {
    return items.findIndex((i) => (
      i.kind !== 'match'
      && i.block.block_id === selectedOrphan.blockId
      && ((i.kind === 'old_only' && selectedOrphan.side === 'old')
        || (i.kind === 'new_only' && selectedOrphan.side === 'new'))
    ));
  }
  return -1;
}

function selectQueueItem(item) {
  if (item.kind === 'match') selectMatch(item.match.match_id);
  else if (item.kind === 'old_only') selectOldBlock(item.block.block_id);
  else selectNewBlock(item.block.block_id);
}

function navigateQueue(delta) {
  const items = filteredQueueItems();
  if (!items.length) {
    toast('队列为空，请先建块或添加配对');
    return;
  }
  let idx = findCurrentQueueIndex(items);
  if (idx < 0) idx = delta > 0 ? -1 : 0;
  const nextIdx = idx + delta;
  if (nextIdx < 0) {
    toast('已是第一项');
    updateQueueNavButtons();
    return;
  }
  if (nextIdx >= items.length) {
    toast('已是最后一项');
    updateQueueNavButtons();
    return;
  }
  selectQueueItem(items[nextIdx]);
}

function refreshWorkbenchView() {
  renderAuditQueue();
  renderAuditLessonBar();
  syncJudgePanelUi();
  if (selectedMatchId) {
    const m = (state?.matches || []).find((x) => x.match_id === selectedMatchId);
    if (m) renderMatchDetail(m);
  }
  updateToolbarState();
}

function renderLessonGradeBanner() {
  const banner = document.getElementById('lesson-grade-banner');
  if (banner) banner.hidden = true;
}

function qaTargetSets() {
  const blockIds = new Set();
  const matchIds = new Set();
  (state?.qa_flags || []).forEach((f) => {
    if (f.target) blockIds.add(f.target);
    if (f.match_id) matchIds.add(f.match_id);
  });
  return { blockIds, matchIds };
}

function renderWbQaPanel() {
  const el = document.getElementById('wb-qa-panel');
  if (!el) return;
  const flags = state?.qa_flags || [];
  if (!flags.length) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  const rows = flags.map((f, i) => {
    const typeLabel = QA_TYPE_LABEL[f.type] || f.type || '质检';
    const sev = QA_SEVERITY_LABEL[f.severity] || f.severity || '';
    const target = f.target || '—';
    return `<li class="wb-qa-item wb-qa-sev-${escHtml(f.severity || 'medium')}" data-match-id="${escHtml(f.match_id || '')}" data-target="${escHtml(target)}">
      <span class="wb-qa-type">${escHtml(typeLabel)}</span>
      ${sev ? `<span class="wb-qa-sev">${escHtml(sev)}</span>` : ''}
      <span class="wb-qa-target">${escHtml(target)}</span>
      <div class="wb-qa-msg">${escHtml(f.message || '')}</div>
    </li>`;
  }).join('');
  el.innerHTML = `
    <div class="wb-qa-head"><strong>质检 ${flags.length} 项</strong><span class="atom-text-muted">点击定位区块</span></div>
    <ul class="wb-qa-list">${rows}</ul>`;
  el.querySelectorAll('.wb-qa-item').forEach((item) => {
    item.addEventListener('click', () => {
      const mid = item.dataset.matchId;
      const target = item.dataset.target;
      if (mid) {
        selectMatch(mid);
        return;
      }
      const m = (state?.matches || []).find(
        (x) => x.new_block_id === target || x.old_block_id === target,
      );
      if (m) selectMatch(m.match_id);
      else if (target) previewSingleBlock('new', target);
    });
  });
}

function renderWbLessonGradeCard() {
  const el = document.getElementById('wb-lesson-grade-card');
  const grade = state?.lesson_grade;
  const pre = state?.pre_stats;
  const qa = state?.qa_flags || [];
  if (!el) return;
  if (!grade?.label) {
    el.innerHTML = '<div class="pair-detail-empty">完成区块配对后可推断课时定级</div>';
    return;
  }
  const confLabel = { high: '高', medium: '中', low: '低' };
  const gradeConf = grade.confidence ? (confLabel[grade.confidence] || grade.confidence) : '';
  const pct = grade.modification_estimate_pct != null ? `${grade.modification_estimate_pct}%` : '—';
  const contPct = grade.continuous_reusable_ratio != null
    ? `${Math.round(grade.continuous_reusable_ratio * 100)}%`
    : '—';
  const violations = (grade.constraint_violations || []).slice(0, 3).map(
    (v) => `<li>${escHtml(v.message || v.action_label || '')}</li>`,
  ).join('');
  const preLines = (pre?.summary_lines || []).map(
    (line) => `<li>${escHtml(line)}</li>`,
  ).join('');
  const qaHint = qa.length
    ? `<div class="wb-qa-hint">质检 ${qa.length} 项 · 见「课时详情」</div>`
    : '';
  el.innerHTML = `
    <div class="wb-grade-title">${escHtml(grade.label)}${gradeConf ? `<span class="wb-grade-conf"> · 置信度 ${escHtml(gradeConf)}</span>` : ''}</div>
    <div class="wb-grade-rationale">${escHtml(grade.rationale || '')}</div>
    <div class="wb-grade-meta">
      修改量预估 <strong>${escHtml(pct)}</strong>
      · 可复用区块 <strong>${Math.round((grade.reusable_block_ratio || 0) * 100)}%</strong>
      · 连续可复用 <strong>${escHtml(contPct)}</strong>
      ${grade.pending_count ? ` · 待判定 ${grade.pending_count}` : ''}
    </div>
    ${preLines ? `<ul class="wb-pre-stats-list">${preLines}</ul>` : ''}
    ${violations ? `<ul class="wb-grade-violations">${violations}</ul>` : ''}
    ${qaHint}
    <button type="button" class="btn btn-sm btn-outline wb-grade-feedback-btn" style="margin-top:8px">定级不准？反馈</button>`;
  el.querySelector('.wb-grade-feedback-btn')?.addEventListener('click', () => {
    openFeedbackDialog({ kind: 'lesson' });
  });
}

function renderWbActionStats() {
  const el = document.getElementById('wb-action-stats');
  const stats = state?.action_stats;
  if (!el) return;
  if (!stats?.breakdown?.length) {
    el.innerHTML = '<div class="pair-detail-empty">暂无区块动作统计</div>';
    return;
  }
  const barColors = {
    reuse_as_is: '#86efac',
    optimize: '#fde047',
    reference: '#fdba74',
    new_build: '#fca5a5',
    remove: '#cbd5e1',
    pending: '#e2e8f0',
  };
  el.innerHTML = `<div class="wb-subhead" style="padding:0 0 8px;border:none">区块动作统计（${stats.total}）</div>`
    + stats.breakdown.map((row) => {
      const w = Math.max(4, Math.round((row.ratio || 0) * 100));
      const color = barColors[row.code] || '#94a3b8';
      return `<div class="wb-stat-row">
        <span class="wb-stat-label">${escHtml(row.label)}</span>
        <div class="wb-stat-bar"><span style="width:${w}%;background:${color}"></span></div>
        <span class="wb-stat-count">${row.count}</span>
      </div>`;
    }).join('');
}

function openFeedbackDialog(ctx) {
  feedbackContext = ctx;
  const dlg = document.getElementById('feedback-dialog');
  const title = document.getElementById('feedback-dialog-title');
  const verdictWrap = document.getElementById('feedback-verdict-wrap');
  const actionWrap = document.getElementById('feedback-action-wrap');
  const actionSel = document.getElementById('feedback-correct-action');
  const note = document.getElementById('feedback-note');
  if (!dlg) return;
  if (ctx.kind === 'lesson') {
    title.textContent = '课时定级反馈';
    verdictWrap.firstChild.textContent = '定级判断';
    document.getElementById('feedback-verdict').innerHTML = `
      <option value="too_high">定级偏高</option>
      <option value="too_low">定级偏低</option>
      <option value="correct">定级准确</option>`;
    actionWrap.hidden = false;
    actionSel.innerHTML = (state?.lesson_grade_options || []).map(
      (g) => `<option value="${escHtml(g.code)}">${escHtml(g.label)}</option>`,
    ).join('');
  } else {
    title.textContent = '区块判定反馈';
    verdictWrap.firstChild.textContent = '您的判断';
    document.getElementById('feedback-verdict').innerHTML = `
      <option value="wrong">AI 判错了</option>
      <option value="unsure">存疑</option>
      <option value="correct">AI 判对了</option>`;
    actionWrap.hidden = false;
    actionSel.innerHTML = optionList(state?.reuse_action_options, '', '— 正确动作 —');
  }
  if (note) note.value = '';
  dlg.showModal();
}

async function submitFeedback(ev) {
  ev.preventDefault();
  const dlg = document.getElementById('feedback-dialog');
  const verdict = document.getElementById('feedback-verdict')?.value || '';
  const correctAction = document.getElementById('feedback-correct-action')?.value || '';
  const note = (document.getElementById('feedback-note')?.value || '').trim();
  if (!feedbackContext) {
    dlg?.close();
    return;
  }
  if (feedbackContext.kind === 'block' && feedbackContext.matchId) {
    const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches/${encodeURIComponent(feedbackContext.matchId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        feedback: {
          kind: 'block',
          verdict,
          correct_action: correctAction || null,
          note,
        },
      }),
    });
    const data = await r.json();
    if (!r.ok || !data.ok) {
      toast(data.error || '反馈保存失败');
      return;
    }
    setState(data);
    refreshWorkbenchView();
    renderReviewTable();
    refreshPairView();
  } else {
    toast(`已记录课时定级反馈：${verdict}${note ? ` · ${note}` : ''}`);
  }
  dlg?.close();
  feedbackContext = null;
}

function initFeedbackUi() {
  document.getElementById('feedback-form')?.addEventListener('submit', submitFeedback);
  document.getElementById('feedback-cancel')?.addEventListener('click', () => {
    document.getElementById('feedback-dialog')?.close();
    feedbackContext = null;
  });
  document.getElementById('btn-feedback-global')?.addEventListener('click', () => {
    const note = prompt('功能建议、规则意见或 Bug（将暂存于本地）：');
    if (note == null) return;
    const key = `compare.feedback.${LESSON_UID}`;
    const prev = JSON.parse(localStorage.getItem(key) || '[]');
    prev.push({ at: new Date().toISOString(), note });
    localStorage.setItem(key, JSON.stringify(prev));
    toast('感谢反馈，已暂存');
  });
  document.getElementById('feedback-verdict')?.addEventListener('change', (ev) => {
    const wrap = document.getElementById('feedback-action-wrap');
    if (!wrap || feedbackContext?.kind === 'lesson') return;
    wrap.hidden = ev.target.value === 'correct';
  });
}

function updatePairCounts() {
  const oldEl = document.getElementById('old-count');
  const newEl = document.getElementById('new-count');
  if (oldEl) oldEl.textContent = String((state?.old_blocks || []).length);
  if (newEl) newEl.textContent = String((state?.new_blocks || []).length);
}

function renderBlockCard(b, side, extraCls) {
  const isOld = side === 'old';
  const ms = (state.matches || []).filter(
    (m) => isOld ? m.old_block_id === b.block_id : m.new_block_id === b.block_id,
  );
  const primaryMatch = ms[0];
  const effAction = primaryMatch ? effectiveActionForMatch(primaryMatch) : 'pending';
  let cls = ms.length ? 'block-card matched' : 'block-card';
  cls += ` ${actionClass(effAction)}`;
  if (extraCls) cls += ` ${extraCls}`;
  const pgKey = isOld ? 'old_tb_pgs' : 'new_tb_pgs';
  const pgs = (b[pgKey] || []).map((p) => `P${p}`).join('、') || '—';
  const cwStr = isOld ? ((b.cw_pgs || []).map((p) => `cw${p}`).join(' ') || '') : '';
  const matchHint = isOld
    ? (ms.map((m) => m.new_block_id || '无').join(', ') || '')
    : (ms.map((m) => m.old_block_id || '无').join(', ') || '');
  const excerpt = blockExcerpt(b, side);
  const anchorRefs = (!isOld && b.metadata && b.metadata.anchor_old_refs) ? b.metadata.anchor_old_refs : [];
  const anchorHint = anchorRefs.length
    ? `<div class="anchor-hint">↗ ${anchorRefs.map((r) => escHtml(`${r.old_block_code || r.block_id || ''}`)).join(' ')}</div>`
    : '';
  let typeTag = '';
  if (isOld && isOldCwHeavyBlock(b) && !ms.length) {
    typeTag = '<span class="block-type-tag cw">偏课件</span>';
  } else if (!isOld && isNewOnlyBlock(b) && !ms.length) {
    typeTag = '<span class="block-type-tag new-only">新增</span>';
  }
  const dataAttr = isOld ? `data-old-id="${b.block_id}"` : `data-new-id="${b.block_id}"`;
  return `
    <div class="${cls}" ${dataAttr}>
      <div class="block-id">${b.block_id}${matchHint ? ` → ${matchHint}` : ''}</div>
      <div class="block-name">${escHtml(b.block_name || b.block_id)}${typeTag}</div>
      <div class="block-meta">
        <span>${isOld ? '教材' : '新教材'} ${pgs}</span>
        ${cwStr ? `<span> ${cwStr}</span>` : ''}
      </div>
      ${excerpt ? `<div class="block-excerpt">${escHtml(excerpt)}</div>` : ''}
      ${anchorHint}
    </div>`;
}

function renderBlockSection(title, blocks, side, extraCls) {
  if (!blocks.length) return '';
  return `<div class="block-section-head">${title} (${blocks.length})</div>`
    + blocks.map((b) => renderBlockCard(b, side, extraCls)).join('');
}

function bindOldBlockCards(container) {
  container.querySelectorAll('[data-old-id]').forEach((card) => {
    const blockId = card.dataset.oldId;
    card.addEventListener('click', () => selectOldBlock(blockId));
  });
}

function bindNewBlockCards(container) {
  container.querySelectorAll('[data-new-id]').forEach((card) => {
    const blockId = card.dataset.newId;
    card.addEventListener('click', () => selectNewBlock(blockId));
  });
}

function renderOldBlocks() {
  const container = document.getElementById('old-blocks-container');
  if (!container) return;
  const oldBlocks = state?.old_blocks || [];
  if (!oldBlocks.length) {
    const oldUid = state?.lesson?.old_lesson_uid;
    container.innerHTML = oldUid
      ? `<div class="pair-detail-empty">暂无旧区块<br><a href="/old-library/lessons/${encodeURIComponent(oldUid)}/annotate">去建块</a></div>`
      : '<div class="pair-detail-empty">暂无旧区块</div>';
    return;
  }
  const matchedIds = new Set((state.matches || []).map((m) => m.old_block_id).filter(Boolean));
  const matched = oldBlocks.filter((b) => matchedIds.has(b.block_id));
  const unmatched = oldBlocks.filter((b) => !matchedIds.has(b.block_id));
  const unmatchedCw = unmatched.filter(isOldCwHeavyBlock);
  const unmatchedTb = unmatched.filter((b) => !isOldCwHeavyBlock(b));
  let html = '';
  html += renderBlockSection('已匹配', matched, 'old', '');
  html += renderBlockSection('未匹配 · 教材块', unmatchedTb, 'old', 'unmatched-text');
  html += renderBlockSection('未匹配 · 偏课件（新侧可无）', unmatchedCw, 'old', 'cw-heavy');
  container.innerHTML = html;
  bindOldBlockCards(container);
}

function renderNewBlocks() {
  const container = document.getElementById('new-blocks-container');
  if (!container) return;
  const newBlocks = state?.new_blocks || [];
  if (!newBlocks.length) {
    container.innerHTML = `<div class="pair-detail-empty">暂无新区块<br><a href="/new-library/lessons/${encodeURIComponent(LESSON_UID)}/annotate">去建块</a></div>`;
    return;
  }
  const matchedIds = new Set((state.matches || []).map((m) => m.new_block_id).filter(Boolean));
  const matched = newBlocks.filter((b) => matchedIds.has(b.block_id));
  const unmatched = newBlocks.filter((b) => !matchedIds.has(b.block_id));
  const unmatchedNewOnly = unmatched.filter(isNewOnlyBlock);
  const unmatchedPending = unmatched.filter((b) => !isNewOnlyBlock(b));
  let html = '';
  html += renderBlockSection('已匹配', matched, 'new', '');
  html += renderBlockSection('未匹配 · 待配旧块', unmatchedPending, 'new', 'unmatched-text');
  html += renderBlockSection('未匹配 · 新增（可不配）', unmatchedNewOnly, 'new', 'new-only');
  container.innerHTML = html;
  bindNewBlockCards(container);
}

function startDrag(e, oldBlockId) {
  const card = e.target.closest('.block-card');
  if (!card) return;
  const rect = card.getBoundingClientRect();
  dragState = {
    oldBlockId,
    startX: rect.right,
    startY: rect.top + rect.height / 2,
  };
  document.addEventListener('mousemove', onDragMove);
  document.addEventListener('mouseup', onDragEnd);
  e.preventDefault();
}

function onDragMove(e) {
  if (!dragState) return;
  drawDragLine(e.clientX, e.clientY);
}

function onDragEnd() {
  document.removeEventListener('mousemove', onDragMove);
  document.removeEventListener('mouseup', onDragEnd);
  clearDragLine();
  dragState = null;
}

function endDragOnNew(newBlockId) {
  if (!dragState) return;
  const oldBlockId = dragState.oldBlockId;
  dragState = null;
  clearDragLine();
  document.removeEventListener('mousemove', onDragMove);
  document.removeEventListener('mouseup', onDragEnd);
  createMatchViaDrag(oldBlockId, newBlockId);
}

function drawDragLine(x, y) {
  const svg = document.getElementById('match-svg');
  if (!svg || !dragState) return;
  svg.querySelectorAll('.drag-line').forEach((l) => l.remove());
  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('x1', dragState.startX);
  line.setAttribute('y1', dragState.startY);
  line.setAttribute('x2', x);
  line.setAttribute('y2', y);
  line.setAttribute('stroke', '#E74C3C');
  line.setAttribute('stroke-width', '2');
  line.setAttribute('stroke-dasharray', '6,4');
  line.classList.add('drag-line');
  svg.appendChild(line);
}

function clearDragLine() {
  const svg = document.getElementById('match-svg');
  if (!svg) return;
  svg.querySelectorAll('.drag-line').forEach((l) => l.remove());
}

function highlightMatchVisuals(m) {
  if (!m) return;
  document.querySelectorAll('.block-card').forEach((c) => {
    c.classList.remove('match-linked', 'match-pulse-old', 'match-pulse-new');
  });
  const oldCard = document.querySelector(`[data-old-id="${m.old_block_id}"]`);
  const newCard = document.querySelector(`[data-new-id="${m.new_block_id}"]`);
  if (oldCard) {
    oldCard.classList.add('selected', 'match-linked');
    if (viewMode === 'pair') {
      oldCard.classList.add('match-pulse-old');
      scrollBlockIntoView(oldCard);
    }
  }
  if (newCard) {
    newCard.classList.add('selected', 'match-linked');
    if (viewMode === 'pair') {
      newCard.classList.add('match-pulse-new');
      scrollBlockIntoView(newCard);
    }
  }
  const svg = document.getElementById('match-svg');
  svg?.querySelectorAll('.match-line').forEach((line) => {
    line.classList.remove('match-line-boost');
    if (line.dataset.matchId === m.match_id) {
      line.classList.add('match-line-boost');
      setTimeout(() => line.classList.remove('match-line-boost'), 2000);
    }
  });
  const queueBtn = document.querySelector(`.audit-queue-item[data-match-id="${m.match_id}"]`);
  if (queueBtn) {
    queueBtn.classList.remove('match-highlight');
    void queueBtn.offsetWidth;
    queueBtn.classList.add('match-highlight');
  }
  if (viewMode === 'pair') requestAnimationFrame(drawAllMatchLines);
}

function playMatchConnectAnimation(oldBlockId, newBlockId) {
  return new Promise((resolve) => {
    const oldCard = document.querySelector(`[data-old-id="${oldBlockId}"]`);
    const newCard = document.querySelector(`[data-new-id="${newBlockId}"]`);
    const svg = document.getElementById('match-svg');
    if (!oldCard || !newCard || !svg || viewMode !== 'pair') {
      resolve();
      return;
    }
    document.body.classList.add('pair-view-active');
    svg.style.display = 'block';
    const oldRect = oldCard.getBoundingClientRect();
    const newRect = newCard.getBoundingClientRect();
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    const x1 = oldRect.right;
    const y1 = oldRect.top + oldRect.height / 2;
    const x2 = newRect.left;
    const y2 = newRect.top + newRect.height / 2;
    const cx = (x1 + x2) / 2;
    path.setAttribute('d', `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`);
    path.classList.add('match-line', 'match-line-connecting');
    path.setAttribute('marker-end', 'url(#arrowhead-active)');
    svg.appendChild(path);
    oldCard.classList.add('match-pulse-old', 'match-linked');
    newCard.classList.add('match-pulse-new', 'match-linked');
    setTimeout(() => {
      path.remove();
      oldCard.classList.remove('match-pulse-old', 'match-pulse-new');
      newCard.classList.remove('match-pulse-old', 'match-pulse-new');
      resolve();
    }, 780);
  });
}

function drawAllMatchLines(stagger = false) {
  const svg = document.getElementById('match-svg');
  if (!svg || viewMode !== 'pair') return;
  svg.querySelectorAll('.match-line').forEach((l) => l.remove());
  const matches = (state.matches || []).filter((m) => m.old_block_id && m.new_block_id);
  matches.forEach((m, i) => {
    if (stagger) {
      setTimeout(() => drawMatchLine(m, true), i * 100);
    } else {
      drawMatchLine(m, false);
    }
  });
}

function drawMatchLine(m, entrance = false) {
  const oldCard = document.querySelector(`[data-old-id="${m.old_block_id}"]`);
  const newCard = document.querySelector(`[data-new-id="${m.new_block_id}"]`);
  if (!oldCard || !newCard || !m.old_block_id || !m.new_block_id) return;
  const svg = document.getElementById('match-svg');
  const oldRect = oldCard.getBoundingClientRect();
  const newRect = newCard.getBoundingClientRect();
  const x1 = oldRect.right;
  const y1 = oldRect.top + oldRect.height / 2;
  const x2 = newRect.left;
  const y2 = newRect.top + newRect.height / 2;
  const cx = (x1 + x2) / 2;
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', `M ${x1} ${y1} C ${cx} ${y1}, ${cx} ${y2}, ${x2} ${y2}`);
  path.setAttribute('marker-end', 'url(#arrowhead)');
  path.classList.add('match-line', matchLineStatusClass(m));
  if (entrance) path.classList.add('match-line-enter');
  if (m.match_id === selectedMatchId) {
    path.classList.add('match-line-active');
    path.setAttribute('marker-end', 'url(#arrowhead-active)');
  }
  path.dataset.matchId = m.match_id;
  path.addEventListener('click', () => selectMatch(m.match_id));
  svg.appendChild(path);
}

function refreshPairView() {
  updatePairCounts();
  renderShowcaseBanner();
  renderOldBlocks();
  renderNewBlocks();
  updateGoWorkbenchHighlight();
  if (selectedMatchId) {
    const m = (state.matches || []).find((x) => x.match_id === selectedMatchId);
    if (m) {
      const oldBlock = (state.old_blocks || []).find((b) => b.block_id === m.old_block_id);
      const newBlock = (state.new_blocks || []).find((b) => b.block_id === m.new_block_id);
      renderComparePreview(oldBlock, newBlock, 'pair-preview', 'pair-preview-hint');
      if (viewMode === 'workbench') renderMatchDetail(m);
    }
  }
  if (viewMode === 'pair') {
    requestAnimationFrame(() => drawAllMatchLines(false));
  }
}

function selectOldBlock(blockId) {
  const m = (state.matches || []).find((x) => x.old_block_id === blockId);
  if (m) selectMatch(m.match_id);
  else previewSingleBlock('old', blockId);
}

function selectNewBlock(blockId) {
  const m = (state.matches || []).find((x) => x.new_block_id === blockId);
  if (m) selectMatch(m.match_id);
  else previewSingleBlock('new', blockId);
}

function bindUnpairedDetailPanel(container, side, block, matchId = null) {
  container.querySelector('.unpaired-add-pair-btn')?.addEventListener('click', () => {
    if (isConfirmed()) {
      toast('对比已锁定，请先解锁');
      return;
    }
    if (matchId) {
      const m = (state?.matches || []).find((x) => x.match_id === matchId);
      openAddMatchDialog(m?.old_block_id || null, m?.new_block_id || null, matchId);
      return;
    }
    if (side === 'old') openAddMatchDialog(block.block_id, null);
    else openAddMatchDialog(null, block.block_id);
  });
  container.querySelector('.unpaired-del-match-btn')?.addEventListener('click', () => {
    if (matchId) deleteMatch(matchId);
  });
}

function renderUnpairedDetail(side, block, matchId = null) {
  const sideLabel = side === 'old' ? '旧课件' : '新教材';
  const otherLabel = side === 'old' ? '新教材' : '旧课件';
  const readonly = isConfirmed();
  const blockLabel = `${block.block_id} ${block.block_name || ''}`.trim();
  return `
    <div class="orphan-detail-panel">
      <div class="orphan-detail-head">
        <span class="orphan-badge">待补全配对</span>
        <strong>${escHtml(blockLabel)}</strong>
      </div>
      <p class="pair-detail-empty">已有${escHtml(sideLabel)}区块，但缺少${escHtml(otherLabel)}一侧。补全配对后才能填写复用判定。</p>
      ${readonly ? '' : `<button type="button" class="btn btn-sm btn-primary unpaired-add-pair-btn">补全配对…</button>`}
      ${(!readonly && matchId) ? `<button type="button" class="btn btn-sm btn-danger unpaired-del-match-btn">删除此不完整匹配</button>` : ''}
      <p class="atom-text-muted orphan-hint">可点顶栏「添加配对」或「锚定导入」补全；对照总览仅展示已有配对，不需手动连线。</p>
    </div>`;
}

function previewSingleBlock(side, blockId) {
  const block = (side === 'old' ? state.old_blocks : state.new_blocks || [])
    .find((b) => b.block_id === blockId);
  if (!block) return;
  selectedMatchId = null;
  selectedOrphan = { side, blockId };
  document.querySelectorAll('.block-card').forEach((c) => c.classList.remove('selected'));
  renderAuditQueue();
  if (side === 'old') {
    renderComparePreview(block, null);
    renderComparePreview(block, null, 'pair-preview', 'pair-preview-hint');
    renderComparePreview(block, null, 'wb-preview', 'wb-preview-hint');
  } else {
    renderComparePreview(null, block);
    renderComparePreview(null, block, 'pair-preview', 'pair-preview-hint');
    renderComparePreview(null, block, 'wb-preview', 'wb-preview-hint');
  }
  const html = renderUnpairedDetail(side, block);
  ['detail-content', 'wb-detail-content'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = html;
    bindUnpairedDetailPanel(el, side, block);
  });
  updateQueueNavButtons();
}

function renderMatchDetail(m, containerId) {
  const detailIds = containerId
    ? [containerId]
    : ['wb-detail-content', 'detail-content'];
  const oldBlock = (state.old_blocks || []).find((b) => b.block_id === m.old_block_id);
  const newBlock = (state.new_blocks || []).find((b) => b.block_id === m.new_block_id);
  if (!oldBlock && !newBlock) {
    detailIds.forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = '<div class="pair-detail-empty">匹配数据不完整</div>';
    });
    return;
  }
  if (!oldBlock || !newBlock) {
    const side = oldBlock ? 'old' : 'new';
    const block = oldBlock || newBlock;
    const html = renderUnpairedDetail(side, block, m.match_id);
    detailIds.forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.innerHTML = html;
      bindUnpairedDetailPanel(el, side, block, m.match_id);
    });
    return;
  }
  const oldLabel = oldBlock ? `${oldBlock.block_id} ${oldBlock.block_name}` : '(无旧块)';
  const newLabel = newBlock ? `${newBlock.block_id} ${newBlock.block_name}` : '(无新块)';
  const readonly = isConfirmed();
  const dis = readonly ? ' disabled' : '';
  const effAction = effectiveActionForMatch(m);
  const aiAction = (m.ai_reuse_action || '').trim();
  const matchTypes = ['1:1', 'N:1', '1:N', '1:0', '0:1'];
  const matchTypeOpts = matchTypes.map((t) => {
    const sel = m.match_type === t ? ' selected' : '';
    const label = t === 'N:1' ? 'N:1（多旧→一新）' : t === '1:0' ? '1:0（仅旧）' : t === '0:1' ? '0:1（仅新）' : t;
    return `<option value="${t}"${sel}>${label}</option>`;
  }).join('');
  const confLabel = { high: '高', medium: '中', low: '低' };
  const aiConf = m.ai_confidence ? (confLabel[m.ai_confidence] || m.ai_confidence) : '';
  const aiRatio = m.ai_text_change?.change_ratio != null
    ? `${Math.round(m.ai_text_change.change_ratio * 100)}%`
    : '';
  const aiSubtype = m.ai_optimize_subtype === 're_voice'
    ? '需重配音'
    : (m.ai_optimize_subtype === 'text_only' ? '仅改文字' : '');
  const aiRationale = (m.ai_rationale || []).length
    ? `<ul class="wb-ai-rationale">${(m.ai_rationale || []).map((r) => `<li>${escHtml(r)}</li>`).join('')}</ul>`
    : '';
  const aiChangePoints = (m.ai_change_points || []).length
    ? `<div class="wb-change-points"><strong>修改要点</strong><ul>${(m.ai_change_points || []).map((p) => `<li>${escHtml(p)}</li>`).join('')}</ul></div>`
    : '';
  const aiPreview = hasAiSuggestion(m)
    ? `<div class="wb-ai-preview">
        <div class="ai-label">AI 预判${aiConf ? ` · 置信度 ${escHtml(aiConf)}` : ''}${aiRatio ? ` · 文本改动 ${escHtml(aiRatio)}` : ''}${aiSubtype ? ` · ${escHtml(aiSubtype)}` : ''}</div>
        <div>${escHtml(actionLabel(aiAction || effAction))}
          ${m.ai_change_type ? ` · ${escHtml(m.ai_change_type_label || m.ai_change_type)}` : ''}
          ${m.ai_match_score != null ? ` · 匹配度 ${Math.round(m.ai_match_score * 100)}%` : ''}</div>
        ${aiRationale}
        ${aiChangePoints}
        ${m.ai_teacher_note ? `<div class="atom-text-muted">${escHtml(m.ai_teacher_note)}</div>` : ''}
        ${aiSourceBadge(m)}
        ${!readonly ? `<button type="button" class="btn btn-sm btn-outline apply-ai-detail-btn" data-id="${m.match_id}" style="margin-top:6px">采纳 AI</button>` : ''}
      </div>`
    : '';
  const html = `
    <div class="match-pair">
      <div class="block-mini old">${escHtml(oldLabel)}</div>
      <div class="arrow">→</div>
      <div class="block-mini new">${escHtml(newLabel)}</div>
    </div>
    <div class="detail-section">
      <label>当前动作 <span class="${actionClass(effAction)}" style="padding:1px 6px;border-radius:4px;font-size:11px">${escHtml(actionLabel(effAction))}</span></label>
    </div>
    ${aiPreview}
    <div class="detail-section">
      <label>匹配类型</label>
      <select class="detail-match-type" data-id="${m.match_id}"${dis}>${matchTypeOpts}</select>
    </div>
    <div class="detail-section">
      <label>复用判定（教研）</label>
      <select class="detail-reuse" data-id="${m.match_id}"${dis}>${optionList(state.reuse_action_options, m.reuse_action, '— 请选择 —')}</select>
    </div>
    <div class="detail-section">
      <label>变化类型</label>
      <select class="detail-change" data-id="${m.match_id}"${dis}>${optionList(state.change_type_options, m.change_type, '—', true)}</select>
    </div>
    <div class="detail-section">
      <label>修改要点 / 教研说明</label>
      <textarea class="detail-note" data-id="${m.match_id}" placeholder="写清：旧课件哪页→新教材哪块→怎么改…"${dis}>${escHtml(m.teacher_note || '')}</textarea>
    </div>
    ${readonly ? '' : `<button type="button" class="btn btn-sm btn-danger detail-del-btn del-match-detail" data-id="${m.match_id}">删除此匹配</button>`}
    <div class="detail-feedback-row">
      <button type="button" class="btn btn-sm btn-outline block-feedback-btn" data-id="${m.match_id}" title="记录 AI 判定有误，便于后续改进">判错了？反馈</button>
      ${m.feedback?.note ? `<div class="atom-text-muted">已反馈：${escHtml(m.feedback.note)}</div>` : ''}
    </div>`;

  detailIds.forEach((id) => {
    const detail = document.getElementById(id);
    if (!detail) return;
    detail.innerHTML = html;
    detail.querySelector('.detail-match-type')?.addEventListener('change', (ev) => {
      updateMatchField(ev.target.dataset.id, 'match_type', ev.target.value);
    });
    detail.querySelector('.detail-reuse')?.addEventListener('change', (ev) => {
      updateMatchField(ev.target.dataset.id, 'reuse_action', ev.target.value);
    });
    detail.querySelector('.detail-change')?.addEventListener('change', (ev) => {
      updateMatchField(ev.target.dataset.id, 'change_type', ev.target.value);
    });
    detail.querySelector('.detail-note')?.addEventListener('blur', (ev) => {
      updateMatchField(ev.target.dataset.id, 'teacher_note', ev.target.value);
    });
    detail.querySelector('.del-match-detail')?.addEventListener('click', () => {
      deleteMatch(m.match_id);
    });
    detail.querySelector('.apply-ai-detail-btn')?.addEventListener('click', () => {
      applyAiSuggestion(m.match_id, false);
    });
    detail.querySelector('.block-feedback-btn')?.addEventListener('click', () => {
      openFeedbackDialog({ kind: 'block', matchId: m.match_id });
    });
  });
}

async function createMatchViaDrag(oldBlockId, newBlockId) {
  if (isConfirmed()) {
    toast('对比已锁定');
    return;
  }
  if ((state.matches || []).some((m) => m.old_block_id === oldBlockId)) {
    toast('该旧区块已有配对，请先删除或修改');
    return;
  }
  const hasNew = (state.matches || []).some((m) => m.new_block_id === newBlockId);
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/matches`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      old_block_id: oldBlockId,
      new_block_id: newBlockId,
      match_type: hasNew ? 'N:1' : '1:1',
    }),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '创建配对失败');
    return;
  }
  setState(data);
  const created = (data.matches || []).find(
    (x) => x.old_block_id === oldBlockId && x.new_block_id === newBlockId,
  ) || (data.matches || []).slice(-1)[0];
  refreshPairView();
  renderReviewTable();
  await playMatchConnectAnimation(oldBlockId, newBlockId);
  toast(`已连线 ${oldBlockId} → ${newBlockId}`);
  if (created?.match_id) selectMatch(created.match_id);
}

function initViewTabs() {
  document.getElementById('btn-view-workbench')?.addEventListener('click', () => setViewMode('workbench'));
  document.getElementById('btn-view-review')?.addEventListener('click', () => setViewMode('review'));
  document.getElementById('btn-view-pair')?.addEventListener('click', () => setViewMode('pair'));
  document.getElementById('btn-go-workbench')?.addEventListener('click', () => {
    const variant = typeof window.getShowcaseVariant === 'function' ? window.getShowcaseVariant() : 'hybrid';
    if (variant === 'doubao' || variant === 'hybrid' || variant === 'v4') {
      window.CompareShowcase?.skipToWorkbench();
      return;
    }
    showcaseAnimToken += 1;
    showcaseRunning = false;
    setViewMode('workbench');
    autoSelectInitialMatch();
  });
  document.getElementById('btn-go-review')?.addEventListener('click', () => setViewMode('review'));
  document.getElementById('btn-showcase-replay')?.addEventListener('click', () => {
    prepareShowcaseLayout();
    renderSimScreen('');
    document.getElementById('hybrid-narrative') && (document.getElementById('hybrid-narrative').innerHTML = '');
    document.getElementById('showcase-doubao-stage')?.classList.remove('is-finale', 'is-flash');
    updateShowcaseCompletePrompt(false);
    runShowcaseAnimation();
  });
  document.getElementById('btn-showcase-enter-workbench')?.addEventListener('click', () => {
    updateShowcaseCompletePrompt(false);
    setViewMode('workbench');
    autoSelectInitialMatch();
  });
  document.getElementById('btn-showcase-stay-overview')?.addEventListener('click', () => {
    toast('可点击左右区块或连线查看详情；准备好后点「进入逐块审核」');
  });
  syncViewTabs();
  syncViewToolbar();
}

function initAuditUi() {
  document.getElementById('audit-page-filter')?.addEventListener('change', (ev) => {
    queuePageFilter = ev.target.value || '';
    renderAuditQueue();
  });
  document.getElementById('btn-toggle-judge')?.addEventListener('click', () => {
    judgePanelCollapsed = !judgePanelCollapsed;
    syncJudgePanelUi();
    toast(judgePanelCollapsed ? '已隐藏判定栏' : '已显示判定栏');
  });
  document.getElementById('btn-prev-block')?.addEventListener('click', () => navigateQueue(-1));
  document.getElementById('btn-next-block')?.addEventListener('click', () => navigateQueue(1));
  document.getElementById('btn-lesson-detail')?.addEventListener('click', openLessonDetailDialog);
  document.getElementById('lesson-detail-close')?.addEventListener('click', () => {
    document.getElementById('lesson-detail-dialog')?.close();
  });
  document.addEventListener('keydown', (ev) => {
    if (viewMode !== 'workbench') return;
    const tag = (ev.target?.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (ev.key === 'ArrowDown' || ev.key === 'j') {
      ev.preventDefault();
      navigateQueue(1);
    } else if (ev.key === 'ArrowUp' || ev.key === 'k') {
      ev.preventDefault();
      navigateQueue(-1);
    }
  });
}

function initPairScrollRedraw() {
  const redraw = () => {
    if (viewMode === 'pair') requestAnimationFrame(drawAllMatchLines);
  };
  document.getElementById('old-blocks-container')?.addEventListener('scroll', redraw, { passive: true });
  document.getElementById('new-blocks-container')?.addEventListener('scroll', redraw, { passive: true });
  window.addEventListener('resize', () => {
    if (viewMode === 'pair') setTimeout(drawAllMatchLines, 100);
  });
}

async function doConfirm() {
  if (!confirm('确认后对比结果将锁定，是否继续？')) return;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '确认失败');
    return;
  }
  setState(data);
  toast(data.reuse_report?.summary ? `已确认 · ${data.reuse_report.summary}` : '已教研确认');
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
  renderReuseReport();
}

async function doUnlock() {
  if (!confirm('解锁后可继续编辑对比结果？')) return;
  const r = await fetch(`${API}/lessons/${encodeURIComponent(LESSON_UID)}/compare/unlock`, {
    method: 'POST',
  });
  const data = await r.json();
  if (!r.ok || !data.ok) {
    toast(data.error || '解锁失败');
    return;
  }
  setState(data);
  toast('已解锁');
  renderReviewTable();
  refreshPairView();
  refreshWorkbenchView();
}

function initSplitResizer() {
  const split = document.getElementById('compare-split');
  const handle = document.getElementById('compare-split-handle');
  const left = document.getElementById('review-table-wrap');
  if (!split || !handle || !left) return;

  const storageKey = 'compare.splitTablePct';
  const saved = Number(localStorage.getItem(storageKey));
  if (!Number.isNaN(saved) && saved >= 32 && saved <= 82) {
    left.style.flexBasis = `${saved}%`;
  }

  const setWidth = (clientX) => {
    const rect = split.getBoundingClientRect();
    const pct = ((clientX - rect.left) / rect.width) * 100;
    const clamped = Math.min(82, Math.max(32, pct));
    left.style.flexBasis = `${clamped}%`;
    localStorage.setItem(storageKey, String(Math.round(clamped)));
  };

  let dragging = false;
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    dragging = true;
    document.body.classList.add('compare-split-dragging');
  });
  handle.addEventListener('dblclick', () => {
    left.style.flexBasis = '62%';
    localStorage.setItem(storageKey, '62');
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    setWidth(e.clientX);
  });
  window.addEventListener('mouseup', () => {
    dragging = false;
    document.body.classList.remove('compare-split-dragging');
  });
}

document.getElementById('btn-auto-suggest')?.addEventListener('click', async () => {
  const hasAny = (state?.matches || []).some((m) => hasAiSuggestion(m));
  if (hasAny) {
    if (confirm('部分行已有 AI 建议，是否覆盖重新生成？')) {
      await runAutoSuggest(true);
    } else {
      await runAutoSuggest(false);
    }
    return;
  }
  await runAutoSuggest(false);
});
document.getElementById('btn-apply-all-ai')?.addEventListener('click', applyAllAiSuggestions);
document.getElementById('btn-refresh-report')?.addEventListener('click', refreshReuseReport);
document.getElementById('btn-export-json')?.addEventListener('click', downloadExportJson);
document.getElementById('btn-export-xlsx')?.addEventListener('click', downloadExportXlsx);
document.getElementById('btn-export-pptx')?.addEventListener('click', downloadExportPptx);
document.getElementById('btn-confirm')?.addEventListener('click', doConfirm);
document.getElementById('btn-unlock')?.addEventListener('click', doUnlock);
document.getElementById('btn-apply-anchor')?.addEventListener('click', applyAnchors);
document.getElementById('btn-add-match')?.addEventListener('click', () => {
  if (isConfirmed()) {
    toast('对比已锁定，请先解锁');
    return;
  }
  openAddMatchDialog();
});
document.getElementById('add-match-form')?.addEventListener('submit', submitAddMatch);
document.getElementById('add-match-cancel')?.addEventListener('click', () => {
  document.getElementById('add-match-dialog').close();
});

window.selectMatch = selectMatch;

window.CompareShowcase = {
  getState: () => state,
  getViewMode: () => viewMode,
  getToken: () => showcaseAnimToken,
  bumpToken: () => {
    showcaseAnimToken += 1;
    return showcaseAnimToken;
  },
  startShowcase: () => {
    if (showcaseRunning) showcaseAnimToken += 1;
    showcaseRunning = true;
    showcaseAnimToken += 1;
    return showcaseAnimToken;
  },
  isRunning: () => showcaseRunning,
  setRunning: (v) => {
    showcaseRunning = v;
  },
  finish: finishShowcaseDemo,
  completeOverview: completeShowcaseOverview,
  setViewMode,
  autoSelectInitialMatch,
  toast,
  prefersReducedMotion,
  showcaseStats,
  resetShowcaseSteps,
  prepareShowcaseLayout,
  renderShowcaseBanner,
  renderShowcaseBannerCompact: () => renderShowcaseBanner({ compact: true }),
  renderSim: renderSimScreen,
  renderHybrid: (html) => {
    const el = document.getElementById('hybrid-narrative');
    if (el) el.innerHTML = html;
  },
  simHtmlPullOld,
  simHtmlPullNew,
  simHtmlScanNew,
  simHtmlMatchOld,
  simHtmlDone,
  calcPullHold,
  setShowcaseDemoProgress,
  revealShowcaseCards,
  highlightSideCard,
  animateSimCounter,
  setShowcaseStep,
  setCaption: (text) => {
    const cap = document.getElementById('db-stage-caption');
    if (cap) cap.textContent = text;
    const tag = document.getElementById('showcase-tagline');
    if (tag && text) tag.textContent = text;
  },
  demoFirstNewBlock,
  demoMatchCandidates,
  forceRevealAllShowcaseCards,
  skipToOverviewComplete: () => {
    showcaseAnimToken += 1;
    showcaseRunning = false;
    completeShowcaseOverview(showcaseAnimToken);
    toast('已跳过动画，展示完整配对连线');
  },
  skipToWorkbench: () => {
    window.CompareShowcase?.skipToOverviewComplete?.();
  },
};

initSplitResizer();
initViewTabs();
initAuditUi();
initPairScrollRedraw();
initFeedbackUi();
loadData().catch((e) => {
  document.getElementById('review-table-body').innerHTML = `<tr><td colspan="7" class="empty-cell">${escHtml(e.message)}</td></tr>`;
});
