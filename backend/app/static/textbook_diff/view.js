(function () {
  const $ = (s) => document.querySelector(s);
  const P = window.DIFF_VIEW_PARAMS || {};
  if (P.preview_blob_id && P.new_pdf_source !== 'draft') {
    P.new_pdf_source = 'draft';
  }
  let state = null;
  let showAtomOverlay = true;
  /** 小科整课叠加层：默认环节框（文字/图片层入口已隐藏） */
  let xiaokeOverlayLayer = 'phase';
  /** 小科环节面板内切换的对比视图：phase / text / image */
  let xiaokePhaseView = 'phase';
  /** 右侧选中的整课对比块：只画对应原子框 */
  let selectedLessonPair = null;
  /** 右侧选中的教学环节：{ side, phaseId } */

  /** 在容器内把 $...$ 和 $$...$$ 渲染为 KaTeX 公式（跳过 <mark> 等标签内部） */
  function renderMath(container) {
    if (!container || typeof window.katex === 'undefined') return;
    const INLINE = /\$([^\$\n]{1,500})\$/g;
    container.querySelectorAll('.diff-block-text, .diff-atom-catalog-text, .old-t, .new-t').forEach((el) => {
      if (el.dataset.mathRendered) return;
      // 遍历文本节点，替换 $...$ 为 KaTeX
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
      const nodes = [];
      let node;
      while ((node = walker.nextNode())) nodes.push(node);
      let changed = false;
      nodes.forEach((tn) => {
        const txt = tn.nodeValue;
        if (!txt || txt.indexOf('$') < 0) return;
        const frag = document.createDocumentFragment();
        let last = 0;
        let m;
        INLINE.lastIndex = 0;
        let found = false;
        while ((m = INLINE.exec(txt)) !== null) {
          if (m[0].trim().length <= 2) continue;
          found = true;
          if (m.index > last) frag.appendChild(document.createTextNode(txt.slice(last, m.index)));
          const span = document.createElement('span');
          try {
            window.katex.render(m[1], span, { throwOnError: false, displayMode: false });
          } catch (e) {
            span.textContent = m[0];
          }
          frag.appendChild(span);
          last = m.index + m[0].length;
        }
        if (found) {
          if (last < txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
          tn.parentNode.replaceChild(frag, tn);
          changed = true;
        }
      });
      if (changed) el.dataset.mathRendered = '1';
    });
  }
  let selectedLessonPhase = null;
  let ocrBusyKey = null;
  let activeAtomKey = null;

  function atomKey(side, atomId) {
    return `${side}:${atomId || ''}`;
  }

  function atomTypeLabel(t) {
    if (t === 'image') return '插图';
    if (t === 'title') return '标题';
    return '文字';
  }

  function focusAtom(side, atomId, { scrollList = true } = {}) {
    if (!side || !atomId) return;
    activeAtomKey = atomKey(side, atomId);
    paintOverlays();
    syncCatalogActiveStates();
    if (scrollList) scrollCatalogAtomIntoView(side, atomId);
  }

  function scrollCatalogAtomIntoView(side, atomId) {
    if (textCompareState || imageCompareState) {
      const wrap = activeResultTab === 'image' && imageCompareState
        ? $('#image-block-pairs')
        : $('#text-block-pairs');
      const row = wrap?.querySelector(
        `tr[data-old-id="${CSS.escape(atomId)}"], tr[data-new-id="${CSS.escape(atomId)}"]`
      );
      row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      return;
    }
    const listId = side === 'old' ? '#atom-old-list' : '#atom-new-list';
    requestAnimationFrame(() => {
      const list = $(listId);
      const row = list?.querySelector(
        `.diff-atom-catalog-item[data-side="${side}"][data-atom-id="${atomId}"]`
      );
      row?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      $('#atom-catalog-wrap')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
  }

  function syncCatalogActiveStates() {
    document.querySelectorAll('.diff-atom-catalog-item').forEach((row) => {
      row.classList.toggle('is-active', row.dataset.atomKey === activeAtomKey);
    });
  }

  function renderAtomCatalogColumn(side, atoms) {
    if (textCompareState || imageCompareState) return;
    const listId = side === 'old' ? '#atom-old-list' : '#atom-new-list';
    const el = $(listId);
    if (!el) return;
    if (!atoms?.length) {
      el.innerHTML = '<p class="diff-atom-catalog-empty">暂无原子</p>';
      return;
    }
    const ordered = [...atoms].sort((a, b) => {
      const ya = ((Number(a.y_start) || 0) + (Number(a.y_end) || 0)) / 2;
      const yb = ((Number(b.y_start) || 0) + (Number(b.y_end) || 0)) / 2;
      if (ya !== yb) return ya - yb;
      return (Number(a.x_start) || 0) - (Number(b.x_start) || 0);
    });
    el.innerHTML = ordered.map((a) => {
      const key = atomKey(side, a.atom_id);
      const text = (a.display_text || a.ocr_text || a.atom_type || '').trim();
      const typeCls = a.atom_type === 'image' ? ' is-image' : '';
      return (
        `<button type="button" class="diff-atom-catalog-item${activeAtomKey === key ? ' is-active' : ''}" `
        + `data-atom-key="${esc(key)}" data-side="${esc(side)}" data-atom-id="${esc(a.atom_id)}">`
        + `<span class="diff-atom-catalog-meta">`
        + `<span class="diff-atom-catalog-code">${esc(a.atom_id)}</span>`
        + `<span class="diff-atom-catalog-type${typeCls}">${esc(atomTypeLabel(a.atom_type))}</span>`
        + `</span>`
        + `<span class="diff-atom-catalog-text">${esc(text)}</span>`
        + `</button>`
      );
    }).join('');
    el.querySelectorAll('.diff-atom-catalog-item').forEach((row) => {
      row.addEventListener('click', () => {
        focusAtom(row.dataset.side, row.dataset.atomId, { scrollList: false });
      });
    });
    renderMath(el);
  }

  const OCR_BUTTONS = [
    { id: '#btn-sync-ocr-text', side: 'both', phase: 'text', idle: '① 文字 OCR' },
    { id: '#btn-sync-ocr-image', side: 'both', phase: 'images', idle: '③ 图片 OCR' },
    { id: '#btn-old-ocr-text', side: 'old', phase: 'text', idle: '① 文字' },
    { id: '#btn-old-ocr-image', side: 'old', phase: 'images', idle: '③ 图片' },
    { id: '#btn-new-ocr-text', side: 'new', phase: 'text', idle: '① 文字' },
    { id: '#btn-new-ocr-image', side: 'new', phase: 'images', idle: '③ 图片' },
  ];
  const TEXT_COMPARE_BTN = '#btn-text-compare';
  const IMAGE_COMPARE_BTN = '#btn-image-compare';
  const PAGE_ONE_CLICK_BTN = '#btn-page-one-click';
  const LESSON_PIPELINE_BTN = '#btn-lesson-pipeline';
  const XIAOK_OCR_LESSON_BTN = '#btn-xiaoke-ocr-new-lesson';
  let textCompareState = null;
  let imageCompareState = null;
  let lessonCompareState = null;
  /** 小科整课：{ old: { pdfPage: atoms[] }, new: {...} } */
  let lessonAtomsByPage = { old: {}, new: {} };
  let lessonPhasesByPage = { old: {}, new: {} };
  let activeResultTab = 'text';
  let changedAtomIds = { old: new Set(), new: new Set() };
  let imageChangedAtomIds = { old: new Set(), new: new Set() };

  function syncOverlayVisibility() {
    const on = showAtomOverlay;
    const stitch = isXiaokeLessonMode();
    document.querySelectorAll('.diff-atom-layer').forEach((layer) => {
      const card = layer.closest('.diff-view-page-card');
      const isActive = !card || card.classList.contains('is-active');
      // 整课拼图：每一页都画框，不藏非当前页
      layer.classList.toggle('is-hidden', !on || (!stitch && !isActive));
    });
    const toggle = $('#toggle-atom-overlay');
    if (toggle) toggle.checked = on;
  }

  function showToast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._tid);
    el._tid = setTimeout(() => { el.hidden = true; }, 4000);
  }

  function esc(s) {
    const el = document.createElement('span');
    el.textContent = s == null ? '' : String(s);
    return el.innerHTML;
  }

  function workspaceQuery(extra) {
    const q = new URLSearchParams({
      old_code: P.old_code,
      new_code: P.new_code,
      mode: P.mode || 'page',
      ...extra,
    });
    if (P.mode === 'lesson') {
      q.set('new_lesson_uid', P.new_lesson_uid);
      q.set('page_index', String(P.page_index || 1));
    } else {
      q.set('old_page', String(P.old_page));
      q.set('new_page', String(P.new_page));
      if (P.new_pdf_source) q.set('new_pdf_source', P.new_pdf_source);
      if (P.preview_blob_id) q.set('preview_blob_id', P.preview_blob_id);
    }
    return q;
  }

  function isDraftNew() {
    return P.new_pdf_source === 'draft' || !!P.preview_blob_id;
  }

  function resolveBackLinks() {
    const q = new URLSearchParams(location.search);
    const from = (q.get('from') || P.from || '').trim();
    const subject = (q.get('subject') || P.subject || '').trim();
    const code = (P.new_code || '').trim();
    const side = code.endsWith('-DOLD') ? 'old' : 'new';

    // 本册建设 / 修订版页对比 → 回本册工作页
    if (P.old_code && code && from !== 'intake' && (from === 'workbook' || isDraftNew())) {
      const p = new URLSearchParams({
        old_code: P.old_code,
        new_code: P.new_code,
      });
      if (subject) p.set('subject', subject);
      const wb = `/textbook-diff/workbook?${p}`;
      return {
        back_url: wb,
        back_label: '返回本册建设',
        hub_url: wb,
        hub_label: '返回本册建设',
      };
    }
    if (isDraftNew() && code) {
      const dq = new URLSearchParams({ code, kind: 'draft' });
      if (P.preview_blob_id) dq.set('preview_blob_id', P.preview_blob_id);
      const u = `/textbook-diff/${side}/intake/compare?${dq}`;
      return {
        back_url: u,
        back_label: '返回修订版对比',
        hub_url: u,
        hub_label: '返回修订版对比',
      };
    }
    if (code) {
      return {
        back_url: `/textbook-diff/${side}/intake?code=${encodeURIComponent(code)}`,
        back_label: '返回教材上传',
        hub_url: `/textbook-diff/${side}/intake?code=${encodeURIComponent(code)}`,
        hub_label: '返回教材上传',
      };
    }
    return {
      back_url: '/textbook-diff/',
      back_label: '教材对比首页',
      hub_url: '/textbook-diff/',
      hub_label: '教材对比首页',
    };
  }

  function isGenericBack(url) {
    return !url
      || url === '/textbook-diff/compare'
      || url.endsWith('/textbook-diff/compare')
      || String(url).includes('/intake/compare');
  }

  function applyBackLinks() {
    const fallback = resolveBackLinks();
    const fromWorkbook = (P.from === 'workbook'
      || new URLSearchParams(location.search).get('from') === 'workbook');
    const meta = fromWorkbook
      ? fallback
      : {
          back_url: isGenericBack(P.back_url) ? fallback.back_url : (P.back_url || fallback.back_url),
          back_label: (isGenericBack(P.back_url)
            || P.back_label === '返回对比列表'
            || P.back_label === '返回课时对比')
            ? fallback.back_label
            : (P.back_label || fallback.back_label),
        };
    const back = $('#back-link');
    if (back) {
      back.href = meta.back_url;
      back.textContent = `← ${meta.back_label}`;
    }
    // 只保留「首页 + 一级返回」，隐藏历史 hub 链
    const hub = $('#hub-link');
    if (hub) hub.hidden = true;
  }

  function pdfCacheToken({ oldLibrary = false, draft = false } = {}) {
    if (oldLibrary) return String(state?.old_pdf_cache || '').trim();
    if (draft && isDraftNew()) {
      return String(P.preview_blob_id || state?.preview_blob_id || '').replace(/-/g, '').slice(0, 12);
    }
    return String(state?.new_pdf_cache || '').trim();
  }

  function pageImageUrl(volumeCode, page, { draft = false, oldLibrary = false } = {}) {
    const q = new URLSearchParams();
    if (draft && isDraftNew()) {
      q.set('source', 'draft');
      if (P.preview_blob_id) q.set('preview_blob_id', P.preview_blob_id);
    }
    const token = pdfCacheToken({ oldLibrary, draft });
    if (token) q.set('v', token);
    const qs = q.toString();
    const suffix = qs ? `?${qs}` : '';
    if (oldLibrary) {
      return `/api/old-library/volumes/${encodeURIComponent(volumeCode)}/pdf-page/${page}.png${suffix}`;
    }
    return `/api/textbook-diff/volumes/${encodeURIComponent(volumeCode)}/pdf-page/${page}.png${suffix}`;
  }

  function oldPageImageUrl(page) {
    const api = state?.old_pdf_api || '';
    const code = state?.old_pdf_volume_code || P.old_code;
    if (api === 'old-library' && code) {
      return pageImageUrl(code, page, { oldLibrary: true });
    }
    return pageImageUrl(P.old_code, page);
  }

  function lessonPdfPages(pageIndex) {
    if (!state || state.mode !== 'lesson') return null;
    const newPs = Number(state.new_lesson?.page_start);
    const newPe = Number(state.new_lesson?.page_end || newPs);
    const oldPs = Number(state.old_lesson?.page_start);
    const oldPe = Number(state.old_lesson?.page_end || oldPs);
    if (!newPs || !oldPs) return null;
    const newCount = Math.max(1, newPe - newPs + 1);
    const oldCount = Math.max(
      1,
      Number(state.old_page_count) || (oldPe - oldPs + 1),
    );
    const pi = Math.max(1, Math.min(Number(pageIndex) || 1, newCount));
    const oldAvailable = pi <= oldCount;
    return {
      page_index: pi,
      page_count: newCount,
      old_page_count: oldCount,
      old_page_available: oldAvailable,
      old_page: oldAvailable ? oldPs + pi - 1 : null,
      new_page: newPs + pi - 1,
    };
  }

  function setPageImageLoading(side, on) {
    const wrap = activePageWrap(side);
    if (wrap) wrap.classList.toggle('is-loading', !!on);
  }

  function markPageImageSettled(img) {
    if (!img) return;
    const shot = img.closest('.diff-view-img-wrap');
    if (shot) shot.classList.remove('is-loading');
  }

  function activePageCard(side) {
    const listId = side === 'old' ? '#old-pages-list' : '#new-pages-list';
    return $(`${listId} .diff-view-page-card.is-active`);
  }

  function activePageWrap(side) {
    return activePageCard(side)?.querySelector('.diff-view-img-wrap') || null;
  }

  function activePageImg(side) {
    return activePageCard(side)?.querySelector('img.diff-view-page-img') || null;
  }

  function activeAtomLayer(side) {
    return activePageCard(side)?.querySelector('.diff-atom-layer') || null;
  }

  function pageUrlsForIndex(pageIndex) {
    const list = state?.page_image_urls;
    if (Array.isArray(list) && list.length) {
      const hit = list.find((x) => Number(x.page_index) === Number(pageIndex));
      if (hit) {
        const available = hit.old_page_available !== false && hit.old_page != null;
        return {
          page_index: Number(hit.page_index),
          page_count: Number(state.page_count || list.length || 1),
          old_page_count: Number(state.old_page_count || 0),
          old_page_available: available,
          old_page: available ? Number(hit.old_page) : null,
          new_page: Number(hit.new_page),
          old_url: available ? (hit.old_url || '') : '',
          new_url: hit.new_url || '',
        };
      }
    }
    const pages = lessonPdfPages(pageIndex);
    if (!pages) return null;
    return {
      ...pages,
      old_url: pages.old_page_available ? oldPageImageUrl(pages.old_page) : '',
      new_url: pageImageUrl(P.new_code, pages.new_page),
    };
  }

  function syncLenHint() {
    const lenHint = $('#old-page-len-hint');
    if (!lenHint) return;
    const oldCount = Number(state?.old_page_count || 0);
    const newCount = Number(state?.page_count || 0);
    if (isXiaokeLessonMode() && oldCount > 0 && newCount !== oldCount) {
      lenHint.hidden = false;
      lenHint.textContent = `整课整体：旧 ${oldCount} 页、新 ${newCount} 页（页数可不一致，按内容对齐）。`;
    } else if (state?.mode === 'lesson' && oldCount > 0 && newCount > oldCount) {
      lenHint.hidden = false;
      lenHint.textContent = `提示：旧课仅 ${oldCount} 页，新课 ${newCount} 页；选中第 ${oldCount + 1} 页起仅比对新侧。`;
    } else {
      lenHint.hidden = true;
      lenHint.textContent = '';
    }
  }

  function syncOldOcrButtons(available) {
    ['#btn-old-ocr-text', '#btn-old-ocr-image'].forEach((sel) => {
      const btn = $(sel);
      if (btn) btn.disabled = !available;
    });
  }

  function updatePageNavLabel() {
    const nav = $('#lesson-page-nav');
    const navLabel = $('#page-nav-label');
    const panelLabel = $('#panel-page-label');
    const prevBtn = $('#prev-page-btn');
    const nextBtn = $('#next-page-btn');
    if (!state) return;
    if (state.mode === 'lesson' && Number(state.page_count || 0) >= 1) {
      if (nav) nav.hidden = false;
      if (prevBtn) prevBtn.hidden = true;
      if (nextBtn) nextBtn.hidden = true;
      const oldCount = Number(state.old_page_count || 0);
      const newCount = Number(state.page_count || 0);
      if (isXiaokeLessonMode()) {
        const text = oldCount > 0
          ? `整课整体 · 旧 ${oldCount} 页 / 新 ${newCount} 页`
          : `整课整体 · 新 ${newCount} 页`;
        if (navLabel) navLabel.textContent = text;
        if (panelLabel) panelLabel.textContent = '· 教学环节';
      } else {
        const base = `共 ${newCount} 页 · 当前第 ${state.page_index} 页`;
        const text = (oldCount > 0 && newCount > oldCount)
          ? `${base}（旧课 ${oldCount} 页）`
          : base;
        if (navLabel) navLabel.textContent = text;
        if (panelLabel) {
          panelLabel.textContent = state.old_page
            ? `· 旧 p${state.old_page} ↔ 新 p${state.new_page}`
            : `· 新 p${state.new_page}（旧无对应）`;
        }
      }
    } else if (state.mode === 'page' && state.old_page && state.new_page) {
      if (nav) nav.hidden = false;
      if (prevBtn) prevBtn.hidden = false;
      if (nextBtn) nextBtn.hidden = false;
      if (navLabel) navLabel.textContent = `预览 p${state.new_page} ↔ 旧 p${state.old_page}`;
      if (prevBtn) prevBtn.disabled = Number(state.new_page) <= 1 || Number(state.old_page) <= 1;
      if (nextBtn) nextBtn.disabled = false;
      if (panelLabel) panelLabel.textContent = `· 旧 p${state.old_page} ↔ 新 p${state.new_page}`;
    } else if (nav) {
      nav.hidden = true;
    }
  }

  function buildPageCardHtml(side, pages, isActive) {
    const available = side === 'old'
      ? (pages.old_page_available !== false && pages.old_page != null)
      : !!pages.new_page;
    // 旧侧缺页：不渲染占位卡，仅新列继续展示
    if (side === 'old' && !available) return '';
    if (!available) return '';
    const pdfPage = side === 'old' ? pages.old_page : pages.new_page;
    const url = side === 'old' ? pages.old_url : pages.new_url;
    if (!url || !pdfPage) return '';
    return `<article class="diff-view-page-card${isActive ? ' is-active' : ''}"
        data-page-index="${pages.page_index}" data-side="${side}" role="button" tabindex="0">
      <div class="diff-view-img-wrap">
        <img class="diff-view-page-img" alt="${side === 'old' ? '旧' : '新'}教材 p${pdfPage}" src="${esc(url)}" />
        <div class="diff-atom-layer"></div>
      </div>
      <p class="diff-view-page-num">PDF p${esc(String(pdfPage))}</p>
    </article>`;
  }

  function pageEntriesForRender() {
    if (state?.mode === 'lesson') {
      const count = Number(state.page_count || 0);
      if (count < 1) return [];
      const rows = [];
      for (let i = 1; i <= count; i += 1) {
        const pages = pageUrlsForIndex(i);
        if (pages) rows.push(pages);
      }
      return rows;
    }
    // page 模式：单页一对
    const oldPage = Number(state?.old_page || P.old_page || 0);
    const newPage = Number(state?.new_page || P.new_page || 0);
    if (!oldPage || !newPage) return [];
    return [{
      page_index: 1,
      page_count: 1,
      old_page_count: 1,
      old_page_available: true,
      old_page: oldPage,
      new_page: newPage,
      old_url: state?.old_page_url || oldPageImageUrl(oldPage),
      new_url: state?.new_page_url || pageImageUrl(P.new_code, newPage, { draft: P.mode !== 'lesson' }),
    }];
  }

  function bindPageListClicks() {
    ['#old-pages-list', '#new-pages-list'].forEach((sel) => {
      const root = $(sel);
      if (!root || root.dataset.bound === '1') return;
      root.dataset.bound = '1';
      root.addEventListener('click', (ev) => {
        if (isXiaokeLessonMode()) return; // 整课拼图：不按页切换重载
        const card = ev.target.closest('.diff-view-page-card');
        if (!card || !root.contains(card)) return;
        const idx = Number(card.dataset.pageIndex || 0);
        if (idx < 1) return;
        selectLessonPage(idx).catch((e) => showToast(String(e)));
      });
      root.addEventListener('keydown', (ev) => {
        if (isXiaokeLessonMode()) return;
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        const card = ev.target.closest('.diff-view-page-card');
        if (!card || !root.contains(card)) return;
        ev.preventDefault();
        const idx = Number(card.dataset.pageIndex || 0);
        if (idx < 1) return;
        selectLessonPage(idx).catch((e) => showToast(String(e)));
      });
      root.addEventListener('error', (ev) => {
        const img = ev.target;
        if (!img || img.tagName !== 'IMG') return;
        const src = img.getAttribute('src') || '';
        const card = img.closest('.diff-view-page-card');
        const side = card?.dataset.side === 'old' ? 'old' : 'new';
        const page = Number(side === 'old'
          ? (card && pageUrlsForIndex(card.dataset.pageIndex)?.old_page)
          : (card && pageUrlsForIndex(card.dataset.pageIndex)?.new_page));
        const code = side === 'old'
          ? (state?.old_pdf_volume_code || P.old_code)
          : P.new_code;
        if (code && page && src.includes('/api/file-blobs/') && !img.dataset.fallbackTried) {
          img.dataset.fallbackTried = '1';
          const fallback = side === 'old'
            ? oldPageImageUrl(page)
            : pageImageUrl(code, page, { draft: P.mode !== 'lesson' });
          if (fallback && img.getAttribute('src') !== fallback) {
            img.src = fallback;
            return;
          }
        }
        markPageImageSettled(img);
      }, true);
    });
  }

  function renderPageLists() {
    const oldList = $('#old-pages-list');
    const newList = $('#new-pages-list');
    if (!oldList || !newList || !state) return;
    const rows = pageEntriesForRender();
    const stitch = isXiaokeLessonMode();
    const active = Number(state.page_index || 1);
    oldList.classList.toggle('is-lesson-stitch', stitch);
    newList.classList.toggle('is-lesson-stitch', stitch);
    oldList.innerHTML = rows
      .filter((p) => p.old_page_available !== false && p.old_page != null)
      .map((p) => buildPageCardHtml('old', p, stitch || Number(p.page_index) === active))
      .join('');
    newList.innerHTML = rows
      .map((p) => buildPageCardHtml('new', p, stitch || Number(p.page_index) === active))
      .join('');
    bindPageListClicks();
    const bindImg = (side) => {
      const list = side === 'old' ? oldList : newList;
      list.querySelectorAll('img.diff-view-page-img').forEach((img) => {
        const onReady = () => {
          markPageImageSettled(img);
          paintOverlays();
        };
        img.addEventListener('load', onReady, { once: true });
        if (img.complete) onReady();
      });
    };
    if (stitch) {
      bindImg('old');
      bindImg('new');
    } else {
      rows.forEach((p) => {
        if (Number(p.page_index) !== active) return;
        ['old', 'new'].forEach((side) => {
          const img = activePageImg(side);
          if (!img) return;
          const onReady = () => {
            markPageImageSettled(img);
            paintOverlays();
          };
          img.addEventListener('load', onReady, { once: true });
          if (img.complete) onReady();
        });
      });
    }
    const available = state.old_page_available !== false && state.old_page != null;
    syncOldOcrButtons(available);
    syncLenHint();
    updatePageNavLabel();
    if (!stitch) {
      requestAnimationFrame(() => {
        activePageCard('old')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        activePageCard('new')?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      });
    }
  }

  function applyPageImages() {
    // 兼容旧调用点：课对模式改为整列渲染
    if (state?.mode === 'lesson' || ($('#old-pages-list') && $('#new-pages-list'))) {
      renderPageLists();
      return;
    }
  }

  function prefetchAdjacentPageImages() {
    if (!state || state.mode !== 'lesson') return;
    const list = state.page_image_urls || [];
    list.forEach((hit) => {
      [hit.old_url, hit.new_url].forEach((url) => {
        if (!url) return;
        const im = new Image();
        im.src = url;
      });
    });
  }

  function syncUrlPageIndex(pageIndex) {
    try {
      const q = new URLSearchParams(location.search);
      if (P.mode === 'lesson') {
        q.set('page_index', String(pageIndex));
        history.replaceState(null, '', `${location.pathname}?${q.toString()}`);
      }
    } catch (_) { /* ignore */ }
  }

  async function selectLessonPage(pageIndex) {
    if (!state) return;
    if (state.mode === 'page') return;
    const pages = pageUrlsForIndex(pageIndex);
    if (!pages) return;
    if (Number(state.page_index) === Number(pages.page_index)) {
      highlightActiveCards(pages.page_index);
      return;
    }
    if (isXiaokeLessonMode()) {
      textCompareState = null;
      imageCompareState = null;
      changedAtomIds = { old: new Set(), new: new Set() };
      imageChangedAtomIds = { old: new Set(), new: new Set() };
    } else {
      clearCompareHighlight();
      textCompareState = null;
      imageCompareState = null;
    }
    P.page_index = String(pages.page_index);
    state = {
      ...state,
      page_index: pages.page_index,
      page_count: pages.page_count,
      old_page_count: pages.old_page_count || state.old_page_count,
      old_page_available: pages.old_page_available,
      old_page: pages.old_page,
      new_page: pages.new_page,
      old_page_url: pages.old_url,
      new_page_url: pages.new_url,
      title: `${formatLesson(state.new_lesson) || '课时'} · 第 ${pages.page_index} 页`,
      atoms: null,
    };
    syncUrlPageIndex(pages.page_index);
    if ($('#view-title')) $('#view-title').textContent = state.title;
    highlightActiveCards(pages.page_index);
    updatePageNavLabel();
    syncOldOcrButtons(pages.old_page_available);
    await loadWorkspace();
  }

  function highlightActiveCards(pageIndex) {
    ['#old-pages-list', '#new-pages-list'].forEach((sel) => {
      const root = $(sel);
      if (!root) return;
      root.querySelectorAll('.diff-view-page-card').forEach((card) => {
        card.classList.toggle('is-active', Number(card.dataset.pageIndex) === Number(pageIndex));
      });
    });
    paintOverlays();
  }

  function optimisticNavigateLesson(delta) {
    const nextIndex = Number(P.page_index || state?.page_index || 1) + delta;
    selectLessonPage(nextIndex).catch((e) => showToast(String(e)));
    return true;
  }

  function bindImageErrors() {
    // 列表模式用事件委托，见 bindPageListClicks
  }

  function atomRequestBody() {
    const body = {
      old_code: P.old_code,
      new_code: P.new_code,
      old_page: Number(state?.old_page || P.old_page),
      new_page: Number(state?.new_page || P.new_page),
    };
    if (P.mode !== 'lesson') {
      if (P.new_pdf_source) body.new_pdf_source = P.new_pdf_source;
      if (P.preview_blob_id) body.preview_blob_id = P.preview_blob_id;
    }
    return body;
  }

  function setAtomStatus(text) {
    const el = $('#atom-status');
    if (el) el.textContent = text;
  }

  let ocrTimer = null;
  function startOcrProgress(label) {
    clearInterval(ocrTimer);
    const t0 = Date.now();
    setAtomStatus(`${label}（已 ${Math.floor((Date.now() - t0) / 1000)} 秒…）`);
    ocrTimer = setInterval(() => {
      const sec = Math.floor((Date.now() - t0) / 1000);
      setAtomStatus(`${label}（已 ${sec} 秒，单页识别，首次约 1–2 分钟）`);
    }, 1000);
  }

  function stopOcrProgress() {
    clearInterval(ocrTimer);
    ocrTimer = null;
  }

  function sideLabel(side) {
    return side === 'old' ? '旧教材' : '新教材';
  }

  function sideTextDone(atoms, side) {
    return side === 'old' ? !!atoms?.old_text_ocr_done : !!atoms?.new_text_ocr_done;
  }

  function setOcrButtonsBusy(busyKey) {
    ocrBusyKey = busyKey || null;
    OCR_BUTTONS.forEach(({ id, side, phase, idle }) => {
      const btn = $(id);
      if (!btn) return;
      const key = `${side}:${phase}`;
      const busy = ocrBusyKey === key;
      btn.disabled = !!ocrBusyKey;
      if (busy) {
        if (side === 'both') {
          btn.textContent = phase === 'text' ? '两侧文字 OCR…' : '两侧图片 OCR…';
        } else {
          btn.textContent = phase === 'text' ? '文字 OCR 中…' : '图片 OCR 中…';
        }
      } else {
        btn.textContent = idle;
      }
    });
    const textCmp = $(TEXT_COMPARE_BTN);
    if (textCmp) {
      textCmp.disabled = !!ocrBusyKey;
      textCmp.textContent = ocrBusyKey === 'text-compare' ? '文字比对中…' : '② 文字比对';
    }
    const imgCmp = $(IMAGE_COMPARE_BTN);
    if (imgCmp) {
      imgCmp.disabled = !!ocrBusyKey;
      imgCmp.textContent = ocrBusyKey === 'image-compare' ? '图片比对中…' : '④ 图片比对';
    }
    const oneClick = $(PAGE_ONE_CLICK_BTN);
    if (oneClick) {
      oneClick.disabled = !!ocrBusyKey;
      oneClick.textContent = ocrBusyKey === 'page-pipeline' ? '本页一键中…' : '本页一键';
    }
    syncXiaokeOcrLessonBtn();
    syncLessonPipelineBtn();
  }

  function clearCompareHighlight() {
    textCompareState = null;
    imageCompareState = null;
    changedAtomIds = { old: new Set(), new: new Set() };
    imageChangedAtomIds = { old: new Set(), new: new Set() };
    ['#text-compare-summary', '#image-compare-summary'].forEach((sel) => {
      const el = $(sel);
      if (el) {
        el.hidden = true;
        el.innerHTML = '';
      }
    });
    ['#text-block-pairs', '#image-block-pairs'].forEach((sel) => {
      const el = $(sel);
      if (el) {
        el.hidden = true;
        el.innerHTML = '';
      }
    });
    if (isXiaokeLessonMode()) {
      showXiaokePhaseView();
    } else {
      activeResultTab = 'text';
      const tabs = $('#result-tabs');
      if (tabs) tabs.hidden = true;
      setResultTab('text');
    }
    syncPanelCompareMode();
  }

  function syncPanelCompareMode() {
    const hasCompare = !!(textCompareState || imageCompareState || lessonCompareState
      || (isXiaokeLessonMode() && $('#lesson-phase-panel') && !$('#lesson-phase-panel').hidden));
    const panel = document.querySelector('.diff-view-panel');
    panel?.classList.toggle('is-compare-mode', hasCompare);
    const catalog = $('#atom-catalog-wrap');
    if (catalog) {
      if (hasCompare) {
        catalog.hidden = true;
        catalog.setAttribute('hidden', '');
      }
    }
    const status = $('#atom-status');
    if (status) status.hidden = hasCompare;
  }

  function hideAtomCatalog() {
    const catalog = $('#atom-catalog-wrap');
    if (!catalog) return;
    catalog.hidden = true;
    catalog.setAttribute('hidden', '');
  }

  function showAtomCatalog() {
    if (textCompareState || imageCompareState || lessonCompareState) return;
    const catalog = $('#atom-catalog-wrap');
    if (!catalog) return;
    catalog.hidden = false;
    catalog.removeAttribute('hidden');
  }

  function setPanelHidden(el, hide) {
    if (!el) return;
    if (hide) {
      el.hidden = true;
      el.setAttribute('hidden', '');
    } else {
      el.hidden = false;
      el.removeAttribute('hidden');
    }
  }

  function setResultTab(tab) {
    if (isXiaokeLessonMode()) {
      activeResultTab = 'phase';
    } else if (tab === 'image') activeResultTab = 'image';
    else if (tab === 'lesson') activeResultTab = 'lesson';
    else if (tab === 'phase') activeResultTab = 'phase';
    else activeResultTab = 'text';
    $('#tab-result-text')?.classList.toggle('is-active', activeResultTab === 'text');
    $('#tab-result-image')?.classList.toggle('is-active', activeResultTab === 'image');
    $('#tab-result-lesson')?.classList.toggle('is-active', activeResultTab === 'lesson');
    const panel = document.querySelector('.diff-view-panel');
    panel?.setAttribute('data-result-tab', activeResultTab);
    const showText = !isXiaokeLessonMode() && activeResultTab === 'text' && !!textCompareState;
    const showImage = !isXiaokeLessonMode() && activeResultTab === 'image' && !!imageCompareState;
    const showLesson = !isXiaokeLessonMode() && activeResultTab === 'lesson' && !!lessonCompareState;
    const showPhase = isXiaokeLessonMode();
    setPanelHidden($('#text-compare-summary'), !showText);
    setPanelHidden($('#text-block-pairs'), !showText);
    setPanelHidden($('#image-compare-summary'), !showImage);
    setPanelHidden($('#lesson-compare-panel'), !showLesson);
    setPanelHidden($('#section-block-panel'), activeResultTab !== 'section');
    setPanelHidden($('#lesson-phase-panel'), !showPhase);
    const iPairs = $('#image-block-pairs');
    if (showImage) {
      setPanelHidden(iPairs, false);
    } else if (!isXiaokeLessonMode() && activeResultTab === 'image') {
      setPanelHidden(iPairs, false);
      if (iPairs) {
        const imgReady = !!(state?.atoms?.old_image_ocr_done && state?.atoms?.new_image_ocr_done);
        iPairs.innerHTML = imgReady
          ? '<p class="diff-block-empty-hint">图片 OCR 已就绪，点顶栏「④ 图片比对」查看结果（或稍候自动恢复）。</p>'
          : '<p class="diff-block-empty-hint">请先完成 ③ 图片 OCR，再点顶栏「④ 图片比对」。</p>';
      }
    } else {
      setPanelHidden(iPairs, true);
    }
    const tabs = $('#result-tabs');
    if (isXiaokeLessonMode()) {
      setPanelHidden(tabs, true);
      if (showPhase) {
        if (lessonCompareState) renderLessonPhases(lessonCompareState);
        else renderLessonPhasesEmpty();
      }
    } else {
      setPanelHidden(tabs, !(textCompareState || imageCompareState || lessonCompareState));
      const lessonTab = $('#tab-result-lesson');
      if (lessonTab) lessonTab.hidden = !lessonCompareState;
    }
    syncPanelCompareMode();
    paintOverlays();
  }

  function refreshResultPanels() {
    if (isXiaokeLessonMode()) {
      showXiaokePhaseView();
      syncPanelCompareMode();
      return;
    }
    const tabs = $('#result-tabs');
    if (tabs) tabs.hidden = !(textCompareState || imageCompareState || lessonCompareState);
    if (textCompareState) renderTextBlockPairs(textCompareState);
    if (imageCompareState) renderImageBlockPairs(imageCompareState);
    if (lessonCompareState) renderLessonCompare(lessonCompareState);
    setResultTab(activeResultTab);
    if (activeResultTab === 'image' && !imageCompareState && textCompareState) {
      setResultTab('text');
    }
    if (activeResultTab === 'text' && !textCompareState && imageCompareState) {
      setResultTab('image');
    }
    if (activeResultTab === 'lesson' && !lessonCompareState) {
      setResultTab(textCompareState ? 'text' : (imageCompareState ? 'image' : 'text'));
    }
    syncPanelCompareMode();
  }

  const PINYIN_PAREN_RE = /^\([a-zA-ZüÜāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ\s]+\)/;

  function mergeMarks(marks) {
    if (!marks.length) return [];
    marks.sort((a, b) => a.start - b.start || a.end - b.end);
    const out = [];
    marks.forEach((m) => {
      const last = out[out.length - 1];
      if (last && m.start <= last.end) {
        last.end = Math.max(last.end, m.end);
        last.punct = last.punct && m.punct;
      } else {
        out.push({ ...m });
      }
    });
    return out;
  }

  /** 删拼音时：旧侧把前一个汉字一并标红；新侧把对应汉字标红。 */
  function buildPairMarks(oldText, newText, ops) {
    const oldMarks = [];
    const newMarks = [];
    (ops || []).forEach((op) => {
      const punct = String(op.kind || '').includes('标点');
      const oSpan = op.old_span;
      const nSpan = op.new_span;
      if (Array.isArray(oSpan) && oSpan[0] !== oSpan[1]) {
        let start = Math.max(0, oSpan[0]);
        let end = Math.min(oldText.length, oSpan[1]);
        const chunk = oldText.slice(start, end);
        if (PINYIN_PAREN_RE.test(chunk) && start > 0) {
          const prev = oldText[start - 1];
          if (/[\u4e00-\u9fff]/.test(prev)) start -= 1;
        }
        oldMarks.push({ start, end, punct, mode: 'del' });
      }
      if (Array.isArray(nSpan) && nSpan[0] !== nSpan[1]) {
        newMarks.push({
          start: Math.max(0, nSpan[0]),
          end: Math.min(newText.length, nSpan[1]),
          punct,
          mode: 'ins',
        });
      }
      // 旧删拼音、新无对应插入：在新文高亮「前一字」
      const oldChunk = Array.isArray(oSpan) ? oldText.slice(oSpan[0], oSpan[1]) : '';
      const newChunk = Array.isArray(nSpan) ? newText.slice(nSpan[0], nSpan[1]) : '';
      if (PINYIN_PAREN_RE.test(oldChunk) && !newChunk && Array.isArray(oSpan) && oSpan[0] > 0) {
        const han = oldText[oSpan[0] - 1];
        if (/[\u4e00-\u9fff]/.test(han)) {
          // 用旧文删段前的上下文在新文定位该字
          const left = oldText.slice(Math.max(0, oSpan[0] - 8), oSpan[0]);
          const needle = left + han;
          let pos = newText.indexOf(needle);
          if (pos >= 0) {
            pos += left.length;
          } else {
            pos = newText.indexOf(han);
            // 优先找与 left 尾部重合的
            const tail = left.slice(-2);
            if (tail) {
              const p2 = newText.indexOf(tail + han);
              if (p2 >= 0) pos = p2 + tail.length;
            }
          }
          if (pos >= 0) {
            newMarks.push({ start: pos, end: pos + 1, punct: false, mode: 'focus' });
          }
        }
      }
    });
    return { oldMarks: mergeMarks(oldMarks), newMarks: mergeMarks(newMarks) };
  }

  function renderMarkedText(text, marks) {
    const raw = text == null ? '' : String(text);
    if (!raw) return '<span class="diff-block-empty">—</span>';
    if (!marks?.length) return esc(raw);
    // 找到所有 $...$ 区间，mark 边界不得切进 $...$ 内部
    const mathRanges = [];
    const M = /\$([^\$\n]{1,500})\$/g;
    let mm;
    while ((mm = M.exec(raw)) !== null) {
      mathRanges.push([mm.index, mm.index + mm[0].length]);
    }
    function clampToMath(start, end) {
      // 如果 mark 边界落在某个 $...$ 内部，扩展到该 $...$ 边界外
      for (const [ms, me] of mathRanges) {
        if (start > ms && start < me) {
          // start 在 $...$ 内部：把 mark 起点移到 $...$ 之前（或之后）
          if (end <= me) { start = ms; end = Math.max(end, me); }
          else { start = ms; }
        }
        if (end > ms && end < me) {
          if (start >= ms) { end = me; start = Math.min(start, ms); }
          else { end = me; }
        }
      }
      return [start, end];
    }
    let html = '';
    let cursor = 0;
    marks.forEach((m) => {
      if (m.start < cursor) return;
      let [s, e] = clampToMath(m.start, m.end);
      if (s < cursor) s = cursor;
      if (e <= s) return;
      if (s > cursor) html += esc(raw.slice(cursor, s));
      let cls = 'diff-hl-focus';
      if (m.punct) cls = 'diff-hl-punct';
      else if (m.mode === 'del') cls = 'diff-hl-del';
      else if (m.mode === 'ins') cls = 'diff-hl-ins';
      html += `<mark class="${cls}">${esc(raw.slice(s, e))}</mark>`;
      cursor = e;
    });
    if (cursor < raw.length) html += esc(raw.slice(cursor));
    return html;
  }

  function splitChangeSummary(summary) {
    const s = String(summary || '').trim();
    if (!s) return [];
    const m = s.match(/^(读音表|写字表|对照表)[:：](.*)$/);
    if (!m) return [s];
    const kind = m[1];
    const rest = (m[2] || '').trim();
    if (!rest) return [kind];
    const parts = rest.split(/[；;]/).map((p) => p.trim()).filter(Boolean);
    if (!parts.length) return [s];
    return [kind, ...parts];
  }

  function formatNoteLineHtml(line) {
    const t = String(line || '');
    if (t === '读音表' || t === '写字表' || t === '对照表' || t === '引号位置改动') {
      return `<div class="diff-table-note-line diff-table-note-line--label">${esc(t)}</div>`;
    }
    if (t.startsWith('删')) {
      return `<div class="diff-table-note-line"><span class="diff-note-verb diff-note-verb--del">删</span>${esc(t.slice(1))}</div>`;
    }
    if (t.startsWith('增')) {
      return `<div class="diff-table-note-line"><span class="diff-note-verb diff-note-verb--ins">增</span>${esc(t.slice(1))}</div>`;
    }
    if (t.startsWith('移动')) {
      return `<div class="diff-table-note-line"><span class="diff-note-verb diff-note-verb--move">移动</span>${esc(t.slice(2))}</div>`;
    }
    if (t.startsWith('改')) {
      return `<div class="diff-table-note-line"><span class="diff-note-verb diff-note-verb--chg">改</span>${esc(t.slice(1))}</div>`;
    }
    return `<div class="diff-table-note-line">${esc(t)}</div>`;
  }

  function describeBlockChange(row) {
    if (Array.isArray(row.change_summary_lines) && row.change_summary_lines.length) {
      return row.change_summary_lines;
    }
    if (row.change_summary) {
      return splitChangeSummary(row.change_summary);
    }
    const ops = row.ops || [];
    if (!ops.length) return ['本块一致，无需改动。'];
    return ops.slice(0, 8).map((op) => {
      const oldC = (op.old || '').trim();
      const newC = (op.new || '').trim();
      if (!oldC && !newC) return null;
      if (PINYIN_PAREN_RE.test(oldC) && !newC) {
        const han = (row.old_text || '')[(op.old_span && op.old_span[0] > 0) ? op.old_span[0] - 1 : -1];
        const tip = /[\u4e00-\u9fff]/.test(han || '') ? `「${han}」` : '该字';
        return `新教材去掉${tip}的拼音 ${oldC}`;
      }
      if (!oldC && PINYIN_PAREN_RE.test(newC)) {
        return `新教材新增拼音 ${newC}`;
      }
      if (String(op.kind || '').includes('标点')) {
        return `${op.kind}：${oldC || '∅'} → ${newC || '∅'}`;
      }
      if (op.kind === '文字删除') return oldC ? `新教材删除「${oldC}」` : null;
      if (op.kind === '文字新增') return newC ? `新教材新增「${newC}」` : null;
      if (op.kind === '文字移动') {
        const t = oldC || newC;
        return t ? `移动「${t}」` : null;
      }
      if (op.kind === '文字改写') {
        if (!oldC && !newC) return null;
        return `文字改写：「${oldC}」→「${newC}」`;
      }
      return `${op.kind || '变化'}：${oldC || '∅'} → ${newC || '∅'}`;
    }).filter(Boolean);
  }

  function normalizeBlockRows(compare) {
    if (compare?.block_rows?.length) return compare.block_rows;
    // 旧后端无 block_rows 时，用 anchors 在前端拼同块行
    const rows = [];
    (compare?.anchors || []).forEach((a) => {
      const ot = (a.old_text || '').trim();
      const nt = (a.new_text || '').trim();
      rows.push({
        kind: 'pair',
        old_atom_id: a.old_atom_id,
        new_atom_id: a.new_atom_id,
        old_text: ot,
        new_text: nt,
        change: a.change || (ot === nt ? '一致' : '文字有差异'),
        ops: [],
        similarity: a.text_similarity ?? 0,
      });
    });
    (compare?.unmatched_old || []).forEach((u) => {
      rows.push({
        kind: 'old_only',
        old_atom_id: u.atom_id,
        new_atom_id: null,
        old_text: (u.display_text || u.ocr_text || '').trim(),
        new_text: '',
        change: '旧有新无',
        ops: [],
        similarity: 0,
      });
    });
    (compare?.unmatched_new || []).forEach((u) => {
      rows.push({
        kind: 'new_only',
        old_atom_id: null,
        new_atom_id: u.atom_id,
        old_text: '',
        new_text: (u.display_text || u.ocr_text || '').trim(),
        change: '新有旧无',
        ops: [],
        similarity: 0,
      });
    });
    return rows;
  }

  function changeLabel(change) {
    if (!change || change === '一致') return '没变化';
    if (change === '分页错位') return '分页错位';
    if (change === '位置移动') return '移动';
    return change;
  }

  function isSoftSameChange(change) {
    const label = changeLabel(change);
    return label === '没变化' || label === '分页错位';
  }

  function lessonRowDiffKind(row) {
    if (row?.diff_kind) return String(row.diff_kind);
    const change = String(row?.change || '');
    const oi = Number(row?.old_lesson_page_index || 0);
    const ni = Number(row?.new_lesson_page_index || 0);
    if (change === '分页错位' || change === '位置移动' || (oi && ni && oi !== ni)) return 'crosspage';
    if (change === '旧有新无' || (row?.old_atom_id && !row?.new_atom_id)) return 'delete';
    if (change === '新有旧无' || (row?.new_atom_id && !row?.old_atom_id)) return 'add';
    if (isSoftSameChange(change) || !change) return 'same';
    return 'modify';
  }

  function lessonDiffBoxClass(kind) {
    if (kind === 'delete') return ' is-diff-delete';
    if (kind === 'add') return ' is-diff-add';
    if (kind === 'modify') return ' is-diff-modify';
    if (kind === 'crosspage') return ' is-diff-crosspage';
    return ' is-diff-same';
  }

  function bundleLocalAtomId(bundleId, pdfPage) {
    if (!bundleId || !pdfPage) return null;
    const prefix = `p${Number(pdfPage)}_`;
    const s = String(bundleId);
    if (s.startsWith(prefix)) return s.slice(prefix.length);
    return s;
  }

  /** 当前页：local atom_id → diff_kind（来自整课 atom_rows） */
  function lessonDiffKindByAtomId(side, pdfPage, { image = false } = {}) {
    const map = new Map();
    if (!lessonCompareState) return map;
    const page = Number(pdfPage);
    if (!page) return map;
    const rows = image
      ? (lessonCompareState.image_rows || [])
      : (lessonCompareState.atom_rows || []);
    rows.forEach((row) => {
      const kind = lessonRowDiffKind(row);
      if (side === 'old') {
        if (Number(row.old_pdf_page) !== page || !row.old_atom_id) return;
        const local = bundleLocalAtomId(row.old_atom_id, row.old_pdf_page);
        if (local) map.set(local, kind);
      } else {
        if (Number(row.new_pdf_page) !== page || !row.new_atom_id) return;
        const local = bundleLocalAtomId(row.new_atom_id, row.new_pdf_page);
        if (local) map.set(local, kind);
      }
    });
    return map;
  }

  function ingestLessonAtomsByPage(result) {
    lessonAtomsByPage = { old: {}, new: {} };
    lessonPhasesByPage = { old: {}, new: {} };
    selectedLessonPair = null;
    selectedLessonPhase = null;
    if (!result) return;
    const oldMap = result.old_atoms_by_page || {};
    const newMap = result.new_atoms_by_page || {};
    Object.keys(oldMap).forEach((k) => {
      lessonAtomsByPage.old[String(k)] = Array.isArray(oldMap[k]) ? oldMap[k] : [];
    });
    Object.keys(newMap).forEach((k) => {
      lessonAtomsByPage.new[String(k)] = Array.isArray(newMap[k]) ? newMap[k] : [];
    });
    const oldPhases = result.old_phases_by_page || {};
    const newPhases = result.new_phases_by_page || {};
    Object.keys(oldPhases).forEach((k) => {
      lessonPhasesByPage.old[String(k)] = Array.isArray(oldPhases[k]) ? oldPhases[k] : [];
    });
    Object.keys(newPhases).forEach((k) => {
      lessonPhasesByPage.new[String(k)] = Array.isArray(newPhases[k]) ? newPhases[k] : [];
    });
  }

  function describeLessonBlockChange(row) {
    const label = changeLabel(row.change);
    const pageHint = [
      row.old_pdf_page != null ? `旧 p${row.old_pdf_page}` : '',
      row.new_pdf_page != null ? `新 p${row.new_pdf_page}` : '',
    ].filter(Boolean).join(' → ');
    if (label === '没变化') return ['没变化'];
    if (row.change === '旧有新无') {
      const t = (row.old_text || '').trim();
      return [t ? `新教材删除「${t.slice(0, 40)}${t.length > 40 ? '…' : ''}」` : '旧有新无'];
    }
    if (row.change === '新有旧无') {
      const t = (row.new_text || '').trim();
      return [t ? `新教材新增「${t.slice(0, 40)}${t.length > 40 ? '…' : ''}」` : '新有旧无'];
    }
    if (row.change === '位置移动' || row.change === '分页错位') {
      const lines = [];
      if (pageHint) lines.push(`移动：${pageHint}`);
      else lines.push('位置移动');
      if (row.match_reason || row.change_summary) {
        lines.push(String(row.match_reason || row.change_summary).trim());
      }
      const opsNotes = describeBlockChange({ ...row, change_summary: '', change_summary_lines: null });
      if (opsNotes.length && !(opsNotes.length === 1 && opsNotes[0].includes('一致'))) {
        lines.push(...opsNotes.filter((n) => n && !n.includes('一致')));
      }
      return lines.filter(Boolean);
    }
    const notes = describeBlockChange(row);
    if (row.match_reason && !notes.some((n) => n.includes(row.match_reason))) {
      return [String(row.match_reason).trim(), ...notes].filter(Boolean);
    }
    return notes;
  }

  function phasePdfPages(ph) {
    if (!ph || typeof ph !== 'object') return [];
    const pages = (Array.isArray(ph.page_boxes) ? ph.page_boxes : [])
      .map((pb) => Number(pb && pb.pdf_page) || 0)
      .filter((p) => p > 0);
    if (pages.length) return Array.from(new Set(pages)).sort((a, b) => a - b);
    const fallback = Number(ph.pdf_page || ph.start_page || 0);
    return fallback > 0 ? [fallback] : [];
  }

  const ADVICE_SUMMARY_REUSE_MIN = 0.25;
  const ADVICE_READING_REUSE_MIN = 0.40;

  const NEW_KNOWLEDGE_CUE_RE = /古代|古今|亚里士多德|林奈|达尔文|博物学家|分类学家|两位.{0,8}学家|沿革|新标准|新概念/;

  function newKnowledgePointIntroduced(oldSum, newSum) {
    const n = String(newSum || '');
    const o = String(oldSum || '');
    return n && NEW_KNOWLEDGE_CUE_RE.test(n) && !NEW_KNOWLEDGE_CUE_RE.test(o);
  }

  function effectivePhaseChangeSize(ch) {
    if (!ch || typeof ch !== 'object') return 'small';
    const kind = String(ch.kind || 'match');
    if (kind === 'added' || kind === 'removed') return 'large';
    if (ch.introduces_new_knowledge) return 'large';
    if (newKnowledgePointIntroduced(ch.old_summary, ch.new_summary)) return 'large';
    return String(ch.change_size || 'small');
  }

  function hanBlob(text) {
    return Array.from(String(text || '')).filter((ch) => /[\u4e00-\u9fff]/.test(ch)).join('');
  }

  function summarySim(a, b) {
    const ha = hanBlob(a);
    const hb = hanBlob(b);
    if (!ha && !hb) return 1;
    if (!ha || !hb) return 0;
    const n = ha.length;
    const m = hb.length;
    const prev = new Uint16Array(m + 1);
    for (let i = 1; i <= n; i += 1) {
      const cur = new Uint16Array(m + 1);
      for (let j = 1; j <= m; j += 1) {
        cur[j] = ha[i - 1] === hb[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], cur[j - 1]);
      }
      prev.set(cur);
    }
    return (2 * prev[m]) / (n + m);
  }

  function joinPhaseSummaries(rows, key) {
    return (Array.isArray(rows) ? rows : [])
      .map((row) => String((row && row[key]) || '').trim())
      .filter(Boolean)
      .join('。');
  }

  function suggestLessonReuse(result) {
    if (!result) return null;
    const backend = String(result.change_advice || '').trim();
    const backendReason = String(result.change_advice_reason || '').trim();
    if (backend === '新制') {
      return {
        value: 'new',
        reason: backendReason || '教研判定：知识点有实质变化，建议新制',
      };
    }
    if (backend === '直接复用') {
      return {
        value: 'reuse',
        reason: backendReason || '教研判定：知识点未实质变化，建议直接复用',
      };
    }
    const changes = Array.isArray(result.phase_changes) ? result.phase_changes : [];
    const oldPhases = Array.isArray(result.old_phases) ? result.old_phases : [];
    const newPhases = Array.isArray(result.new_phases) ? result.new_phases : [];
    if (!changes.length && !oldPhases.length && !newPhases.length) return null;
    let oldSum = joinPhaseSummaries(oldPhases, 'summary');
    let newSum = joinPhaseSummaries(newPhases, 'summary');
    if (!oldSum && !newSum) {
      oldSum = joinPhaseSummaries(changes, 'old_summary');
      newSum = joinPhaseSummaries(changes, 'new_summary');
    }
    const sumSim = summarySim(oldSum, newSum);
    const textSim = summarySim(result.old_reading_text || '', result.new_reading_text || '');
    const hasText = hanBlob(result.old_reading_text || '') || hanBlob(result.new_reading_text || '');
    if (sumSim >= ADVICE_SUMMARY_REUSE_MIN || (hasText && textSim >= ADVICE_READING_REUSE_MIN)) {
      return { value: 'reuse', reason: '新旧课环节任务仍接近，建议直接复用' };
    }
    return { value: 'new', reason: '新旧课环节任务差异较大，建议新制' };
  }

  function syncLessonReuseSuggest() {
    const wrap = $('#lesson-reuse-suggest');
    if (!wrap) return;
    const sug = isXiaokeLessonMode() ? suggestLessonReuse(lessonCompareState) : null;
    if (!sug) {
      wrap.hidden = true;
      wrap.classList.remove('is-new', 'is-reuse');
      return;
    }
    wrap.hidden = false;
    wrap.classList.toggle('is-new', sug.value === 'new');
    wrap.classList.toggle('is-reuse', sug.value === 'reuse');
    wrap.title = '采用 C 方案（模型+示例），再用环节对比加权：证据足才把直接复用改为新制';
    wrap.querySelectorAll('.diff-reuse-opt').forEach((lab) => {
      const val = lab.dataset.value;
      const inp = lab.querySelector('input');
      const on = val === sug.value;
      if (inp) inp.checked = on;
      lab.classList.toggle('is-active', on);
      lab.hidden = !on;
    });
    const reason = $('#lesson-reuse-reason');
    if (reason) reason.textContent = sug.reason;
  }

  function phaseChangeMetaById() {
    const oldMap = {};
    const newMap = {};
    const rows = (lessonCompareState && lessonCompareState.phase_changes) || [];
    rows.forEach((ch) => {
      if (!ch || typeof ch !== 'object') return;
      const kind = String(ch.kind || 'match');
      const meta = { kind, size: effectivePhaseChangeSize(ch) };
      const oid = String(ch.old_phase_id || '').trim();
      const nid = String(ch.new_phase_id || '').trim();
      if (oid) oldMap[oid] = meta;
      if (nid) newMap[nid] = meta;
    });
    return { old: oldMap, new: newMap };
  }

  function phaseBoxMatchesSelection(side, phaseId) {
    if (!selectedLessonPhase) return true;
    const pid = String(phaseId || '').trim();
    if (!pid) return false;
    if (selectedLessonPhase.fromChange) {
      return side === 'old'
        ? pid === (selectedLessonPhase.oldPhaseId || '')
        : pid === (selectedLessonPhase.newPhaseId || '');
    }
    return selectedLessonPhase.side === side && pid === (selectedLessonPhase.phaseId || '');
  }

  function phasePagesHint(ph) {
    const uniq = phasePdfPages(ph);
    if (!uniq.length) return '';
    if (uniq.length === 1) return `p${uniq[0]}`;
    return `p${uniq[0]}–p${uniq[uniq.length - 1]}`;
  }

  function firstPhasePdfPage(ph) {
    const pages = phasePdfPages(ph);
    return pages.length ? pages[0] : null;
  }

  function renderPhaseSideList(side, phases) {
    const sideLabel = side === 'old' ? '旧' : '新';
    const list = (Array.isArray(phases) ? phases : []).filter((ph) => ph && typeof ph === 'object');
    if (!list.length) {
      return `<p class="diff-block-empty-hint">${sideLabel}侧暂无环节数据。</p>`;
    }
    return list.map((ph, idx) => {
      const label = String(ph.label || '').trim() || '其它';
      const summary = String(ph.summary || '').trim();
      const pages = phasePagesHint(ph);
      const atomN = Array.isArray(ph.atom_ids) ? ph.atom_ids.length : 0;
      const firstPage = firstPhasePdfPage(ph);
      const pid = String(ph.phase_id || '').trim();
      return `<div class="diff-phase-item" role="button" tabindex="0"`
        + ` data-side="${side}" data-phase-id="${esc(pid)}"`
        + (firstPage != null ? ` data-pdf-page="${Number(firstPage)}"` : '')
        + `>`
        + `<div class="diff-phase-item-meta">`
        + `<span class="diff-phase-item-idx">${idx + 1}</span>`
        + `<span class="diff-phase-item-label">${esc(label)}</span>`
        + (pages ? `<span class="diff-phase-item-pages">${esc(pages)}</span>` : '')
        + `</div>`
        + (summary
          ? `<div class="diff-phase-item-summary">${esc(summary)}</div>`
          : '')
        + `<div class="diff-phase-item-sub">${atomN} 个原子</div>`
        + `</div>`;
    }).join('');
  }

  function phaseChangeSizeLabel(size, kind) {
    if (kind === 'added') return '新增·大改';
    if (kind === 'removed') return '删除·大改';
    if (size === 'large') return '大改';
    if (size === 'medium') return '中改';
    return '小改';
  }

  function phaseById(phases, phaseId) {
    const pid = String(phaseId || '').trim();
    if (!pid) return null;
    return (phases || []).find((p) => String(p.phase_id || '').trim() === pid) || null;
  }

  function clipPhaseTopic(text, limit) {
    const t = String(text || '').replace(/\s+/g, ' ').trim();
    if (t.length <= limit) return t;
    return `${t.slice(0, limit - 1)}…`;
  }

  function isWordDiffNarrative(text) {
    return /旧「|旧侧重|内容有调整：|建议对照新旧页|新版改为|引入了旧课没有覆盖的知识内容|没有换成全新知识点/.test(String(text || ''));
  }

  function concreteNewReason(oldSum, newSum) {
    const n = String(newSum || '');
    const o = String(oldSum || '');
    if (/古代|古今|亚里士多德|林奈/.test(n) && !/古代|古今|亚里士多德|林奈/.test(o)) {
      if (/分类|动物|生物/.test(n + o)) return '引入了两位古代生物学家';
      return '引入了古代人物或知识史内容';
    }
    if (/新标准|新概念/.test(n) && !/新标准|新概念/.test(o)) {
      return '引入了新的分类标准或概念';
    }
    const topic = clipPhaseTopic(n, 22);
    if (topic) return `增加了「${topic}」`;
    return '调整了本环节的学习任务';
  }

  function teacherPhaseDetail(ch) {
    const kind = String(ch.kind || 'match');
    const size = effectivePhaseChangeSize(ch);
    const reason = concreteNewReason(ch.old_summary, ch.new_summary);
    if (kind === 'removed') {
      return '由于新教材不再单独安排本环节，所以在这个教学环节中相关知识点可能被合并或删减，改动较大';
    }
    if (kind === 'added') {
      return `由于新教材${reason}并新增了本环节，所以在这个教学环节中是引入了新的知识任务，改动较大`;
    }
    if (size === 'small') {
      return '由于新旧教材在本环节的知识任务基本一致，所以在这个教学环节中没有引入新的知识点，改动较小';
    }
    if (size === 'medium') {
      return '由于新教材主要调整了活动方式或例证，所以在这个教学环节中没有引入新的知识点，改动中等';
    }
    return `由于新教材${reason}，所以在这个教学环节中是引入了新的知识点，改动较大`;
  }

  function teacherPhaseSuggestion() {
    return '';
  }

  function displayPhaseDetail(ch) {
    const raw = String(ch.detail || '').trim();
    if (raw && /由于.+所以在这个教学环节中/.test(raw) && !isWordDiffNarrative(raw)) {
      return raw;
    }
    return teacherPhaseDetail(ch);
  }

  function displayPhaseSuggestion(ch) {
    const detail = displayPhaseDetail(ch);
    if (/所以在这个教学环节中/.test(detail)) return '';
    const raw = String(ch.suggestion || '').trim();
    if (raw && !isWordDiffNarrative(raw)) return raw;
    return teacherPhaseSuggestion(ch);
  }

  function renderPhaseChanges(changes, oldPhases, newPhases) {
    const rows = Array.isArray(changes) ? changes : [];
    if (!rows.length) return '';
    const largeN = rows.filter((c) => effectivePhaseChangeSize(c) === 'large').length;
    const items = rows.map((ch, idx) => {
      const kind = String(ch.kind || 'match');
      const size = effectivePhaseChangeSize(ch);
      const badge = phaseChangeSizeLabel(size, kind);
      const label = String(ch.label || '').trim() || '环节';
      const detail = displayPhaseDetail(ch);
      const suggestion = displayPhaseSuggestion(ch);
      const oldPage = firstPhasePdfPage(phaseById(oldPhases, ch.old_phase_id));
      const newPage = firstPhasePdfPage(phaseById(newPhases, ch.new_phase_id));
      return `<div class="diff-phase-change is-${esc(kind)} is-${esc(size)}"`
        + ` data-change-idx="${idx}"`
        + ` data-old-phase-id="${esc(ch.old_phase_id || '')}"`
        + ` data-new-phase-id="${esc(ch.new_phase_id || '')}"`
        + (oldPage != null ? ` data-old-page="${Number(oldPage)}"` : '')
        + (newPage != null ? ` data-new-page="${Number(newPage)}"` : '')
        + ` role="button" tabindex="0">`
        + `<div class="diff-phase-change-head">`
        + `<span class="diff-phase-change-badge">${esc(badge)}</span>`
        + `<span class="diff-phase-change-label">${esc(label)}</span>`
        + `</div>`
        + (detail ? `<div class="diff-phase-change-detail">${esc(detail)}</div>` : '')
        + (suggestion ? `<div class="diff-phase-change-suggest">${esc(suggestion)}</div>` : '')
        + `</div>`;
    }).join('');
    return `<div class="diff-phase-changes">`
      + `<h3 class="diff-phase-list-title">环节改动对照`
      + `<span class="diff-phase-change-count">（${rows.length} 项 · 大改 ${largeN}）</span></h3>`
      + items
      + `</div>`;
  }

  function renderLessonPhasesEmpty() {
    const panel = $('#lesson-phase-panel');
    const sum = $('#lesson-phase-summary');
    const rowsEl = $('#lesson-phase-rows');
    const title = $('#panel-compare-title');
    const panelLabel = $('#panel-page-label');
    if (title) title.textContent = '环节对比';
    if (panelLabel) panelLabel.textContent = '· 教学环节';
    if (sum) {
      sum.hidden = false;
      sum.innerHTML = '<p class="diff-compare-verdict">教学环节对照</p>'
        + '<p class="diff-compare-meta">尚无环节数据</p>';
    }
    if (rowsEl) {
      rowsEl.innerHTML = '<p class="diff-block-empty-hint">暂无环节对照数据。</p>';
      rowsEl.hidden = false;
    }
    const toggle = $('#lesson-phase-toggle');
    if (toggle) toggle.hidden = true;
    if (panel) {
      panel.hidden = false;
      panel.removeAttribute('hidden');
    }
    setPanelHidden($('#lesson-compare-panel'), true);
    hideAtomCatalog();
    syncPanelCompareMode();
  }

  function showXiaokePhaseView() {
    xiaokeOverlayLayer = 'phase';
    xiaokePhaseView = 'phase';
    showAtomOverlay = true;
    $('#btn-xiaoke-phase-compare')?.classList.add('is-active');
    if (lessonCompareState) renderLessonPhases(lessonCompareState);
    else renderLessonPhasesEmpty();
    paintOverlays();
  }

  function renderLessonPhases(result) {
    const panel = $('#lesson-phase-panel');
    const sum = $('#lesson-phase-summary');
    const rowsEl = $('#lesson-phase-rows');
    if (!panel || !result) return;
    hideAtomCatalog();
    setPanelHidden($('#lesson-compare-panel'), true);
    const oldPhases = (Array.isArray(result.old_phases) ? result.old_phases : [])
      .filter((ph) => ph && typeof ph === 'object');
    const newPhases = (Array.isArray(result.new_phases) ? result.new_phases : [])
      .filter((ph) => ph && typeof ph === 'object');
    const changes = Array.isArray(result.phase_changes) ? result.phase_changes : [];
    const title = $('#panel-compare-title');
    const panelLabel = $('#panel-page-label');
    if (title) title.textContent = '环节对比';
    if (panelLabel) panelLabel.textContent = `· 旧 ${oldPhases.length} / 新 ${newPhases.length}`;
    const largeN = changes.filter((c) => effectivePhaseChangeSize(c) === 'large').length;
    if (sum) {
      sum.hidden = false;
      sum.innerHTML = `<p class="diff-compare-verdict">教学环节对照（从知识点与能力目标判断）</p>`
        + `<p class="diff-compare-meta">旧 ${oldPhases.length} 个 · 新 ${newPhases.length} 个`
        + (changes.length ? ` · 对照 ${changes.length} 项（大改 ${largeN}）` : '')
        + ` · 点击对照项只显示对应环节框，再点一次恢复全部</p>`;
    }
    syncXiaokePhaseToggle(result);
    if (rowsEl) {
      renderXiaokePhaseRows(result, { oldPhases, newPhases, changes });
      rowsEl.hidden = false;
    }
    panel.hidden = false;
    paintOverlays();
    syncLessonReuseSuggest();
  }

  /** 同步环节面板顶部三联切换按钮的显隐与激活态。 */
  function syncXiaokePhaseToggle(result) {
    const toggle = $('#lesson-phase-toggle');
    if (!toggle) return;
    const hasText = !!(result && (result.atom_rows || []).length);
    const hasImage = !!(result && (result.image_rows || []).length);
    // 只要进入环节面板就显示切换条；无数据的按钮禁用而非隐藏整条
    toggle.hidden = false;
    // 当前视图无数据时回退到环节对比
    if (xiaokePhaseView === 'text' && !hasText) xiaokePhaseView = 'phase';
    if (xiaokePhaseView === 'image' && !hasImage) xiaokePhaseView = 'phase';
    toggle.querySelectorAll('.diff-phase-toggle-btn').forEach((btn) => {
      const v = btn.dataset.phaseView || 'phase';
      btn.classList.toggle('is-active', v === xiaokePhaseView);
      // 无对应数据时禁用对应按钮
      if (v === 'text') btn.disabled = !hasText;
      else if (v === 'image') btn.disabled = !hasImage;
    });
  }

  /** 渲染环节面板主体：按当前切换视图（phase/text/image）填充 #lesson-phase-rows。 */
  function renderXiaokePhaseRows(result, ctx = {}) {
    const rowsEl = $('#lesson-phase-rows');
    if (!rowsEl || !result) return;
    if (xiaokePhaseView === 'text') {
      rowsEl.innerHTML = buildLessonCompareRowsHtml(result, false);
      bindXiaokeCompareRows(rowsEl);
      return;
    }
    if (xiaokePhaseView === 'image') {
      rowsEl.innerHTML = buildLessonCompareRowsHtml(result, true);
      bindXiaokeCompareRows(rowsEl);
      return;
    }
    // 环节对比（默认）
    const { oldPhases, newPhases, changes } = ctx;
    const phaseChanges = Array.isArray(changes) ? changes : [];
    const oldPh = Array.isArray(oldPhases) ? oldPhases : [];
    const newPh = Array.isArray(newPhases) ? newPhases : [];
    try {
      rowsEl.innerHTML = renderPhaseChanges(phaseChanges, oldPh, newPh)
        + `<div class="diff-phase-lists">`
        + `<div class="diff-phase-list-col diff-phase-list-col--old">`
        + `<h3 class="diff-phase-list-title">旧教材环节</h3>`
        + renderPhaseSideList('old', oldPh)
        + `</div>`
        + `<div class="diff-phase-list-col diff-phase-list-col--new">`
        + `<h3 class="diff-phase-list-title">新教材环节</h3>`
        + renderPhaseSideList('new', newPh)
        + `</div></div>`;
    } catch (e) {
      rowsEl.innerHTML = `<p class="diff-block-empty-hint">环节列表渲染失败：${esc(e && e.message ? e.message : e)}</p>`;
    }
    rowsEl.querySelectorAll('.diff-phase-item').forEach((row) => {
      row.addEventListener('click', () => selectLessonPhaseRow(row));
      row.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        ev.preventDefault();
        selectLessonPhaseRow(row);
      });
    });
    rowsEl.querySelectorAll('.diff-phase-change').forEach((row) => {
      row.addEventListener('click', () => selectPhaseChangeRow(row));
      row.addEventListener('keydown', (ev) => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        ev.preventDefault();
        selectPhaseChangeRow(row);
      });
    });
    syncLessonPhaseRowActive();
  }

  /** 文字/图片对比视图行：点击定位到对应页图原子（仅定位，不切换叠加层）。 */
  function bindXiaokeCompareRows(rowsEl) {
    if (!rowsEl) return;
    rowsEl.querySelectorAll('.diff-lesson-atom-row').forEach((row) => {
      row.addEventListener('click', () => {
        const oldPage = Number(row.dataset.oldPage || 0) || null;
        const newPage = Number(row.dataset.newPage || 0) || null;
        if (row.dataset.oldId) scrollLessonPdfPageIntoView('old', oldPage);
        if (row.dataset.newId) scrollLessonPdfPageIntoView('new', newPage);
      });
    });
  }

  /** 切换环节面板内的对比视图。 */
  function setXiaokePhaseView(view) {
    if (view !== 'phase' && view !== 'text' && view !== 'image') return;
    xiaokePhaseView = view;
    if (lessonCompareState) {
      const result = lessonCompareState;
      const oldPhases = (Array.isArray(result.old_phases) ? result.old_phases : [])
        .filter((ph) => ph && typeof ph === 'object');
      const newPhases = (Array.isArray(result.new_phases) ? result.new_phases : [])
        .filter((ph) => ph && typeof ph === 'object');
      const changes = Array.isArray(result.phase_changes) ? result.phase_changes : [];
      syncXiaokePhaseToggle(result);
      renderXiaokePhaseRows(result, { oldPhases, newPhases, changes });
    }
  }

  function selectPhaseChangeRow(row) {
    if (!row) return;
    const oldId = row.dataset.oldPhaseId || '';
    const newId = row.dataset.newPhaseId || '';
    const oldPage = Number(row.dataset.oldPage || 0) || null;
    const newPage = Number(row.dataset.newPage || 0) || null;
    const already = !!(selectedLessonPhase
      && selectedLessonPhase.fromChange
      && (selectedLessonPhase.oldPhaseId || '') === oldId
      && (selectedLessonPhase.newPhaseId || '') === newId);
    if (already) {
      selectedLessonPhase = null;
      syncLessonPhaseRowActive();
      paintOverlays();
      return;
    }
    selectedLessonPhase = {
      side: newId ? 'new' : 'old',
      phaseId: newId || oldId,
      oldPhaseId: oldId,
      newPhaseId: newId,
      fromChange: true,
    };
    selectedLessonPair = null;
    syncLessonPhaseRowActive();
    if (xiaokeOverlayLayer !== 'phase') {
      setXiaokeOverlayLayer('phase', { keepSelection: true });
    } else {
      paintOverlays();
    }
    scrollLessonPdfPageIntoView('old', oldPage);
    scrollLessonPdfPageIntoView('new', newPage);
  }

  function selectLessonPhaseRow(row) {
    if (!row) return;
    const side = row.dataset.side === 'new' ? 'new' : 'old';
    const phaseId = row.dataset.phaseId || '';
    const already = selectedLessonPhase
      && selectedLessonPhase.side === side
      && selectedLessonPhase.phaseId === phaseId;
    if (already) {
      selectedLessonPhase = null;
      syncLessonPhaseRowActive();
      paintOverlays();
      return;
    }
    selectedLessonPhase = { side, phaseId };
    selectedLessonPair = null;
    syncLessonPhaseRowActive();
    if (xiaokeOverlayLayer !== 'phase') {
      setXiaokeOverlayLayer('phase', { keepSelection: true });
    } else {
      paintOverlays();
    }
    const pdfPage = Number(row.dataset.pdfPage || 0) || null;
    scrollLessonPdfPageIntoView(side, pdfPage);
  }

  function syncLessonPhaseRowActive() {
    const rowsEl = $('#lesson-phase-rows');
    if (!rowsEl) return;
    rowsEl.querySelectorAll('.diff-phase-item').forEach((row) => {
      const side = row.dataset.side === 'new' ? 'new' : 'old';
      const pid = row.dataset.phaseId || '';
      let match = false;
      if (selectedLessonPhase) {
        if (selectedLessonPhase.fromChange) {
          match = (side === 'old' && pid && pid === (selectedLessonPhase.oldPhaseId || ''))
            || (side === 'new' && pid && pid === (selectedLessonPhase.newPhaseId || ''));
        } else {
          match = row.dataset.side === selectedLessonPhase.side
            && pid === (selectedLessonPhase.phaseId || '');
        }
      }
      row.classList.toggle('is-active', match);
    });
    rowsEl.querySelectorAll('.diff-phase-change').forEach((row) => {
      const match = !!(selectedLessonPhase
        && selectedLessonPhase.fromChange
        && (row.dataset.oldPhaseId || '') === (selectedLessonPhase.oldPhaseId || '')
        && (row.dataset.newPhaseId || '') === (selectedLessonPhase.newPhaseId || ''));
      row.classList.toggle('is-active', match);
    });
  }

  /** 构建整课文字/图片块比对行 HTML（head + body），供课对与环节面板切换复用。 */
  function buildLessonCompareRowsHtml(result, image = false) {
    if (!result) return '';
    const rows = image ? (result.image_rows || []) : (result.atom_rows || []);
    if (!rows.length) {
      return image
        ? '<p class="diff-block-empty-hint">无插图比对结果（请先完成整课一键中的图片 OCR）。</p>'
        : '<p class="diff-block-empty-hint">无文字块比对结果。</p>';
    }
    const changed = rows.filter((r) => !isSoftSameChange(r.change)).length;
    const head = changed
      ? `<p class="diff-block-pairs-banner">${image ? '图片块比对' : '文字块比对'} · 有 <strong>${changed}</strong> / ${rows.length} 块有变化</p>`
      : `<p class="diff-block-pairs-banner is-same">${image ? '图片块比对' : '文字块比对'} · ${rows.length} 块均【没变化】</p>`;
    const body = rows.map((row, idx) => {
      const label = changeLabel(row.change);
      const isSame = isSoftSameChange(row.change);
      const diffKind = lessonRowDiffKind(row);
      const pageHint = [
        row.old_pdf_page != null ? `旧 p${row.old_pdf_page}` : '',
        row.new_pdf_page != null ? `新 p${row.new_pdf_page}` : '',
      ].filter(Boolean).join(' ↔ ');
      const cls = isSame ? 'is-same' : `is-text is-diff-${diffKind}`;
      const oldLocal = row.old_pdf_page != null
        ? (bundleLocalAtomId(row.old_atom_id, row.old_pdf_page) || '')
        : '';
      const newLocal = row.new_pdf_page != null
        ? (bundleLocalAtomId(row.new_atom_id, row.new_pdf_page) || '')
        : '';
      const notes = isSame ? ['没变化'] : describeLessonBlockChange(row);
      const noteHtml = notes.map((n) => formatNoteLineHtml(n)).join('');
      return `<div class="diff-lesson-atom-row ${cls}" role="button" tabindex="0" data-pair-idx="${idx}"`
        + ` data-diff-kind="${esc(diffKind)}" data-row-kind="${image ? 'image' : 'text'}"`
        + (row.old_pdf_page != null ? ` data-old-page="${Number(row.old_pdf_page)}"` : '')
        + (row.new_pdf_page != null ? ` data-new-page="${Number(row.new_pdf_page)}"` : '')
        + (oldLocal ? ` data-old-id="${esc(oldLocal)}"` : '')
        + (newLocal ? ` data-new-id="${esc(newLocal)}"` : '')
        + `>`
        + `<div class="diff-lesson-atom-meta"><span class="diff-kind-badge is-diff-${diffKind}">${esc(label)}</span>`
        + (pageHint ? `<span class="diff-lesson-pages">${esc(pageHint)}</span>` : '')
        + `</div>`
        + `<div class="diff-lesson-atom-grid">`
        + `<div class="diff-lesson-atom-col"><div class="diff-lesson-col-h">旧</div><div class="old-t">${esc(row.old_text || '—')}</div></div>`
        + `<div class="diff-lesson-atom-col"><div class="diff-lesson-col-h">新</div><div class="new-t">${esc(row.new_text || '—')}</div></div>`
        + `<div class="diff-lesson-atom-col diff-lesson-atom-col--notes"><div class="diff-lesson-col-h">变化情况</div><div class="diff-lesson-notes">${noteHtml}</div></div>`
        + `</div></div>`;
    }).join('');
    return head + body;
  }

  function renderLessonCompare(result) {
    if (isXiaokeLessonMode()) {
      renderLessonPhases(result);
      return;
    }
    const panel = $('#lesson-compare-panel');
    const sum = $('#lesson-compare-summary');
    const rowsEl = $('#lesson-atom-rows');
    if (!panel || !result) return;
    hideAtomCatalog();
    const title = $('#panel-compare-title');
    if (title) title.textContent = '整课比对';
    const showImage = false;
    const ov = result.text_overview || {};
    const atom = result.atom_summary || {};
    const imgSum = result.image_summary || {};
    const verdict = showImage
      ? (imgSum.verdict || atom.image_verdict || '—')
      : (ov.verdict || atom.verdict || '—');
    const sim = ov.similarity != null ? ov.similarity : atom.similarity;
    if (sum) {
      sum.hidden = false;
      if (showImage) {
        sum.innerHTML = `<p class="diff-compare-verdict">整课图片 · ${esc(String(verdict))}</p>`
          + `<p class="diff-compare-meta">差异块 ${imgSum.changed_blocks ?? atom.image_changed_blocks ?? 0}`
          + `/${imgSum.block_count ?? atom.image_block_count ?? 0}`
          + ` · 旧图 ${imgSum.old_image_count ?? '?'} / 新图 ${imgSum.new_image_count ?? '?'}`
          + (imgSum.match_source || atom.image_match_source
            ? ` · ${(imgSum.match_source || atom.image_match_source) === 'llm' ? 'LLM匹配' : '启发式'}`
            : '')
          + `</p>`;
      } else {
        sum.innerHTML = `<p class="diff-compare-verdict">整课文字 · ${esc(String(verdict))}`
          + (sim != null ? ` · 相似度 ${Number(sim).toFixed(2)}` : '')
          + `</p><p class="diff-compare-meta">旧 ${atom.old_page_count || '?'} 页 / 新 ${atom.new_page_count || '?'} 页`
          + ` · 差异块 ${atom.changed_blocks || 0}/${atom.block_count || 0}`
          + (atom.match_source || result.match_source
            ? ` · ${atom.match_source === 'llm' || result.match_source === 'llm' ? 'LLM匹配' : '启发式'}`
            : '')
          + (atom.image_block_count != null
            ? ` · 图片块 ${atom.image_changed_blocks || 0}/${atom.image_block_count}`
            : '')
          + `</p>`;
      }
    }
    if (rowsEl) {
      rowsEl.innerHTML = buildLessonCompareRowsHtml(result, showImage);
      rowsEl.querySelectorAll('.diff-lesson-atom-row').forEach((row) => {
        row.addEventListener('click', () => selectLessonCompareRow(row));
        row.addEventListener('keydown', (ev) => {
          if (ev.key !== 'Enter' && ev.key !== ' ') return;
          ev.preventDefault();
          selectLessonCompareRow(row);
        });
      });
      syncLessonCompareRowActive();
      rowsEl.hidden = false;
    }
    panel.hidden = false;
    paintOverlays();
  }

  function selectLessonCompareRow(row) {
    if (!row) return;
    const already = row.classList.contains('is-active');
    if (already) {
      selectedLessonPair = null;
      syncLessonCompareRowActive();
      paintOverlays();
      return;
    }
    const oldPage = Number(row.dataset.oldPage || 0) || null;
    const newPage = Number(row.dataset.newPage || 0) || null;
    selectedLessonPair = {
      oldId: row.dataset.oldId || null,
      newId: row.dataset.newId || null,
      oldPage,
      newPage,
    };
    syncLessonCompareRowActive();
    const wantLayer = row.dataset.rowKind === 'image' ? 'image-compare' : 'compare';
    if (xiaokeOverlayLayer !== wantLayer) {
      setXiaokeOverlayLayer(wantLayer, { keepSelection: true });
    } else {
      paintOverlays();
    }
    scrollLessonPdfPageIntoView('old', oldPage);
    scrollLessonPdfPageIntoView('new', newPage);
  }

  function syncLessonCompareRowActive() {
    const rowsEl = $('#lesson-atom-rows');
    if (!rowsEl) return;
    rowsEl.querySelectorAll('.diff-lesson-atom-row').forEach((row) => {
      const match = !!(selectedLessonPair
        && (row.dataset.oldId || '') === (selectedLessonPair.oldId || '')
        && (row.dataset.newId || '') === (selectedLessonPair.newId || '')
        && Number(row.dataset.oldPage || 0) === Number(selectedLessonPair.oldPage || 0)
        && Number(row.dataset.newPage || 0) === Number(selectedLessonPair.newPage || 0));
      row.classList.toggle('is-active', match);
    });
  }

  function scrollLessonPdfPageIntoView(side, pdfPage) {
    if (!pdfPage) return;
    const listId = side === 'old' ? '#old-pages-list' : '#new-pages-list';
    const root = $(listId);
    if (!root) return;
    const card = Array.from(root.querySelectorAll('.diff-view-page-card')).find((c) => {
      const pages = pageUrlsForIndex(c.dataset.pageIndex);
      if (!pages) return false;
      const p = side === 'old' ? pages.old_page : pages.new_page;
      return Number(p) === Number(pdfPage);
    });
    card?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }

  function renderTextBlockPairs(compare) {
    const wrap = $('#text-block-pairs');
    if (!wrap) return;
    // 比对三列表出现时，务必关掉旧的左右原子双列（避免两套结果叠在一起）
    hideAtomCatalog();
    const allRows = normalizeBlockRows(compare);
    if (!allRows.length) {
      wrap.hidden = true;
      wrap.innerHTML = '';
      return;
    }
    // 保持教材阅读顺序，不把有差异块提前
    const rows = allRows;
    const changedCount = allRows.filter((r) => !isSoftSameChange(r.change)).length;
    wrap.hidden = false;
    const head = changedCount
      ? `<p class="diff-block-pairs-banner">按教材顺序 · 有 <strong>${changedCount}</strong> / ${allRows.length} 块有变化（橙色行 ↔ 页图红框）</p>`
      : `<p class="diff-block-pairs-banner is-same">按教材顺序 · 本页 ${allRows.length} 块均【没变化】</p>`;

    const tableRows = rows.map((row, idx) => {
      const label = changeLabel(row.change);
      const isSame = isSoftSameChange(row.change);
      const changeCls = label === '没变化'
        ? 'is-same'
        : (label === '分页错位' || label === '仅标点差异'
          ? 'is-punct'
          : (label === '音标变动注意' ? 'is-pinyin' : 'is-text'));
      const oldId = row.old_atom_id || '';
      const newId = row.new_atom_id || '';
      let oldMarks = [];
      let newMarks = [];
      if (!isSame && (row.ops || []).length) {
        ({ oldMarks, newMarks } = buildPairMarks(row.old_text || '', row.new_text || '', row.ops || []));
      } else if (!isSame && !(row.ops || []).length && row.old_text !== row.new_text) {
        // 无 ops 时整段提示有差异
        oldMarks = row.old_text ? [{ start: 0, end: row.old_text.length, mode: 'del' }] : [];
        newMarks = row.new_text ? [{ start: 0, end: row.new_text.length, mode: 'ins' }] : [];
      }
      const roleCls = row.block_role === 'reading_list'
        ? ' diff-block-text--pairs'
        : (row.block_role === 'writing_grid' ? ' diff-block-text--grid' : '');
      const notes = label === '没变化'
        ? ['没变化']
        : (label === '分页错位'
          ? (Array.isArray(row.change_summary_lines) && row.change_summary_lines.length
            ? row.change_summary_lines
            : (row.change_summary
              ? String(row.change_summary).split(/[；;]/).map((s) => s.trim()).filter(Boolean)
              : ['版式分页不同，非正文改写']))
          : describeBlockChange(row));
      const noteHtml = notes.map((n) => formatNoteLineHtml(n)).join('');
      return `<tr class="diff-table-row ${isSame ? 'is-same-pair' : 'is-changed'}" data-pair-idx="${idx}"
          data-old-id="${esc(oldId)}" data-new-id="${esc(newId)}">
        <td class="diff-table-cell diff-table-cell--old">
          <div class="diff-table-id">
            ${isSame ? '' : '<span class="diff-table-flag" title="页图红框对应块">变</span>'}
            ${esc(oldId || '—')}
          </div>
          <div class="diff-block-text${roleCls}">${renderMarkedText(row.old_text, oldMarks)}</div>
        </td>
        <td class="diff-table-cell diff-table-cell--new">
          <div class="diff-table-id">${esc(newId || '—')}</div>
          <div class="diff-block-text${roleCls}">${renderMarkedText(row.new_text, newMarks)}</div>
        </td>
        <td class="diff-table-cell diff-table-cell--note">
          <div class="diff-block-pair-change ${changeCls}">【${esc(label)}】</div>
          ${label === '没变化' ? '' : noteHtml}
        </td>
      </tr>`;
    }).join('');

    wrap.innerHTML = `${head}
      <table class="diff-pair-table">
        <thead>
          <tr>
            <th>旧教材原子</th>
            <th>新教材原子</th>
            <th>变化说明</th>
          </tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>`;

    updateTextCompareSummary(compare, changedCount, allRows.length);

    wrap.querySelectorAll('.diff-table-row').forEach((row) => {
      row.addEventListener('click', () => {
        wrap.querySelectorAll('.diff-table-row').forEach((r) => r.classList.remove('is-active'));
        row.classList.add('is-active');
        if (row.dataset.oldId) focusAtom('old', row.dataset.oldId);
        if (row.dataset.newId) focusAtom('new', row.dataset.newId);
      });
    });
    renderMath(wrap);
    syncBlockTextHeights(wrap);
  }

  /** 同行旧/新侧 .diff-block-text 高度统一为最大值 */
  function syncBlockTextHeights(wrap) {
    if (!wrap) return;
    wrap.querySelectorAll('tr.diff-table-row').forEach((tr) => {
      const cells = tr.querySelectorAll('.diff-block-text');
      if (cells.length < 2) return;
      // 先清除高度限制
      cells.forEach((c) => { c.style.height = ''; c.style.maxHeight = 'none'; });
      // 取最大值
      let maxH = 0;
      cells.forEach((c) => { maxH = Math.max(maxH, c.scrollHeight); });
      cells.forEach((c) => { c.style.height = maxH + 'px'; });
    });
  }

  function verdictClass(verdict, { same = [], punct = [] } = {}) {
    if (same.includes(verdict)) return 'is-same';
    if (punct.includes(verdict)) return 'is-punct';
    return 'is-text';
  }

  function updateTextCompareSummary(compare, changedCount, totalCount) {
    const el = $('#text-compare-summary');
    if (!el || !compare) return;
    const verdict = compare.verdict || '';
    const s = compare.summary || {};
    const verdictCls = verdict === '音标变动注意'
      ? 'is-pinyin'
      : verdictClass(verdict, { same: ['一致', '无文字'], punct: ['仅标点差异'] });
    el.hidden = false;
    el.innerHTML = `
      <span class="diff-compare-verdict ${verdictCls}">② ${esc(verdict)}</span>
      <span class="diff-compare-meta">相似度 ${Number(s.similarity ?? compare.similarity ?? 0).toFixed(3)}
        · 有差异块 ${changedCount ?? s.changed_blocks ?? 0}/${totalCount ?? s.block_count ?? 0}</span>`;
  }

  function normalizeImageBlockRows(compare) {
    const layout = compare?.layout || {};
    const capsByKey = {};
    (compare?.captions?.rows || []).forEach((r) => {
      capsByKey[`${r.old_atom_id}|${r.new_atom_id}`] = r;
    });
    const visByKey = {};
    (compare?.visuals?.rows || []).forEach((r) => {
      visByKey[`${r.old_atom_id}|${r.new_atom_id}`] = r;
    });
    const rows = [];
    (layout.pairs || []).forEach((p) => {
      const key = `${p.old_atom_id}|${p.new_atom_id}`;
      const cap = capsByKey[key] || {};
      const vis = visByKey[key] || {};
      rows.push({
        kind: 'pair',
        old_atom_id: p.old_atom_id,
        new_atom_id: p.new_atom_id,
        layout_change: p.change || '',
        anchor_score: p.anchor_score,
        old_theme: cap.old_theme || cap.old_label || '',
        new_theme: cap.new_theme || cap.new_label || '',
        theme_change: cap.theme_change || '',
        old_matched_text: cap.old_matched_text || '',
        new_matched_text: cap.new_matched_text || '',
        old_caption: cap.old_caption || '',
        new_caption: cap.new_caption || '',
        caption_change: cap.caption_change || cap.change || '',
        visual_grade: vis.grade || '',
        visual_similarity: vis.similarity ?? null,
        llm_summary: vis.llm_summary || '',
      });
    });
    (layout.unmatched_old || []).forEach((u) => {
      rows.push({
        kind: 'old_only',
        old_atom_id: u.atom_id,
        new_atom_id: null,
        layout_change: '旧有新无',
        old_theme: '',
        new_theme: '',
        theme_change: '',
        old_caption: '',
        new_caption: '',
        caption_change: '',
        visual_grade: '',
        visual_similarity: null,
      });
    });
    (layout.unmatched_new || []).forEach((u) => {
      rows.push({
        kind: 'new_only',
        old_atom_id: null,
        new_atom_id: u.atom_id,
        layout_change: '新有旧无',
        old_theme: '',
        new_theme: '',
        theme_change: '',
        old_caption: '',
        new_caption: '',
        caption_change: '',
        visual_grade: '',
        visual_similarity: null,
      });
    });
    return rows;
  }

  function imageChangeLabel(row) {
    if (row.kind === 'old_only') return '旧有新无';
    if (row.kind === 'new_only') return '新有旧无';
    const issues = [];
    if (row.layout_change && row.layout_change !== '版面对齐') issues.push(row.layout_change);
    if (row.theme_change === '主题改写') issues.push('主题改写');
    const cc = row.caption_change;
    if (cc === '图注改写') issues.push('图注改写');
    // 兼容旧 API
    if (!row.theme_change && cc && !['主题一致', '图注一致', '无图注', '两侧无说明', '说明一致'].includes(cc)
      && !String(cc).includes('一致')) {
      if (cc === '主题改写' || cc === '图注改写' || String(cc).includes('差异') || String(cc).includes('改写')) {
        issues.push(cc);
      }
    }
    if (row.visual_grade && row.visual_grade !== '画面接近') issues.push(row.visual_grade);
    if (!issues.length) return '没变化';
    return issues[0];
  }

  function describeImageBlockChange(row) {
    if (row.kind === 'old_only') return ['旧教材有此插图，新教材无对应块'];
    if (row.kind === 'new_only') return ['新教材新增插图，旧教材无对应块'];
    const lines = [`A 版面：${row.layout_change || '—'}`];
    const themeOld = row.old_theme || '';
    const themeNew = row.new_theme || '';
    if (themeOld || themeNew || row.theme_change) {
      let c1 = `C1 图义：${row.theme_change || '—'}（${themeOld || '—'}${themeOld !== themeNew ? ` → ${themeNew || '—'}` : ''}）`;
      lines.push(c1);
      const mOld = (row.old_matched_text || '').trim();
      const mNew = (row.new_matched_text || '').trim();
      if (mOld || mNew) {
        if (mOld && mNew && mOld === mNew) {
          lines.push(`  对应课文：${mOld.length > 36 ? `${mOld.slice(0, 36)}…` : mOld}`);
        } else {
          if (mOld) lines.push(`  旧对应：${mOld.length > 36 ? `${mOld.slice(0, 36)}…` : mOld}`);
          if (mNew) lines.push(`  新对应：${mNew.length > 36 ? `${mNew.slice(0, 36)}…` : mNew}`);
        }
      }
    }
    const cc = row.caption_change || '';
    if (cc && cc !== '无图注') {
      if (cc === '图注一致' || cc === '旧无图注' || cc === '新无图注') {
        lines.push(`C2 图注：${cc}`);
      } else {
        lines.push(`C2 图注：${cc}`);
        if (row.old_caption) lines.push(`  旧图注：${row.old_caption}`);
        if (row.new_caption) lines.push(`  新图注：${row.new_caption}`);
      }
    } else {
      lines.push('C2 图注：无图注');
    }
    if (row.visual_similarity != null) {
      lines.push(`B 画面：${row.visual_grade || '—'}（${Number(row.visual_similarity).toFixed(3)}）`);
    }
    if (row.llm_summary) lines.push(row.llm_summary);
    return lines;
  }

  function renderImageCell(atomId, theme, caption) {
    if (!atomId) return '<span class="diff-block-empty">—</span>';
    const name = (theme || '').trim();
    const cap = (caption || '').trim();
    return `<div class="diff-table-id">${esc(atomId)}</div>
      <div class="diff-block-text diff-block-text--image">${esc(name || '插图')}</div>
      ${cap ? `<div class="diff-image-caption">图注：${esc(cap)}</div>` : ''}`;
  }

  function renderImageBlockPairs(compare) {
    const wrap = $('#image-block-pairs');
    if (!wrap) return;
    hideAtomCatalog();
    const allRows = normalizeImageBlockRows(compare);
    if (!allRows.length) {
      wrap.hidden = true;
      wrap.innerHTML = '';
      const sum = $('#image-compare-summary');
      if (sum) {
        sum.hidden = false;
        sum.innerHTML = `<span class="diff-compare-verdict is-same">④ ${esc(compare?.verdict || '无插图')}</span>`;
      }
      return;
    }
    const changedCount = allRows.filter((r) => imageChangeLabel(r) !== '没变化').length;
    wrap.hidden = false;
    const head = changedCount
      ? `<p class="diff-block-pairs-banner diff-block-pairs-banner--image">按版面顺序 · 有 <strong>${changedCount}</strong> / ${allRows.length} 对插图有变化（紫色框 ↔ 页图）</p>`
      : `<p class="diff-block-pairs-banner diff-block-pairs-banner--image is-same">按版面顺序 · ${allRows.length} 对插图均【没变化】</p>`;

    const tableRows = allRows.map((row, idx) => {
      const label = imageChangeLabel(row);
      const isSame = label === '没变化';
      const changeCls = isSame ? 'is-same' : 'is-text';
      const notes = isSame ? ['没变化'] : describeImageBlockChange(row);
      const noteHtml = notes.map((n) => `<div class="diff-table-note-line">${esc(n)}</div>`).join('');
      const oldId = row.old_atom_id || '';
      const newId = row.new_atom_id || '';
      return `<tr class="diff-table-row ${isSame ? 'is-same-pair' : 'is-changed is-image-changed'}" data-pair-idx="${idx}"
          data-old-id="${esc(oldId)}" data-new-id="${esc(newId)}">
        <td class="diff-table-cell diff-table-cell--old">
          ${renderImageCell(oldId, row.old_theme, row.old_caption)}
        </td>
        <td class="diff-table-cell diff-table-cell--new">
          ${renderImageCell(newId, row.new_theme, row.new_caption)}
        </td>
        <td class="diff-table-cell diff-table-cell--note">
          <div class="diff-block-pair-change ${changeCls}">【${esc(label)}】</div>
          ${isSame ? '' : noteHtml}
        </td>
      </tr>`;
    }).join('');

    wrap.innerHTML = `${head}
      <table class="diff-pair-table diff-pair-table--image">
        <thead>
          <tr>
            <th>旧教材插图</th>
            <th>新教材插图</th>
            <th>变化说明</th>
          </tr>
        </thead>
        <tbody>${tableRows}</tbody>
      </table>`;

    updateImageCompareSummary(compare, changedCount, allRows.length);

    wrap.querySelectorAll('.diff-table-row').forEach((row) => {
      row.addEventListener('click', () => {
        wrap.querySelectorAll('.diff-table-row').forEach((r) => r.classList.remove('is-active'));
        row.classList.add('is-active');
        if (row.dataset.oldId) focusAtom('old', row.dataset.oldId);
        if (row.dataset.newId) focusAtom('new', row.dataset.newId);
      });
    });
  }

  function updateImageCompareSummary(compare, changedCount, totalCount) {
    const el = $('#image-compare-summary');
    if (!el || !compare) return;
    const layout = compare.layout || {};
    const caps = compare.captions || {};
    const visuals = compare.visuals || {};
    const ls = layout.summary || {};
    const verdict = compare.verdict || '';
    const verdictCls = verdictClass(verdict, { same: ['插图一致', '无插图'] });
    el.hidden = false;
    el.innerHTML = `
      <span class="diff-compare-verdict ${verdictCls}">④ ${esc(verdict)}</span>
      <span class="diff-compare-meta">旧图 ${ls.old_image_count || 0} · 新图 ${ls.new_image_count || 0}
        · 锚定 ${ls.paired || 0} · A ${esc(layout.verdict || '')} · C ${esc(caps.verdict || '')} · B ${esc(visuals.verdict || '')}
        · 有变化 ${changedCount ?? 0}/${totalCount ?? 0}</span>`;
  }

  function applyTextCompareHighlight(compare) {
    const oldIds = new Set();
    const newIds = new Set();
    (compare?.changed_anchors || []).forEach((a) => {
      if (a.old_atom_id) oldIds.add(a.old_atom_id);
      if (a.new_atom_id) newIds.add(a.new_atom_id);
    });
    (compare?.unmatched_old || []).forEach((a) => {
      if (a.atom_id) oldIds.add(a.atom_id);
    });
    (compare?.unmatched_new || []).forEach((a) => {
      if (a.atom_id) newIds.add(a.atom_id);
    });
    changedAtomIds = { old: oldIds, new: newIds };
  }

  function applyImageCompareHighlight(compare) {
    const oldIds = new Set();
    const newIds = new Set();
    const layout = compare?.layout || {};
    (layout.unmatched_old || []).forEach((a) => { if (a.atom_id) oldIds.add(a.atom_id); });
    (layout.unmatched_new || []).forEach((a) => { if (a.atom_id) newIds.add(a.atom_id); });
    (compare?.visuals?.rows || []).forEach((r) => {
      if (r.grade && r.grade !== '画面接近') {
        if (r.old_atom_id) oldIds.add(r.old_atom_id);
        if (r.new_atom_id) newIds.add(r.new_atom_id);
      }
    });
    (compare?.captions?.changed || []).forEach((r) => {
      if (r.old_atom_id) oldIds.add(r.old_atom_id);
      if (r.new_atom_id) newIds.add(r.new_atom_id);
    });
    (layout.pairs || []).forEach((p) => {
      if (p.change === '位置偏移') {
        if (p.old_atom_id) oldIds.add(p.old_atom_id);
        if (p.new_atom_id) newIds.add(p.new_atom_id);
      }
    });
    imageChangedAtomIds = { old: oldIds, new: newIds };
  }

  function showPageImagesImmediately() {
    bindPageListClicks();
  }

  async function loadWorkspace() {
    showPageImagesImmediately();
    setAtomStatus('加载页信息…');
    const res = await fetch(`/api/textbook-diff/view/workspace?${workspaceQuery({ include_atoms: '1' })}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '加载失败');
    state = data;
    renderWorkspace();
    prefetchAdjacentPageImages();
    // 优先从磁盘缓存恢复比对结果；无缓存再重算（OCR 本身已缓存，不必重跑）
    // 小科整课模式：只走整课合成比对，不自动跑本页 ②/④
    if (!isXiaokeLessonMode()) {
      const atoms = state?.atoms;
      if (atoms?.text_compare) {
        textCompareState = atoms.text_compare;
        applyTextCompareHighlight(textCompareState);
      } else if (atoms?.old_text_ocr_done && atoms?.new_text_ocr_done) {
        await runTextCompare({ skipBusyGuard: true }).catch((e) => showToast(String(e)));
      }
      if (atoms?.image_compare) {
        imageCompareState = atoms.image_compare;
        applyImageCompareHighlight(imageCompareState);
      } else if (atoms?.old_image_ocr_done && atoms?.new_image_ocr_done) {
        await runImageCompare({ skipBusyGuard: true }).catch((e) => showToast(String(e)));
      }
      if (textCompareState || imageCompareState) {
        refreshResultPanels();
        paintOverlays();
      }
    }
    if (isXiaokeLessonMode()) {
      await loadXiaokeLessonCompareResult({ activate: true }).catch(() => {});
      if (!lessonCompareState) showXiaokePhaseView();
      paintOverlays();
    }
  }

  function renderWorkspace() {
    if (!state) return;
    $('#view-title').textContent = state.title || '页面对照';
    $('#view-meta').textContent = [
      state.old_volume?.display_title,
      '↔',
      state.new_volume?.display_title,
      state.match_method ? `· ${matchMethodZh(state.match_method)}` : '',
    ].filter(Boolean).join(' ');
    $('#old-vol-label').textContent = state.old_volume?.display_title || '';
    $('#new-vol-label').textContent = state.new_volume?.display_title || '';
    $('#old-lesson-label').textContent = formatLesson(state.old_lesson) || '\u00a0';
    $('#new-lesson-label').textContent = formatLesson(state.new_lesson)
      || (state.mode === 'page'
        ? (isDraftNew() ? `修订预览 · PDF p${state.new_page}` : `PDF p${state.new_page}`)
        : '\u00a0');

    renderPageLists();
    renderAtomsPanel();
    paintOverlays();
    applyBackLinks();
    syncPairStatusChrome();
    syncXiaokeLessonUi();
    syncLessonPipelineBtn();
  }

  function matchMethodZh(m) {
    const map = {
      title_anchor: '课名匹配',
      lesson_no: '课号匹配',
      index: '顺序对齐',
      manual: '手工改对',
      name_exact: '完全匹配',
      name_similar: '相似匹配',
      name_edition: '跨年级匹配',
      name_llm: '大模型匹配',
    };
    return map[m] || m || '';
  }

  function formatLesson(les) {
    if (!les) return '';
    const label = les.lesson_label
      || [les.lesson_no, les.lesson_name].filter(Boolean).join(' ').trim()
      || les.lesson_name
      || les.lesson_no
      || '';
    const parts = [les.unit_title, label].filter(Boolean);
    if (les.page_start) parts.push(`p${les.page_start}–${les.page_end || les.page_start}`);
    return parts.join(' · ');
  }

  function sideStatusLine(atoms, side) {
    const textDone = sideTextDone(atoms, side);
    const imageDone = side === 'old' ? !!atoms?.old_image_ocr_done : !!atoms?.new_image_ocr_done;
    const count = side === 'old'
      ? (atoms?.summary?.old_atom_count || 0)
      : (atoms?.summary?.new_atom_count || 0);
    const parts = [];
    if (textDone) parts.push('文字已 OCR');
    else parts.push('待文字 OCR');
    if (imageDone) parts.push('图片已 OCR');
    else if (textDone) parts.push('可图片 OCR');
    if (count) parts.push(`${count} 个原子`);
    return `${sideLabel(side)}：${parts.join(' · ')}`;
  }

  function atomStatusHint(atoms) {
    if (!atoms) return state?.ocr_hint || '点顶部 ① 文字 OCR 或 ③ 图片 OCR（单页分轨）';
    return [sideStatusLine(atoms, 'old'), sideStatusLine(atoms, 'new')].join(' | ');
  }

  function renderAtomsPanel() {
    const atoms = state.atoms || null;
    if (!atoms || (
      !atoms.old_atoms?.length
      && !atoms.new_atoms?.length
      && !atoms.old_text_ocr_done
      && !atoms.new_text_ocr_done
    )) {
      $('#atom-summary').innerHTML = '';
      setAtomStatus(atomStatusHint(atoms));
      $('#atom-catalog-wrap').hidden = true;
      return;
    }
    const s = atoms.summary || {};
    $('#atom-summary').innerHTML = `
      <span class="diff-atom-stat">旧 ${s.old_atom_count || 0}</span>
      <span class="diff-atom-stat">新 ${s.new_atom_count || 0}</span>
      <span class="diff-atom-stat">旧文字 ${s.old_text_atoms || 0}</span>
      <span class="diff-atom-stat">新文字 ${s.new_text_atoms || 0}</span>
      <span class="diff-atom-stat">旧图 ${s.old_image_atoms || 0}</span>
      <span class="diff-atom-stat">新图 ${s.new_image_atoms || 0}</span>`;
    setAtomStatus(atomStatusHint(atoms));

    const oldAtoms = atoms.old_atoms || [];
    const newAtoms = atoms.new_atoms || [];
    if (textCompareState || imageCompareState) {
      hideAtomCatalog();
      refreshResultPanels();
      return;
    }
    syncPanelCompareMode();
    const pairs = $('#text-block-pairs');
    if (pairs) {
      pairs.hidden = true;
      pairs.innerHTML = '';
    }
    const iPairs = $('#image-block-pairs');
    if (iPairs) {
      iPairs.hidden = true;
      iPairs.innerHTML = '';
    }
    if (oldAtoms.length || newAtoms.length) {
      showAtomCatalog();
      renderAtomCatalogColumn('old', oldAtoms);
      renderAtomCatalogColumn('new', newAtoms);
    } else {
      hideAtomCatalog();
    }
    syncPanelCompareMode();
  }

  function paintOverlays() {
    syncOverlayVisibility();
    if (isXiaokeLessonMode()) {
      paintAllLessonSide('old');
      paintAllLessonSide('new');
      return;
    }
    paintSide(activeAtomLayer('old'), activePageImg('old'), 'old');
    paintSide(activeAtomLayer('new'), activePageImg('new'), 'new');
  }

  function paintAllLessonSide(side) {
    const listId = side === 'old' ? '#old-pages-list' : '#new-pages-list';
    const root = $(listId);
    if (!root) return;
    root.querySelectorAll('.diff-view-page-card').forEach((card) => {
      const pages = pageUrlsForIndex(card.dataset.pageIndex);
      if (!pages) return;
      const pdfPage = side === 'old' ? pages.old_page : pages.new_page;
      if (!pdfPage) return;
      const layer = card.querySelector('.diff-atom-layer');
      const img = card.querySelector('img.diff-view-page-img');
      const atoms = lessonAtomsByPage[side]?.[String(pdfPage)] || [];
      const phases = lessonPhasesByPage[side]?.[String(pdfPage)] || [];
      paintSide(layer, img, side, { atoms, phases, pdfPage });
    });
  }

  function overlayAtomIdsForSide(side) {
    if (isXiaokeLessonMode()) return null;
    if (!(textCompareState || imageCompareState)) return null;
    const ids = new Set();
    if (activeResultTab === 'text' && textCompareState) {
      normalizeBlockRows(textCompareState).forEach((row) => {
        const id = side === 'old' ? row.old_atom_id : row.new_atom_id;
        if (id) ids.add(id);
      });
      return ids;
    }
    if (activeResultTab === 'image' && imageCompareState) {
      normalizeImageBlockRows(imageCompareState).forEach((row) => {
        const id = side === 'old' ? row.old_atom_id : row.new_atom_id;
        if (id) ids.add(id);
      });
      return ids;
    }
    return ids;
  }

  function atomMeaningfulForOverlay(a, filterMode) {
    const t = (a.display_text || a.ocr_text || a.content || '').trim();
    if (a.atom_type === 'image') {
      const bb = a.bbox || {};
      const w = Math.max(0, (bb.x_end || 0) - (bb.x_start || 0));
      const h = Math.max(0, (bb.y_end || 0) - (bb.y_start || 0));
      if (w * h < 0.004) return false;
      const aspect = w / Math.max(1e-6, h);
      if (aspect >= 6 && h < 0.10) return false;
      if ((bb.y_start || 0) >= 0.86 && w >= 0.30 && h <= 0.14) return false;
      return true;
    }
    if (!t || t.startsWith('[未拆分') || t.startsWith('[整页未识别')) return false;
    if (filterMode === 'image') return false;
    return true;
  }

  function overlayAtomsForSide(side, overrideAtoms) {
    if (Array.isArray(overrideAtoms)) return overrideAtoms;
    /** OCR 原子 + 文字比对一对多虚拟切片（#m / #p），否则表格有行但页图无框。 */
    const base = side === 'old' ? (state?.atoms?.old_atoms || []) : (state?.atoms?.new_atoms || []);
    const byId = new Map();
    base.forEach((a) => {
      if (a?.atom_id) byId.set(a.atom_id, a);
    });
    const injectFromAnchors = (anchors) => {
      (anchors || []).forEach((anc) => {
        const id = side === 'old' ? anc.old_atom_id : anc.new_atom_id;
        const bb = side === 'old' ? anc.old_bbox : anc.new_bbox;
        if (!id || byId.has(id) || !bb) return;
        if (!String(id).includes('#')) return;
        const text = side === 'old' ? (anc.old_text || '') : (anc.new_text || '');
        byId.set(id, {
          atom_id: id,
          atom_type: 'text',
          bbox: {
            x_start: bb.x_start,
            y_start: bb.y_start,
            x_end: bb.x_end,
            y_end: bb.y_end,
          },
          display_text: text,
          ocr_text: text,
          content: text,
          synthetic: true,
        });
      });
    };
    if (textCompareState) injectFromAnchors(textCompareState.anchors);
    if (imageCompareState) injectFromAnchors(imageCompareState.anchors);
    return Array.from(byId.values());
  }

  function paintSide(layer, img, side, opts = {}) {
    if (!layer || !img) return;
    layer.innerHTML = '';
    if (!img.complete || !img.naturalWidth) return;
    const xkLesson = isXiaokeLessonMode();
    if (!xkLesson && !state?.atoms) return;
    const pdfPage = opts.pdfPage != null
      ? Number(opts.pdfPage)
      : Number(side === 'old' ? (state?.old_page || P.old_page) : (state?.new_page || P.new_page));
    const xkLayer = xkLesson ? xiaokeOverlayLayer : null;
    if (xkLesson && xkLayer === 'phase') {
      const phases = (opts.phases != null
        ? opts.phases
        : (lessonPhasesByPage[side]?.[String(pdfPage)] || []));
      const changeMeta = phaseChangeMetaById()[side] || {};
      (phases || []).forEach((ph) => {
        if (!ph) return;
        if (!phaseBoxMatchesSelection(side, ph.phase_id)) return;
        const bb = ph.bbox;
        if (!bb) return;
        const x0 = Number(bb.x_start);
        const y0 = Number(bb.y_start);
        const x1 = Number(bb.x_end);
        const y1 = Number(bb.y_end);
        if (![x0, y0, x1, y1].every((n) => Number.isFinite(n))) return;
        const left = Math.min(x0, x1) * 100;
        const top = Math.min(y0, y1) * 100;
        const width = Math.abs(x1 - x0) * 100;
        const height = Math.abs(y1 - y0) * 100;
        if (width < 0.3 || height < 0.3) return;
        const pid = String(ph.phase_id || '').trim();
        const meta = changeMeta[pid] || { kind: 'match', size: 'small' };
        const sizeCls = (meta.kind === 'added' || meta.kind === 'removed')
          ? `is-phase-${meta.kind}`
          : `is-phase-${meta.size || 'small'}`;
        const box = document.createElement('div');
        box.className = `diff-atom-box is-phase ${sizeCls}${side === 'new' ? ' new-side' : ''}`;
        box.style.left = `${left}%`;
        box.style.top = `${top}%`;
        box.style.width = `${width}%`;
        box.style.height = `${height}%`;
        const label = String(ph.label || '').trim() || '环节';
        const sizeHint = meta.kind === 'added' ? '新增'
          : meta.kind === 'removed' ? '删除'
          : (meta.size === 'large' ? '大改' : meta.size === 'medium' ? '中改' : '小改');
        box.title = `${label} · ${sizeHint}`;
        box.dataset.phaseId = pid;
        if (selectedLessonPhase) box.classList.add('is-active');
        const tag = document.createElement('span');
        tag.className = 'diff-phase-label';
        tag.textContent = `${label} · ${sizeHint}`;
        box.appendChild(tag);
        layer.appendChild(box);
      });
      return;
    }
    const atoms = overlayAtomsForSide(side, opts.atoms);
    const filterMode = xkLesson
      ? ((xkLayer === 'image' || xkLayer === 'image-compare') ? 'image' : 'text')
      : ((textCompareState || imageCompareState) ? activeResultTab : 'all');
    const tableIds = overlayAtomIdsForSide(side);
    const lessonKinds = (xkLesson && (xkLayer === 'compare' || xkLayer === 'image-compare'))
      ? lessonDiffKindByAtomId(side, pdfPage, { image: xkLayer === 'image-compare' })
      : null;
    atoms.forEach((a) => {
      const isImage = a.atom_type === 'image';
      if (xkLesson) {
        if (xkLayer === 'text' && isImage) return;
        if (xkLayer === 'image' && !isImage) return;
        if (xkLayer === 'compare' && isImage) return;
        if (xkLayer === 'image-compare' && !isImage) return;
      } else {
        // 文字 Tab 绝不画插图；图片 Tab 绝不画文字（即使用旧缓存含杂音框）
        if (filterMode === 'text' && isImage) return;
        if (filterMode === 'image' && !isImage) return;
      }
      if (tableIds !== null) {
        if (!a.atom_id || !tableIds.has(a.atom_id)) return;
      } else if (!atomMeaningfulForOverlay(a, filterMode === 'all' ? (isImage ? 'image' : 'text') : filterMode)) {
        return;
      }
      // 无实质内容的文字框不画（空白噪点）
      if (!isImage) {
        const t = (a.display_text || a.ocr_text || a.content || '').trim();
        if (!t || t.startsWith('[未拆分') || t.startsWith('[整页未识别') || t === '[插图]') return;
      } else if (!atomMeaningfulForOverlay(a, 'image')) {
        return;
      }
      // 比对层：只画有差异的原子；若右侧选中某块则只画该块
      if (xkLayer === 'compare' || xkLayer === 'image-compare') {
        if (selectedLessonPair) {
          const wantId = side === 'old' ? selectedLessonPair.oldId : selectedLessonPair.newId;
          const wantPage = side === 'old' ? selectedLessonPair.oldPage : selectedLessonPair.newPage;
          if (!wantId || Number(pdfPage) !== Number(wantPage) || a.atom_id !== wantId) return;
        } else {
          const kind = lessonKinds?.get(a.atom_id);
          if (!kind || kind === 'same') return;
        }
      }
      const bb = a.bbox || {};
      const key = atomKey(side, a.atom_id);
      let cls = side === 'new' ? ' new-side' : '';
      if (isImage) cls += ' is-image';
      if ((xkLayer === 'compare' || xkLayer === 'image-compare') && lessonKinds) {
        const kind = lessonKinds.get(a.atom_id);
        if (kind) cls += lessonDiffBoxClass(kind);
        else if (selectedLessonPair) cls += ' is-diff-modify';
      } else if (xkLayer === 'text') {
        cls += ' is-ocr-text';
      } else if (xkLayer === 'image') {
        cls += ' is-ocr-image';
      } else if (xkLayer === 'image-compare') {
        cls += ' is-image-compare';
      } else {
        if (changedAtomIds[side]?.has(a.atom_id)) cls += ' is-changed';
        if (imageChangedAtomIds[side]?.has(a.atom_id)) cls += ' is-image-changed';
      }
      if (activeAtomKey === key) cls += ' is-active';
      const box = document.createElement('div');
      box.className = `diff-atom-box${cls}`;
      box.dataset.atomKey = key;
      box.dataset.side = side;
      box.dataset.atomId = a.atom_id || '';
      if ((xkLayer === 'compare' || xkLayer === 'image-compare') && lessonKinds?.has(a.atom_id)) {
        box.dataset.diffKind = lessonKinds.get(a.atom_id);
      }
      box.style.left = `${(bb.x_start || 0) * 100}%`;
      box.style.top = `${(bb.y_start || 0) * 100}%`;
      box.style.width = `${Math.max(1, ((bb.x_end || 0) - (bb.x_start || 0)) * 100)}%`;
      box.style.height = `${Math.max(1, ((bb.y_end || 0) - (bb.y_start || 0)) * 100)}%`;
      box.title = (a.display_text || a.ocr_text || a.atom_type || '').trim();
      box.addEventListener('click', (e) => {
        e.stopPropagation();
        focusAtom(side, a.atom_id);
      });
      layer.appendChild(box);
    });
  }

  async function runOcrPhase(side, phase, options = {}) {
    const {
      skipConfirm = false,
      skipAutoTextCompare = false,
      manageBusy = true,
      quietToast = false,
    } = options;
    const key = `${side}:${phase}`;
    if (manageBusy && ocrBusyKey) return false;
    const body = atomRequestBody();
    if (!body.old_code || !body.new_code || body.old_page < 1 || body.new_page < 1) {
      showToast('页码参数不完整');
      return false;
    }
    if (phase === 'images') {
      if (side === 'both') {
        if (!state?.atoms?.old_text_ocr_done || !state?.atoms?.new_text_ocr_done) {
          showToast('请先完成 ① 文字 OCR（两侧）');
          return false;
        }
      } else if (!sideTextDone(state?.atoms, side)) {
        showToast(`请先在${sideLabel(side)}页完成 ① 文字 OCR`);
        return false;
      }
    }
    const label = side === 'both'
      ? (phase === 'text'
        ? `① 文字 OCR（两侧 p${body.old_page}↔p${body.new_page}）`
        : `③ 图片 OCR（两侧 p${body.old_page}↔p${body.new_page}）`)
      : (phase === 'text'
        ? `${sideLabel(side)} · ① 文字 OCR（p${side === 'old' ? body.old_page : body.new_page}）`
        : `${sideLabel(side)} · ③ 图片 OCR（p${side === 'old' ? body.old_page : body.new_page}）`);
    if (!skipConfirm) {
      const confirmMsg = side === 'both'
        ? `${label}\n同步识别旧+新当前页（① 豆包文字 OCR：去印章预处理 + 忽略水印 + 气泡/便签整段成框；失败回退 RapidOCR。将刷新本页缓存）。继续？`
        : `${label}\n仅识别当前侧页面，另一侧不变。继续？`;
      if (!confirm(confirmMsg)) {
        return false;
      }
    }
    if (manageBusy) setOcrButtonsBusy(key);
    startOcrProgress(label);
    try {
      const res = await fetch('/api/textbook-diff/view/ocr', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, side, phase }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'OCR 失败');
      if (!state) state = {};
      state.atoms = data.atoms;
      if (state.old_page == null) state.old_page = body.old_page;
      if (state.new_page == null) state.new_page = body.new_page;
      if (phase === 'text') {
        textCompareState = null;
        changedAtomIds = { old: new Set(), new: new Set() };
        const tSum = $('#text-compare-summary');
        if (tSum) { tSum.hidden = true; tSum.innerHTML = ''; }
        const pairs = $('#text-block-pairs');
        if (pairs) {
          pairs.hidden = true;
          pairs.innerHTML = '';
        }
      } else {
        imageCompareState = null;
        imageChangedAtomIds = { old: new Set(), new: new Set() };
        const iSum = $('#image-compare-summary');
        if (iSum) { iSum.hidden = true; iSum.innerHTML = ''; }
        const iPairs = $('#image-block-pairs');
        if (iPairs) {
          iPairs.hidden = true;
          iPairs.innerHTML = '';
        }
      }
      const tabs = $('#result-tabs');
      if (tabs && !textCompareState && !imageCompareState) tabs.hidden = true;
      renderAtomsPanel();
      paintOverlays();
      const s = data.atoms?.summary || {};
      const countMsg = side === 'both'
        ? `旧 ${s.old_atom_count || 0} · 新 ${s.new_atom_count || 0} 个原子`
        : `${side === 'old' ? (s.old_atom_count || 0) : (s.new_atom_count || 0)} 个原子`;
      if (!quietToast) showToast(`${label} 完成 · ${countMsg}`);
      // 仅文字 OCR 完成后自动确认课对；图片 OCR 不应再弹「文字 OCR 完成」
      if (phase === 'text') {
        await markReadyForReview();
      }
      // 两侧文字 OCR 完成后自动比对（一键流水线自行调用 ②，此处跳过）
      if (
        !skipAutoTextCompare
        && phase === 'text'
        && state?.atoms?.old_text_ocr_done
        && state?.atoms?.new_text_ocr_done
      ) {
        await runTextCompare({ skipBusyGuard: true, manageBusy: false, quietToast });
      }
      return true;
    } catch (e) {
      setAtomStatus(String(e));
      showToast(String(e));
      return false;
    } finally {
      stopOcrProgress();
      if (manageBusy) setOcrButtonsBusy(null);
    }
  }

  async function runTextCompare(options = {}) {
    const { skipBusyGuard = false, manageBusy = true, quietToast = false, force = false } = options;
    if (!skipBusyGuard && ocrBusyKey) return false;
    const body = atomRequestBody();
    if (!body.old_code || !body.new_code || body.old_page < 1 || body.new_page < 1) {
      showToast('页码参数不完整');
      return false;
    }
    if (!state?.atoms?.old_text_ocr_done || !state?.atoms?.new_text_ocr_done) {
      showToast('请先完成 ① 文字 OCR（两侧）');
      return false;
    }
    if (manageBusy) setOcrButtonsBusy('text-compare');
    setAtomStatus('② 比对正文与标点…');
    try {
      const res = await fetch('/api/textbook-diff/view/text-compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, force: !!force }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || '文字比对失败');
      if (data.atoms) state.atoms = data.atoms;
      textCompareState = data.compare || null;
      applyTextCompareHighlight(textCompareState);
      activeResultTab = 'text';
      refreshResultPanels();
      renderAtomsPanel();
      paintOverlays();
      const n = textCompareState?.summary?.changed_blocks || 0;
      if (!quietToast) showToast(`② ${textCompareState?.verdict || '文字比对完成'} · ${n} 块有变化`);
      await markReadyForReview();
      return true;
    } catch (e) {
      setAtomStatus(String(e));
      showToast(String(e));
      return false;
    } finally {
      if (manageBusy) setOcrButtonsBusy(null);
    }
  }

  async function runImageCompare(options = {}) {
    const { skipBusyGuard = false, manageBusy = true, quietToast = false } = options;
    if (!skipBusyGuard && ocrBusyKey) return false;
    const body = atomRequestBody();
    if (!body.old_code || !body.new_code || body.old_page < 1 || body.new_page < 1) {
      showToast('页码参数不完整');
      return false;
    }
    if (!state?.atoms?.old_image_ocr_done || !state?.atoms?.new_image_ocr_done) {
      showToast('请先完成 ③ 图片 OCR（两侧）');
      return false;
    }
    if (manageBusy) setOcrButtonsBusy('image-compare');
    setAtomStatus('④ 比对插图版面 / 说明 / 画面…');
    try {
      const res = await fetch('/api/textbook-diff/view/image-compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || '图片比对失败');
      if (data.atoms) state.atoms = data.atoms;
      imageCompareState = data.compare || null;
      applyImageCompareHighlight(imageCompareState);
      // 刷新自动恢复时保持当前 Tab；用户手动点 ④ 再切到图片
      if (!skipBusyGuard) activeResultTab = 'image';
      refreshResultPanels();
      renderAtomsPanel();
      paintOverlays();
      if (!quietToast && !skipBusyGuard) {
        showToast(`④ ${imageCompareState?.verdict || '图片比对完成'}`);
      }
      return true;
    } catch (e) {
      setAtomStatus(String(e));
      showToast(String(e));
      return false;
    } finally {
      if (manageBusy) setOcrButtonsBusy(null);
    }
  }

  function isXiaokeView() {
    const subj = String(P.subject || state?.subject || state?.old_volume?.subject || '').trim();
    if (subj === '小科' || subj === 'xiaoke') return true;
    const code = String(P.new_code || state?.new_volume?.volume_code || '');
    return /^XK/i.test(code);
  }

  function isXiaokeLessonMode() {
    return isXiaokeView() && (P.mode || state?.mode) === 'lesson';
  }

  function setXiaokeOverlayLayer(layer, opts = {}) {
    if (isXiaokeLessonMode()) {
      showXiaokePhaseView();
      return;
    }
    if (layer !== 'text' && layer !== 'image' && layer !== 'phase'
      && layer !== 'compare' && layer !== 'image-compare') return;
    xiaokeOverlayLayer = layer;
    showAtomOverlay = true;
    const toggle = $('#toggle-atom-overlay');
    if (toggle) toggle.checked = true;
    if (lessonCompareState && layer === 'phase') {
      if (!opts.keepSelection) selectedLessonPair = null;
      renderLessonPhases(lessonCompareState);
      return;
    }
    syncOverlayVisibility();
    paintOverlays();
  }

  function syncXiaokeLessonUi() {
    const xk = isXiaokeLessonMode();
    document.body.classList.toggle('is-xiaoke-lesson', xk);
    const pageBtn = $(PAGE_ONE_CLICK_BTN);
    if (pageBtn) pageBtn.hidden = xk;
    const rail = $('#page-compare-step-rail');
    if (rail) rail.hidden = xk;
    const hint = $('#ocr-hint');
    if (hint) {
      hint.hidden = xk;
      if (!xk) {
        hint.textContent = '本页一键：①→②→③→④（有缓存跳过）· 分步可重跑';
      }
    }
    const phaseTools = $('#xiaoke-phase-tools');
    if (phaseTools) phaseTools.hidden = true;
    const phaseBtn = $('#btn-xiaoke-phase-segment');
    if (phaseBtn) phaseBtn.hidden = true; // 暂隐藏「环节整理」入口
    const toggleWrap = $('#overlay-toggle-wrap');
    if (toggleWrap) toggleWrap.hidden = xk;
    const legend = $('#atom-diff-legend');
    if (legend) legend.hidden = true;
    const phaseLegend = $('#phase-diff-legend');
    if (phaseLegend) phaseLegend.hidden = !xk;
    const title = $('#panel-compare-title');
    if (title) title.textContent = xk ? '环节对比' : '单页比对';
    const dl = $('#btn-download-compare');
    if (dl) dl.hidden = xk;
    const textTab = $('#tab-result-text');
    const imageTab = $('#tab-result-image');
    if (textTab) textTab.hidden = xk;
    if (imageTab) imageTab.hidden = xk;
    if (xk) $('#btn-xiaoke-phase-compare')?.classList.add('is-active');
    syncXiaokeOcrLessonBtn();
    syncLessonReuseSuggest();
  }

  function syncXiaokeOcrLessonBtn() {
    const btn = $(XIAOK_OCR_LESSON_BTN);
    if (!btn) return;
    const show = isXiaokeLessonMode() && !!P.new_lesson_uid;
    btn.hidden = !show;
    btn.disabled = !show || !!ocrBusyKey;
    if (show) {
      btn.classList.add('diff-view-btn-primary');
      btn.classList.remove('secondary');
    }
    if (ocrBusyKey === 'xiaoke-lesson-one-click') {
      btn.textContent = '整课一键中…';
    } else {
      btn.textContent = '整课一键';
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function pollXiaokeLessonFull(lessonUid) {
    const maxMs = 45 * 60 * 1000;
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
      const res = await fetch(
        `/api/textbook-diff/workbook/xiaoke/lesson-full/status?old_code=${encodeURIComponent(P.old_code)}`
        + `&new_code=${encodeURIComponent(P.new_code)}`
        + `&new_lesson_uid=${encodeURIComponent(lessonUid)}`
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const job = data.job;
      if (!job) {
        await sleep(1500);
        continue;
      }
      const done = Number(job.done || job.pages_done || 0);
      const total = Number(job.total || 0);
      const phase = job.phase || '';
      const phaseLabel = {
        starting: '启动',
        old_ocr: '旧侧文字识别',
        new_ocr: '新侧文字识别',
        bundle: '合成整课',
        phase: '划分环节',
        compare: '大模型匹配文字块',
        done: '完成',
        error: '失败',
      }[phase] || phase;
      const statusLine = [
        total ? `整课一键 ${done}/${total}` : '整课一键进行中…',
        phaseLabel,
        job.message || '',
      ].filter(Boolean).join(' · ');
      setAtomStatus(statusLine);
      startOcrProgress(statusLine);

      const st = String(job.status || '');
      if (st === 'running') {
        await sleep(2000);
        continue;
      }
      if (st === 'done' || st === 'done_with_errors' || st === 'error' || st === 'cancelled') {
        return job;
      }
      await sleep(2000);
    }
    throw new Error('整课一键超时，请稍后刷新查看');
  }

  async function loadXiaokeLessonCompareResult({ activate = true } = {}) {
    if (!isXiaokeView() || !P.old_code || !P.new_code || !P.new_lesson_uid) return null;
    try {
      const res = await fetch(
        `/api/textbook-diff/workbook/xiaoke/lesson-full/result?old_code=${encodeURIComponent(P.old_code)}`
        + `&new_code=${encodeURIComponent(P.new_code)}`
        + `&new_lesson_uid=${encodeURIComponent(P.new_lesson_uid)}`
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok || !data.result) {
        lessonCompareState = null;
        const lessonTab = $('#tab-result-lesson');
        if (lessonTab) lessonTab.hidden = true;
        return null;
      }
      lessonCompareState = data.result;
      ingestLessonAtomsByPage(lessonCompareState);
      syncXiaokeLessonUi();
      showXiaokePhaseView();
      if (activate) setResultTab('phase');
      else refreshResultPanels();
      paintOverlays();
      return data.result;
    } catch (_) {
      return null;
    }
  }

  async function runXiaokeLessonOneClick() {
    if (!isXiaokeView() || state?.mode !== 'lesson') {
      showToast('仅小科课对模式可用');
      return;
    }
    if (!P.old_code || !P.new_code || !P.new_lesson_uid) {
      showToast('课对参数不完整');
      return;
    }
    if (ocrBusyKey) {
      showToast('有 OCR 任务进行中，请稍候');
      return;
    }
    const newCount = Number(state?.page_count || 0);
    const oldCount = Number(state?.old_page_count || 0);
    if (!confirm(
      `整课一键（合成比对）\n`
      + `1) 识别旧课全部 ${oldCount || '?'} 页并落盘\n`
      + `2) 识别新课全部 ${newCount || '?'} 页并落盘\n`
      + `3) 每页小科独立「AI整理原子」（与新库隔离，强化实验表合并）\n`
      + `4) 合成两侧整课识别结果，做全文差异 + 跨页原子差异\n`
      + '页数不一致也可对齐内容。已有侧缓存会跳过。继续？'
    )) return;

    ocrBusyKey = 'xiaoke-lesson-one-click';
    syncXiaokeOcrLessonBtn();
    setOcrButtonsBusy('xiaoke-lesson-one-click');
    startOcrProgress('整课一键启动中…');
    setAtomStatus('整课一键启动中…');
    try {
      const res = await fetch('/api/textbook-diff/workbook/xiaoke/lesson-full', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: P.old_code,
          new_code: P.new_code,
          new_lesson_uid: P.new_lesson_uid,
          skip_cached: true,
          purpose: 'full',
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      const job = await pollXiaokeLessonFull(P.new_lesson_uid);
      const st = String(job.status || '');
      const errN = (job.errors || []).length;
      if (st === 'error') {
        throw new Error(job.last_error || job.message || '整课一键失败');
      }
      if (st === 'done_with_errors' || errN) {
        setAtomStatus(job.message || `整课一键完成（有失败）`);
        showToast(`整课一键完成（有 ${errN || '部分'} 失败）`);
      } else {
        setAtomStatus(job.message || '整课一键完成');
        showToast('整课一键完成');
      }
      await loadWorkspace().catch(() => {});
      await loadXiaokeLessonCompareResult().catch(() => {});
    } catch (e) {
      setAtomStatus(`整课一键失败：${e.message || e}`);
      showToast(String(e.message || e));
      throw e;
    } finally {
      ocrBusyKey = null;
      stopOcrProgress();
      setOcrButtonsBusy(null);
      syncXiaokeOcrLessonBtn();
    }
  }

  async function runPageOneClick() {
    if (ocrBusyKey) return;
    const body = atomRequestBody();
    if (!body.old_code || !body.new_code || body.old_page < 1 || body.new_page < 1) {
      showToast('页码参数不完整');
      return;
    }
    const needTextOcr = !(state?.atoms?.old_text_ocr_done && state?.atoms?.new_text_ocr_done);
    const needImageOcr = !(state?.atoms?.old_image_ocr_done && state?.atoms?.new_image_ocr_done);
    const plan = [
      needTextOcr ? '① 文字 OCR' : '① 跳过(缓存)',
      '② 文字比对',
      needImageOcr ? '③ 图片 OCR' : '③ 跳过(缓存)',
      '④ 图片比对',
    ].join(' → ');
    if (!confirm(`本页一键比对\n${plan}\n\n已有缓存会跳过对应 OCR；分步按钮仍可单独重跑。继续？`)) {
      return;
    }
    setOcrButtonsBusy('page-pipeline');
    const done = [];
    try {
      if (needTextOcr) {
        startOcrProgress('本页一键 · ① 文字 OCR');
        const ok = await runOcrPhase('both', 'text', {
          skipConfirm: true,
          skipAutoTextCompare: true,
          manageBusy: false,
          quietToast: true,
        });
        if (!ok) throw new Error('① 文字 OCR 失败，已中止（文字轨未写入坏结果时请重试）');
        done.push('①');
      } else {
        done.push('①缓存');
      }

      setAtomStatus('本页一键 · ② 文字比对…');
      const okText = await runTextCompare({
        skipBusyGuard: true,
        manageBusy: false,
        quietToast: true,
      });
      if (!okText) throw new Error('② 文字比对失败，已中止（图片轨未跑）');
      done.push('②');

      // ③ 失败不抹掉文字结果，只停图片轨
      const stillNeedImageOcr = !(state?.atoms?.old_image_ocr_done && state?.atoms?.new_image_ocr_done);
      if (stillNeedImageOcr) {
        startOcrProgress('本页一键 · ③ 图片 OCR');
        const okImg = await runOcrPhase('both', 'images', {
          skipConfirm: true,
          manageBusy: false,
          quietToast: true,
        });
        if (!okImg) {
          setAtomStatus('本页一键：文字轨完成；③ 图片 OCR 失败');
          showToast(`本页一键：文字轨完成（${done.join('→')}）；③ 失败，图片轨未跑。可单独点 ③ 重试`);
          setResultTab('text');
          return;
        }
        done.push('③');
      } else {
        done.push('③缓存');
      }

      setAtomStatus('本页一键 · ④ 图片比对…');
      const okImgCmp = await runImageCompare({
        skipBusyGuard: true,
        manageBusy: false,
        quietToast: true,
      });
      if (!okImgCmp) {
        setAtomStatus('本页一键：文字轨完成；④ 图片比对失败');
        showToast(`本页一键：文字轨完成（${done.join('→')}）；④ 失败。可单独点 ④ 重试`);
        setResultTab('text');
        return;
      }
      done.push('④');
      setResultTab('text');
      const tv = textCompareState?.verdict || '文字比对完成';
      const iv = imageCompareState?.verdict || '图片比对完成';
      await markReadyForReview();
      setAtomStatus(`本页一键完成 · ${tv} / ${iv}`);
      showToast(`本页一键完成 · ${done.join('→')} · ${tv} · ${iv}`);
    } catch (e) {
      setAtomStatus(String(e));
      showToast(String(e));
    } finally {
      stopOcrProgress();
      setOcrButtonsBusy(null);
    }
  }

  /** 数学等非小科学科：整课一键——对本课所有页跑 ①→④（后台串行/并行） */
  let lessonPipelineBusy = false;
  let lessonPollTimer = null;

  function isMathView() {
    const subj = String(P.subject || state?.subject || state?.old_volume?.subject || '').trim();
    return subj === '数学' || subj === 'shuxue';
  }

  function syncLessonPipelineBtn() {
    const btn = $(LESSON_PIPELINE_BTN);
    if (!btn) return;
    const show = isMathView() && (P.mode || state?.mode) === 'lesson' && !!P.new_lesson_uid;
    btn.hidden = !show;
    btn.disabled = !show || lessonPipelineBusy || !!ocrBusyKey;
    btn.textContent = lessonPipelineBusy ? '整课一键中…' : '整课一键';
    if (show && !lessonPipelineBusy) {
      btn.classList.add('diff-view-btn-primary');
      btn.classList.remove('secondary');
    }
    // 数学整课模式：隐藏"本页一键"
    const pageBtn = $(PAGE_ONE_CLICK_BTN);
    if (pageBtn) pageBtn.hidden = show;
  }

  async function runLessonPipeline() {
    if (lessonPipelineBusy) return;
    if (!P.old_code || !P.new_code || !P.new_lesson_uid) {
      showToast('课对参数不完整');
      return;
    }
    const pageCount = Number(state?.page_count || 0);
    if (!confirm(`整课一键\n对本课全部 ${pageCount || '?'} 页逐页跑 ①→④（有缓存跳过）。\n可离开本页，任务在后台继续。继续？`)) return;

    lessonPipelineBusy = true;
    syncLessonPipelineBtn();
    setOcrButtonsBusy('lesson-pipeline');
    setAtomStatus('整课一键已启动…');
    try {
      const res = await fetch('/api/textbook-diff/workbook/pipeline/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: P.old_code,
          new_code: P.new_code,
          scope: 'lesson',
          new_lesson_uid: P.new_lesson_uid,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      setAtomStatus('整课一键进行中…');
      showToast('整课一键已启动');
      // 轮询 pipeline status
      await pollLessonPipeline();
      await loadWorkspace().catch(() => {});
      setAtomStatus('整课一键完成');
      showToast('整课一键完成');
    } catch (e) {
      setAtomStatus(`整课一键失败：${e.message || e}`);
      showToast(String(e.message || e));
    } finally {
      lessonPipelineBusy = false;
      syncLessonPipelineBtn();
      setOcrButtonsBusy(null);
    }
  }

  async function pollLessonPipeline() {
    const maxMs = 45 * 60 * 1000;
    const t0 = Date.now();
    while (Date.now() - t0 < maxMs) {
      await sleep(3000);
      try {
        const q = new URLSearchParams({ old_code: P.old_code, new_code: P.new_code });
        const r = await fetch(`/api/textbook-diff/workbook/pipeline/status?${q}`);
        const d = await r.json().catch(() => ({}));
        const job = d.job || {};
        const status = String(job.status || '');
        const msg = job.message || '';
        setAtomStatus(msg || '整课一键进行中…');
        if (status === 'done' || status === 'done_with_errors' || status === 'cancelled' || status === 'error') {
          return job;
        }
      } catch (_) { /* ignore poll errors */ }
    }
    throw new Error('整课一键超时');
  }

  function pairStatusLabel(st) {
    const map = {
      suggested: '待对比',
      pending_review: '待确认',
      confirmed: '已确认',
      rejected: '无对应',
    };
    return map[st] || st || '';
  }

  function syncPairStatusChrome() {
    const badge = $('#pair-status-badge');
    const btn = $('#btn-confirm-lesson');
    if (P.mode !== 'lesson') {
      if (badge) badge.hidden = true;
      if (btn) btn.hidden = true;
      return;
    }
    const st = state?.pair_status || 'suggested';
    if (badge) {
      badge.hidden = false;
      badge.textContent = pairStatusLabel(st);
      badge.className = `badge-pair-status badge-pair-${st}`;
    }
    if (btn) {
      btn.hidden = true;
    }
  }

  async function markReadyForReview() {
    if (P.mode !== 'lesson' || !P.new_lesson_uid) return;
    try {
      const res = await fetch('/api/textbook-diff/workbook/coarse-ready-review', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: P.old_code,
          new_code: P.new_code,
          new_lesson_uid: P.new_lesson_uid,
        }),
      });
      const data = await res.json();
      if (data.ok) {
        state.pair_status = data.pair_status || 'pending_review';
        syncPairStatusChrome();
      }
    } catch (e) {
      /* 不阻断主流程 */
    }
  }

  async function confirmLessonPair() {
    if (P.mode !== 'lesson' || !P.new_lesson_uid) return;
    if (!confirm('确认已看完本课对比结果？状态将变为「已确认」。')) return;
    try {
      const res = await fetch('/api/textbook-diff/workbook/coarse-confirm-lesson', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: P.old_code,
          new_code: P.new_code,
          new_lesson_uid: P.new_lesson_uid,
        }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || '确认失败');
      state.pair_status = data.pair_status || 'confirmed';
      syncPairStatusChrome();
      showToast('本课对比已确认');
    } catch (e) {
      showToast(String(e));
    }
  }

  OCR_BUTTONS.forEach(({ id, side, phase }) => {
    $(id)?.addEventListener('click', () => runOcrPhase(side, phase).catch((e) => showToast(String(e))));
  });
  $(TEXT_COMPARE_BTN)?.addEventListener('click', () => runTextCompare({ force: true }).catch((e) => showToast(String(e))));
  $(IMAGE_COMPARE_BTN)?.addEventListener('click', () => runImageCompare().catch((e) => showToast(String(e))));
  $(PAGE_ONE_CLICK_BTN)?.addEventListener('click', () => runPageOneClick().catch((e) => showToast(String(e))));
  $(LESSON_PIPELINE_BTN)?.addEventListener('click', () => runLessonPipeline().catch((e) => showToast(String(e))));
  $(XIAOK_OCR_LESSON_BTN)?.addEventListener('click', () => runXiaokeLessonOneClick().catch((e) => showToast(String(e))));
  $('#btn-confirm-lesson')?.addEventListener('click', () => confirmLessonPair().catch((e) => showToast(String(e))));
  $('#tab-result-text')?.addEventListener('click', () => setResultTab('text'));
  $('#tab-result-image')?.addEventListener('click', () => setResultTab('image'));
  $('#tab-result-lesson')?.addEventListener('click', () => setResultTab('lesson'));

  async function downloadCompareExport() {
    const body = atomRequestBody();
    // 数学整课模式：导出全部页对比
    if (isMathView() && (P.mode || state?.mode) === 'lesson' && P.new_lesson_uid) {
      const q = new URLSearchParams({
        old_code: body.old_code || P.old_code,
        new_code: body.new_code || P.new_code,
      });
      if (P.new_lesson_uid) q.set('new_lesson_uid', P.new_lesson_uid);
      showToast('正在生成整课对比 Excel…');
      try {
        const res = await fetch(`/api/textbook-diff/workbook/export-lesson-compare?${q}`);
        const ct = (res.headers.get('content-type') || '').toLowerCase();
        if (!res.ok || ct.includes('application/json')) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.error || `导出失败（${res.status}）`);
        }
        const blob = await res.blob();
        let filename = `${body.old_code || P.old_code}_${body.new_code || P.new_code}_lesson_compare.xlsx`;
        const cd = res.headers.get('content-disposition') || '';
        const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd);
        if (m) {
          try { filename = decodeURIComponent(m[1].replace(/"/g, '').trim()); } catch (_) { filename = m[1].replace(/"/g, '').trim(); }
        }
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = filename;
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
        showToast('整课对比 Excel 已下载');
      } catch (e) {
        showToast(String(e.message || e));
      }
      return;
    }
    if (!body.old_page || !body.new_page) {
      showToast('页面未就绪，请稍后再试');
      return;
    }
    const q = new URLSearchParams({
      old_code: body.old_code,
      new_code: body.new_code,
      old_page: String(body.old_page),
      new_page: String(body.new_page),
    });
    if (body.new_pdf_source) q.set('new_pdf_source', body.new_pdf_source);
    if (body.preview_blob_id) q.set('preview_blob_id', body.preview_blob_id);
    showToast('正在生成 Excel…');
    try {
      const res = await fetch(`/api/textbook-diff/view/compare-export?${q}`);
      const ct = (res.headers.get('content-type') || '').toLowerCase();
      if (!res.ok || ct.includes('application/json')) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `导出失败（${res.status}）`);
      }
      const blob = await res.blob();
      let filename = `${body.old_code}_${body.new_code}_p${body.old_page}-${body.new_page}_compare.xlsx`;
      const cd = res.headers.get('content-disposition') || '';
      const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd);
      if (m) {
        try {
          filename = decodeURIComponent(m[1].replace(/"/g, '').trim());
        } catch (_) {
          filename = m[1].replace(/"/g, '').trim() || filename;
        }
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      showToast('对比 Excel 已下载');
    } catch (e) {
      showToast(String(e.message || e));
    }
  }

  $('#btn-download-compare')?.addEventListener('click', () => {
    downloadCompareExport().catch((e) => showToast(String(e)));
  });

  $('#toggle-atom-overlay')?.addEventListener('change', (e) => {
    showAtomOverlay = !!e.target.checked;
    syncOverlayVisibility();
  });

  $('#btn-xiaoke-phase-compare')?.addEventListener('click', () => {
    if (!isXiaokeLessonMode()) return;
    showXiaokePhaseView();
  });

  $('#lesson-phase-toggle')?.addEventListener('click', (ev) => {
    const btn = ev.target.closest('.diff-phase-toggle-btn');
    if (!btn || btn.disabled) return;
    setXiaokePhaseView(btn.dataset.phaseView || 'phase');
  });

  $('#btn-xiaoke-phase-segment')?.addEventListener('click', async () => {
    if (!isXiaokeLessonMode() || !P.old_code || !P.new_code || !P.new_lesson_uid) {
      showToast('仅小科课对模式可用');
      return;
    }
    const btn = $('#btn-xiaoke-phase-segment');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '环节整理中…';
    }
    try {
      const res = await fetch('/api/textbook-diff/workbook/xiaoke/phase-segment', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: P.old_code,
          new_code: P.new_code,
          new_lesson_uid: P.new_lesson_uid,
          force: true,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      if (data.started && data.job) {
        ocrBusyKey = 'xiaoke-lesson-one-click';
        syncXiaokeOcrLessonBtn();
        startOcrProgress(data.message || '文字识别后划分环节…');
        const job = await pollXiaokeLessonFull(P.new_lesson_uid);
        const st = String(job.status || '');
        if (st === 'error') {
          throw new Error(job.last_error || job.message || '环节整理失败');
        }
        await loadXiaokeLessonCompareResult({ activate: true });
        showToast(job.message || '环节整理完成');
        return;
      }
      if (lessonCompareState) {
        lessonCompareState.old_phases = data.old_phases || [];
        lessonCompareState.new_phases = data.new_phases || [];
        lessonCompareState.old_phases_by_page = data.old_phases_by_page || {};
        lessonCompareState.new_phases_by_page = data.new_phases_by_page || {};
        lessonCompareState.phase_changes = data.phase_changes || [];
        if (data.change_advice) {
          lessonCompareState.change_advice = data.change_advice;
          lessonCompareState.change_advice_reason = data.change_advice_reason || '';
        }
        ingestLessonAtomsByPage(lessonCompareState);
      } else {
        await loadXiaokeLessonCompareResult({ activate: false });
      }
      showXiaokePhaseView();
      showToast(data.message || '环节划分完成');
    } catch (e) {
      showToast(e.message || String(e));
    } finally {
      ocrBusyKey = null;
      syncXiaokeOcrLessonBtn();
      if (btn) {
        btn.disabled = false;
        btn.textContent = '环节整理';
      }
    }
  });
  function navigatePageMode(delta) {
    const op = Number(state.old_page) + delta;
    const np = Number(state.new_page) + delta;
    if (np < 1 || op < 1) {
      showToast('已到首页');
      return;
    }
    const q = new URLSearchParams(location.search);
    q.set('old_page', String(op));
    q.set('new_page', String(np));
    q.set('mode', 'page');
    location.assign(`${location.pathname}?${q.toString()}`);
  }

  $('#prev-page-btn')?.addEventListener('click', () => {
    if (!state) return;
    if (state.mode === 'page') {
      navigatePageMode(-1);
      return;
    }
    if (state.page_index <= 1) return;
    selectLessonPage(Number(state.page_index) - 1).catch((e) => showToast(String(e)));
  });
  $('#next-page-btn')?.addEventListener('click', () => {
    if (!state) return;
    if (state.mode === 'page') {
      navigatePageMode(1);
      return;
    }
    if (state.page_index >= state.page_count) return;
    selectLessonPage(Number(state.page_index) + 1).catch((e) => showToast(String(e)));
  });

  if (!P.old_code || !P.new_code) {
    showToast('缺少册次参数');
  } else {
    applyBackLinks();
    syncXiaokeLessonUi();
    syncLessonPipelineBtn();
    loadWorkspace().catch((e) => {
      stopOcrProgress();
      setAtomStatus(String(e));
      showToast(String(e));
    });
  }
})();
