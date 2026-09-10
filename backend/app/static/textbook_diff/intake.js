(function () {
  const API = window.DIFF_API_PREFIX;
  const BOOK_TYPE = window.DIFF_BOOK_TYPE;

  let currentCode = null;
  let lastVolumeData = null;
  let parsePollTimer = null;
  let previewOpenLessonUid = null;
  let previewRefreshTimer = null;
  let previewBackdropScrollY = 0;
  /** 上传后自动整册预处理进行中，避免重复触发 */
  let preprocessPipelineBusy = false;
  /** @type {ReturnType<typeof IntakeDualPdf.init> | null} */
  let dualPdfUi = null;

  const $ = (id) => document.getElementById(id);

  function hintWithDot(className, text) {
    return (
      `<span class="${className}">` +
      `<span class="cw-upload-dot" aria-hidden="true"></span>` +
      `${text}</span>`
    );
  }

  /** 仅有修订版、无完整版：走修订预处理，不跑识别目录/划分页码 */
  function isDraftOnlyMode(data) {
    return !!(data && !data.has_pdf && data.has_preview_pdf);
  }

  function initDualPdfUi() {
    if (dualPdfUi || !currentCode || typeof IntakeDualPdf === 'undefined') return;
    dualPdfUi = IntakeDualPdf.init({
      apiPrefix: API,
      getVolumeCode: () => currentCode,
      getVolumeData: () => lastVolumeData,
      reloadVolume: loadVolume,
      readJson: (r) => r.json(),
      toast,
      escapeHtml,
      formatBytes,
      hintWithDot,
      bookType: BOOK_TYPE,
      onUploadSuccess: ({ role }) => afterPdfUpload(role),
    });
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  function isFullParseDone(data) {
    const d = data || lastVolumeData;
    if (!d || d.parse_status !== 'done') return false;
    return (d.lessons || []).some((l) => l.page_start && l.page_end);
  }

  function isFullPagesDone(data) {
    const d = data || lastVolumeData;
    const lessons = d?.lessons || [];
    return lessons.length > 0 && lessons.every((l) => (l.lesson_page_count || 0) > 0);
  }

  async function runCatalogStep() {
    if (!currentCode) throw new Error('未选择册次');
    const catalogBtn = $('catalog-btn');
    if (catalogBtn) {
      catalogBtn.disabled = true;
      catalogBtn.textContent = '识别中…';
    }
    const meta = $('catalog-meta');
    if (meta) {
      meta.hidden = false;
      meta.textContent = '正在识别目录（约 1–2 分钟）…';
    }
    const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}/catalog-from-pdf`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ replace: true }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '识别失败');
    toast(d.message || '目录识别完成');
    await loadVolume();
  }

  async function waitForParseCompletion({ toastOnDone = true } = {}) {
    const maxMs = 20 * 60 * 1000;
    const started = Date.now();
    while (Date.now() - started < maxMs) {
      await sleep(3000);
      if (!currentCode) throw new Error('未选择册次');
      const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}`);
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '加载册次失败');
      lastVolumeData = d;
      renderVolume(d);
      if (d.parse_status === 'done') {
        if (toastOnDone) toast('页码划分完成');
        return d;
      }
      if (d.parse_status === 'failed') {
        throw new Error(d.parse_error || d.error || '划分失败');
      }
      if (d.parse_status !== 'processing') {
        throw new Error(d.parse_error || '划分状态异常');
      }
      const waited = Math.round((Date.now() - started) / 1000);
      const meta = $('parse-meta');
      if (meta) {
        meta.hidden = false;
        meta.textContent = `后台划分中… 已等待 ${waited} 秒`;
      }
    }
    throw new Error('划分仍在进行，请稍后刷新页面查看结果');
  }

  async function runParseStep(opts = {}) {
    if (!currentCode) throw new Error('未选择册次');
    const forceRecalibrate = !!opts.forceRecalibrate;
    const parseBtn = $('parse-btn');
    if (parseBtn) {
      parseBtn.disabled = true;
      parseBtn.textContent = '划分中…';
    }
    const meta = $('parse-meta');
    if (meta) {
      meta.hidden = false;
      meta.textContent = '正在划分页码（约 2–5 分钟）…';
    }
    const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}/parse`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        replace_lessons: true,
        force_recalibrate: forceRecalibrate,
      }),
    });
    const d = await r.json();
    if (d.parse_status === 'processing' || r.status === 202) {
      toast(d.message || '后台划分已开始');
      await waitForParseCompletion({ toastOnDone: true });
      return;
    }
    if (!d.ok) throw new Error(d.error || d.parse_error || '划分失败');
    await loadVolume();
  }

  async function runBuildPagesStep() {
    if (!currentCode) throw new Error('未选择册次');
    const buildBtn = $('build-pages-btn');
    if (buildBtn) {
      buildBtn.disabled = true;
      buildBtn.textContent = '生成中…';
    }
    const meta = $('pages-meta');
    if (meta) {
      meta.hidden = false;
      meta.textContent = '正在生成页图…';
    }
    const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}/lesson-pages`, {
      method: 'POST',
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '生成失败');
    toast(`已生成 ${d.lesson_pages_written || 0} 张页图`);
    await loadVolume();
  }

  async function runDraftMapStep() {
    if (!currentCode) throw new Error('未选择册次');
    const draftMapBtn = $('draft-map-btn');
    if (draftMapBtn) {
      draftMapBtn.disabled = true;
      draftMapBtn.textContent = '解析中…';
    }
    const meta = $('draft-meta');
    if (meta) {
      meta.hidden = false;
      meta.textContent = '正在解析印刷页码（约几十秒）…';
    }
    const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}/draft-page-map`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const d = await readApiJson(r);
    if (!d.ok) throw new Error(d.error || '解析失败');
    toast(`已识别印刷页码 ${d.mapped || d.draft_page_mapped || 0}/${d.page_count || d.draft_page_count || 0}`);
    await loadVolume();
  }

  async function runDraftPagesStep() {
    if (!currentCode) throw new Error('未选择册次');
    const draftPagesBtn = $('draft-pages-btn');
    if (draftPagesBtn) {
      draftPagesBtn.disabled = true;
      draftPagesBtn.textContent = '生成中…';
    }
    const meta = $('draft-meta');
    if (meta) {
      meta.hidden = false;
      meta.textContent = '正在生成修订版页图…';
    }
    const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}/draft-pages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const d = await readApiJson(r);
    if (!d.ok) throw new Error(d.error || '生成失败');
    toast(`已生成 ${d.written || d.draft_pages_count || 0} 张修订版页图`);
    await loadVolume();
  }

  /** 完整版：识别目录 → 划分页码 → 生成页图 */
  async function runFullPreprocessPipeline({ force = false } = {}) {
    if (preprocessPipelineBusy) return;
    preprocessPipelineBusy = true;
    stopParsePolling();
    try {
      let data = lastVolumeData;
      if (force || !((data?.lesson_count || 0) > 0)) {
        await runCatalogStep();
        data = lastVolumeData;
      }
      if (force || !isFullParseDone(data)) {
        await runParseStep({ forceRecalibrate: force });
        data = lastVolumeData;
      }
      if (!isFullParseDone(data)) {
        toast('页码划分未完成，请检查预处理结果');
        return;
      }
      if (force || !isFullPagesDone(data)) {
        await runBuildPagesStep();
      }
      toast('整册预处理完成');
    } catch (e) {
      toast(e.message || String(e));
    } finally {
      preprocessPipelineBusy = false;
      await loadVolume();
    }
  }

  /** 仅修订版：解析印刷页码 → 生成页图 */
  async function runDraftPreprocessPipeline({ force = false } = {}) {
    if (preprocessPipelineBusy) return;
    preprocessPipelineBusy = true;
    try {
      let data = lastVolumeData;
      const mapped = (data?.draft_page_mapped || 0) > 0;
      if (force || !mapped) {
        await runDraftMapStep();
        data = lastVolumeData;
      }
      if (force || !data?.draft_pages_ready) {
        await runDraftPagesStep();
      }
      toast('修订版预处理完成');
    } catch (e) {
      toast(e.message || String(e));
    } finally {
      preprocessPipelineBusy = false;
      await loadVolume();
    }
  }

  async function afterPdfUpload(role) {
    if (role === 'full') {
      await runFullPreprocessPipeline({ force: true });
      return;
    }
    if (role === 'draft' && isDraftOnlyMode(lastVolumeData)) {
      await runDraftPreprocessPipeline({ force: true });
    }
  }

  function toast(msg) {
    const el = $('toast'); if (!el) return;
    el.textContent = String(msg || '').slice(0, 200);
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 4000);
  }

  function formatBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let v = n, i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  function isSubtitleLesson(les) {
    // 语文副标题课号如 8.1；数学主课本身就是 12.1，不得当副标题只显示标题
    const subject = String(
      (lastVolumeData && lastVolumeData.subject) || ''
    ).trim();
    if (subject && subject !== '语文' && subject !== 'Test') return false;
    return /\.\d+$/.test(String(les.lesson_no || '').trim())
      || les.import_batch === 'subtitle';
  }

  function formatLessonLabel(les) {
    const no = String(les.lesson_no || '').trim();
    const name = String(les.lesson_name || '').trim();
    let label;
    if (no && name) {
      // 课号已含标题时勿重复拼接（如「读一读 分式方程的增根」+「分式方程的增根」）
      if (no === name || no.endsWith(name) || no.includes(` ${name}`)) label = no;
      else if (name.startsWith(no) || name.includes(` ${no}`)) label = name;
      else label = `${no} ${name}`;
    } else {
      label = no || name || '—';
    }
    if (isSubtitleLesson(les)) label = `└ ${name || label}`;
    return label;
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  }

  function lessonUnitPositions(lessons) {
    const buckets = {};
    for (const les of lessons) {
      const u = les.unit_no;
      if (!buckets[u]) buckets[u] = [];
      buckets[u].push(les.lesson_uid);
    }
    const map = {};
    for (const uids of Object.values(buckets)) {
      uids.forEach((uid, idx) => {
        map[uid] = { idx, total: uids.length };
      });
    }
    return map;
  }

  function infoRows(rows) {
    return rows.filter(Boolean).map(([k, v]) =>
      `<div class="intake-info-row"><span class="label">${escapeHtml(k)}</span><span>${v}</span></div>`
    ).join('');
  }

  function parseStatusLabel(s) {
    return { pending:'待解析', processing:'解析中', done:'已解析', failed:'解析失败' }[s] || s || '待解析';
  }

  async function loadVolume() {
    if (!currentCode) return;
    try {
      const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}`);
      const d = await r.json();
      if (!d.ok) { toast('加载失败：' + (d.error || '')); return; }
      renderVolume(d);
    } catch (e) { toast('加载失败：' + e); }
  }

  function renderVolume(data) {
    lastVolumeData = data;
    const titleEl = $('page-title');
    const subEl = $('page-sub');
    if (titleEl) titleEl.textContent = data.display_title || data.volume_code;
    if (subEl) subEl.innerHTML = [
      `<span class="intake-meta-chip">${escapeHtml(data.volume_code)}</span>`,
      `<span class="intake-meta-chip">${data.lesson_count} 课</span>`,
      `<span class="status-badge ${data.parse_status || 'pending'}">${parseStatusLabel(data.parse_status)}</span>`,
    ].join('');

    const draftOnly = isDraftOnlyMode(data);
    const leadEl = $('intake-lead');
    if (leadEl) {
      const kind = BOOK_TYPE === 'diff_old' ? '旧教材' : '新教材';
      leadEl.textContent = draftOnly
        ? `${kind}上传 · 修订版预处理（解析印刷页码 → 生成页图）`
        : `${kind}上传 · 整册预处理（新旧共用同一套流程）`;
    }
    const processTitle = $('process-title');
    const processHint = $('process-hint');
    const flow2 = $('flow-step-2');
    const flow3 = $('flow-step-3');
    if (processTitle) processTitle.textContent = draftOnly ? '修订版预处理' : '整册预处理';
    if (processHint) {
      if (draftOnly) {
        processHint.innerHTML =
          '上传修订版后自动：解析印刷页码 → 生成页图（也可手动重跑）。<span id="page-layout-hint"></span>';
      } else {
        processHint.innerHTML =
          '上传完整版后自动依次：识别目录 → 划分页码 → 生成页图（也可手动重跑）。<span id="page-layout-hint"></span>';
      }
    }
    if (flow2) flow2.innerHTML = draftOnly
      ? '<span class="wb-flow-num">2</span>解析印刷页码 · 生成页图'
      : '<span class="wb-flow-num">2</span>识别目录 · 划分页码';
    if (flow3) flow3.innerHTML = draftOnly
      ? '<span class="wb-flow-num">3</span>粗分 · 对比'
      : '<span class="wb-flow-num">3</span>生成页图 · 对比';

    const layoutHint = $('page-layout-hint');
    if (layoutHint) {
      if (data.page_layout_label) {
        layoutHint.textContent = `页图模式：${data.page_layout_label}（按 PDF 宽高比自动判定）。`;
      } else if (data.has_pdf) {
        layoutHint.textContent = '页图模式将在识别目录/划分页码时按 PDF 自动判定。';
      } else if (draftOnly) {
        layoutHint.textContent = '页图按修订版 PDF 物理页渲染，对照用印刷页码。';
      } else {
        layoutHint.textContent = '';
      }
    }

    const fullBtns = $('full-process-btns');
    const draftBtns = $('draft-process-btns');
    if (fullBtns) {
      fullBtns.hidden = draftOnly;
      fullBtns.style.display = draftOnly ? 'none' : 'flex';
      fullBtns.style.gap = '10px';
      fullBtns.style.flexWrap = 'wrap';
    }
    if (draftBtns) {
      draftBtns.hidden = !draftOnly;
      draftBtns.style.display = draftOnly ? 'flex' : 'none';
      draftBtns.style.gap = '10px';
      draftBtns.style.flexWrap = 'wrap';
    }

    // Lesson / catalog meta（完整版流程）
    const catMeta = $('catalog-meta');
    if (catMeta) {
      if (draftOnly) {
        catMeta.hidden = true;
        catMeta.textContent = '';
      } else if (data.lesson_count > 0) {
        catMeta.hidden = false;
        let catText = `已识别 ${data.lesson_count} 课时`;
        if (data.catalog_degraded) {
          catText += '（已降级，需复核）';
          const warns = data.catalog_warnings || [];
          if (warns.length) catText += '：' + warns.join('；');
        }
        catMeta.textContent = catText;
      } else if (data.has_pdf) {
        catMeta.hidden = false;
        catMeta.textContent = '尚无目录，请点「识别目录」';
      } else {
        catMeta.hidden = true;
        catMeta.textContent = '';
      }
    }

    // Parse meta
    const pMeta = $('parse-meta');
    if (pMeta) {
      if (draftOnly) {
        pMeta.hidden = true;
        pMeta.textContent = '';
      } else if (data.parse_status === 'done') { pMeta.hidden = false; pMeta.textContent = '页码划分已完成'; }
      else if (data.parse_status === 'failed') { pMeta.hidden = false; pMeta.textContent = '划分失败：' + (data.parse_error || ''); }
      else if (data.parse_status === 'processing') { pMeta.hidden = false; pMeta.textContent = '划分页码进行中…'; }
      else if ((data.lesson_count || 0) > 0) {
        pMeta.hidden = false;
        pMeta.textContent = '页码尚未划分，请点「划分页码」';
      } else {
        pMeta.hidden = true;
        pMeta.textContent = '';
      }
    }

    // Pages meta（完整版课时页图）
    const pagesMeta = $('pages-meta');
    if (pagesMeta) {
      if (draftOnly) {
        pagesMeta.hidden = true;
        pagesMeta.textContent = '';
      } else {
        const totalPages = (data.lessons || []).reduce((s, l) => s + (l.lesson_page_count || 0), 0);
        if (totalPages > 0) { pagesMeta.hidden = false; pagesMeta.textContent = `已生成 ${totalPages} 张页图`; }
        else {
          pagesMeta.hidden = true;
          pagesMeta.textContent = '';
        }
      }
    }

    // Draft preprocess meta
    const draftMeta = $('draft-meta');
    if (draftMeta) {
      if (draftOnly) {
        const mapped = data.draft_page_mapped || 0;
        const count = data.draft_page_count || 0;
        const ready = !!data.draft_pages_ready;
        const readyCount = data.draft_pages_count || 0;
        if (ready) {
          draftMeta.hidden = false;
          draftMeta.textContent = `印刷页码 ${mapped}/${count}；已生成 ${readyCount} 张修订版页图`;
        } else if (count > 0) {
          draftMeta.hidden = false;
          draftMeta.textContent = `已解析印刷页码 ${mapped}/${count}，请点「生成页图」`;
        } else {
          draftMeta.hidden = false;
          draftMeta.textContent = '请先点「解析印刷页码」';
        }
      } else {
        draftMeta.hidden = true;
        draftMeta.textContent = '';
      }
    }

    renderDraftMapPanel(data, draftOnly);

    // Lesson table
    const tbody = $('lesson-rows');
    const lessons = data.lessons || [];
    const unitPos = lessonUnitPositions(lessons);
    if (tbody) {
      tbody.innerHTML = lessons.map(les => {
        const pos = unitPos[les.lesson_uid] || { idx: 0, total: 1 };
        const canUp = pos.idx > 0;
        const canDown = pos.idx < pos.total - 1;
        const hasRange = les.page_start && les.page_end;
        const pageImgCount = les.lesson_page_count || 0;
        return `<tr data-lesson-row="${les.lesson_uid}">
          <td>${escapeHtml(les.unit_title || '')}</td>
          <td class="lesson-title-cell">
            <div class="lesson-reorder">
              <span class="lesson-move-btns" aria-label="调整顺序">
                <button type="button" class="btn-icon lesson-move-btn" data-lesson="${les.lesson_uid}" data-dir="up" title="上移" ${canUp ? '' : 'disabled'}>▲</button>
                <button type="button" class="btn-icon lesson-move-btn" data-lesson="${les.lesson_uid}" data-dir="down" title="下移" ${canDown ? '' : 'disabled'}>▼</button>
              </span>
              <span class="lesson-title-text${isSubtitleLesson(les) ? ' lesson-title-text--sub' : ''}">${escapeHtml(formatLessonLabel(les))}</span>
            </div>
          </td>
          <td class="page-range-cell">
            <div class="page-range-inputs">
              <input type="number" min="1" class="page-start-input" data-lesson="${les.lesson_uid}" value="${les.page_start ?? ''}" placeholder="起">
              <span class="page-range-dash">–</span>
              <input type="number" min="1" class="page-end-input" data-lesson="${les.lesson_uid}" value="${les.page_end ?? ''}" placeholder="止">
              ${les.page_range_verified ? '<span class="page-verified-badge">已校对</span>' : ''}
            </div>
            <div class="page-range-actions">
              <button type="button" class="page-preview-btn" data-lesson="${les.lesson_uid}">预览</button>
              <button type="button" class="page-save-btn" data-lesson="${les.lesson_uid}" hidden>保存</button>
            </div>
          </td>
          <td>${pageImgCount ? `<span class="intake-meta-chip">${pageImgCount} 张</span>` : (hasRange ? '<span class="page-pending-badge">未生成</span>' : '—')}</td>
        </tr>`;
      }).join('');
    }

    const lessonsCard = $('lessons-card');
    if (lessonsCard) lessonsCard.hidden = draftOnly || !lessons.length;

    bindLessonEvents();
    dualPdfUi?.refresh(data);
    refreshButtons();
    if (data.parse_status === 'processing' && !preprocessPipelineBusy) startParsePolling();
    else if (data.parse_status !== 'processing') stopParsePolling();
  }

  function draftPageImageUrl(pdfPage) {
    const bid = lastVolumeData?.preview_blob_id
      || lastVolumeData?.draft_pdfs?.[0]?.blob_id
      || '';
    const qs = new URLSearchParams({ source: 'draft', layout: 'single' });
    if (bid) qs.set('preview_blob_id', bid);
    return `${API}/volumes/${encodeURIComponent(currentCode)}/pdf-page/${pdfPage}.png?${qs}`;
  }

  function renderDraftMapPanel(data, draftOnly) {
    const card = $('draft-map-card');
    const tbody = $('draft-map-rows');
    if (!card || !tbody) return;

    const pages = data?.draft_page_map?.pages;
    if (!draftOnly || !pages?.length) {
      card.hidden = true;
      tbody.innerHTML = '';
      return;
    }

    card.hidden = false;
    tbody.innerHTML = pages.map((p) => {
      const pdfPage = p.pdf_page;
      const logical = p.mapped && p.logical_page != null
        ? String(p.logical_page)
        : '—';
      const kind = p.kind || (p.mapped ? '正文' : '未识别');
      const label = String(p.label || '').trim();
      const typeCell = (label && label !== kind)
        ? `${escapeHtml(kind)}<div class="intake-lessons-hint" style="margin:2px 0 0;display:block;">${escapeHtml(label)}</div>`
        : escapeHtml(kind);
      const known = p.mapped
        || ['单元页', '课题页', '封面', '目录', '前言说明', '正文'].includes(kind);
      const rowClass = known ? '' : ' class="draft-map-row--unmapped"';
      return `<tr${rowClass}>
        <td>${pdfPage}</td>
        <td>${escapeHtml(logical)}</td>
        <td>${typeCell}</td>
        <td><button type="button" class="draft-page-preview-btn" data-pdf-page="${pdfPage}">预览</button></td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('.draft-page-preview-btn').forEach((btn) => {
      btn.onclick = () => openDraftPagesPreview([Number(btn.dataset.pdfPage)]);
    });
  }

  function openDraftPagesPreview(pdfPages) {
    const pages = (pdfPages && pdfPages.length)
      ? pdfPages
      : (lastVolumeData?.draft_page_map?.pages || []).map((p) => p.pdf_page);
    if (!pages.length) { toast('暂无页码映射'); return; }

    openPreviewShell();
    const grid = $('preview-grid');
    const title = $('preview-title');
    if (!grid || !title) return;
    title.textContent = pages.length === 1
      ? `修订版 PDF 第 ${pages[0]} 页`
      : `修订版页图（共 ${pages.length} 页）`;
    // 展示跟页数走：1 居中 / 2 并排 / 多页网格（渲染 layout 仍由 PDF 决定）
    grid.className = pages.length === 1
      ? 'preview-grid preview-grid--one'
      : (pages.length === 2 ? 'preview-grid preview-grid--two' : 'preview-grid preview-grid--draft');
    const mapByPdf = Object.fromEntries(
      (lastVolumeData?.draft_page_map?.pages || []).map((p) => [p.pdf_page, p]),
    );
    grid.innerHTML = '<p class="hint-inline preview-grid-hint">点击图片可放大查看</p>' + pages.map((pdfPage) => {
      const meta = mapByPdf[pdfPage] || {};
      const logicLabel = meta.mapped && meta.logical_page != null
        ? `印刷 p${meta.logical_page}`
        : (meta.kind || '未识别');
      return `<div class="preview-card">
        <img src="${draftPageImageUrl(pdfPage)}" alt="pdf ${pdfPage}" onclick="this.classList.toggle('is-zoomed')">
        <div>PDF p${pdfPage} · ${escapeHtml(logicLabel)}</div>
      </div>`;
    }).join('');
  }

  function refreshButtons() {
    const d = lastVolumeData;
    if (!d) return;
    const draftOnly = isDraftOnlyMode(d);
    const hasPdf = !!d.has_pdf;
    const hasLessons = (d.lesson_count || 0) > 0;
    const hasRanges = (d.lessons || []).some(l => l.page_start && l.page_end);
    const parseDone = d.parse_status === 'done' && hasRanges;
    const hasPageImages = (d.lessons || []).every(l => (l.lesson_page_count || 0) > 0);
    const parseBusy = d.parse_status === 'processing';

    function setBtn(id, state) {
      const btn = $(id); if (!btn) return;
      btn.classList.remove('intake-step-btn-idle', 'intake-step-btn-done', 'intake-step-btn-ready');
      if (state.busy) { btn.disabled = true; btn.textContent = state.busyText || '处理中…'; return; }
      if (state.done) { btn.classList.add('intake-step-btn-done'); btn.disabled = false; btn.textContent = state.label || '已完成'; return; }
      if (state.enabled) { btn.classList.add('intake-step-btn-ready'); btn.disabled = false; btn.textContent = state.label || '执行'; return; }
      btn.classList.add('intake-step-btn-idle'); btn.disabled = true; btn.textContent = state.label || '待执行';
    }

    if (draftOnly) {
      const mapped = (d.draft_page_mapped || 0) > 0;
      const ready = !!d.draft_pages_ready;
      setBtn('draft-map-btn', {
        enabled: !preprocessPipelineBusy,
        done: mapped && !preprocessPipelineBusy,
        busy: preprocessPipelineBusy && !mapped,
        label: mapped ? '已解析页码' : '解析印刷页码',
        busyText: '解析中…',
      });
      setBtn('draft-pages-btn', {
        enabled: mapped && !ready && !preprocessPipelineBusy,
        done: ready && !preprocessPipelineBusy,
        busy: preprocessPipelineBusy && mapped && !ready,
        label: ready ? '已生成页图' : '生成页图',
        busyText: '生成中…',
      });
      return;
    }

    const pipelineBusy = preprocessPipelineBusy;
    setBtn('catalog-btn', {
      enabled: hasPdf && !pipelineBusy,
      done: hasLessons && !pipelineBusy,
      busy: pipelineBusy && !hasLessons,
      label: hasLessons ? '已识别' : '识别目录',
      busyText: '识别中…',
    });
    // 换 PDF 后：页码已清空 → 「划分页码」用 ready 样式，文案强调下一步
    const parseNeedsRun = hasLessons && hasPdf && !parseDone && !parseBusy;
    setBtn('parse-btn', {
      enabled: (parseNeedsRun || parseDone) && !pipelineBusy,
      done: parseDone && !pipelineBusy,
      busy: parseBusy || (pipelineBusy && hasLessons && !parseDone),
      label: parseDone ? '已划分' : (parseNeedsRun ? '请划分页码' : '划分页码'),
      busyText: '划分中…',
    });
    setBtn('build-pages-btn', {
      enabled: parseDone && !hasPageImages && !pipelineBusy,
      done: hasPageImages && parseDone && !pipelineBusy,
      busy: pipelineBusy && parseDone && !hasPageImages,
      label: hasPageImages && parseDone ? '已生成' : '一键生成页图',
      busyText: '生成中…',
    });
  }

  async function moveLesson(lessonUid, direction) {
    if (!currentCode) return;
    const btn = document.querySelector(
      `.lesson-move-btn[data-lesson="${lessonUid}"][data-dir="${direction}"]`,
    );
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(
        `${API}/volumes/${encodeURIComponent(currentCode)}/lessons/${encodeURIComponent(lessonUid)}/reorder`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ direction }),
        },
      );
      const d = await r.json();
      if (!r.ok || !d.ok) throw new Error(d.error || '调整顺序失败');
      renderVolume(d);
      toast(direction === 'up' ? '已上移' : '已下移');
    } catch (e) {
      toast(e.message || String(e));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function bindLessonEvents() {
    document.querySelectorAll('.lesson-move-btn').forEach(btn => {
      btn.onclick = () => moveLesson(btn.dataset.lesson, btn.dataset.dir);
    });
    // 仅课时表；勿绑定修订版明细里的预览按钮
    document.querySelectorAll('#lesson-rows .page-preview-btn').forEach(btn => {
      btn.onclick = () => openPreview(btn.dataset.lesson);
    });
    document.querySelectorAll('.page-save-btn').forEach(btn => {
      btn.onclick = () => savePageRange(btn.dataset.lesson);
    });
    document.querySelectorAll('.page-start-input, .page-end-input').forEach(inp => {
      inp.addEventListener('input', () => {
        markDirty(inp.dataset.lesson, true);
        if (previewOpenLessonUid === inp.dataset.lesson) {
          clearTimeout(previewRefreshTimer);
          previewRefreshTimer = setTimeout(() => openPreview(inp.dataset.lesson), 350);
        }
      });
    });
  }

  function getRangeInputs(uid) {
    return {
      start: document.querySelector(`.page-start-input[data-lesson="${uid}"]`),
      end: document.querySelector(`.page-end-input[data-lesson="${uid}"]`),
    };
  }

  function readRange(uid) {
    const { start, end } = getRangeInputs(uid);
    if (!start || !end) return null;
    const s = parseInt(start.value, 10), e = parseInt(end.value, 10);
    if (!Number.isFinite(s) || !Number.isFinite(e) || s < 1 || e < s) return null;
    return { page_start: s, page_end: e };
  }

  function markDirty(uid, dirty) {
    const { start, end } = getRangeInputs(uid);
    if (start) start.classList.toggle('dirty', dirty);
    if (end) end.classList.toggle('dirty', dirty);
    const saveBtn = document.querySelector(`.page-save-btn[data-lesson="${uid}"]`);
    if (saveBtn) saveBtn.hidden = !dirty;
  }

  async function savePageRange(uid) {
    const range = readRange(uid);
    if (!range) { toast('请填写有效起止页码'); return; }
    const btn = document.querySelector(`.page-save-btn[data-lesson="${uid}"]`);
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    try {
      const r = await fetch(`${API}/lessons/${uid}/page-range`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(range),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '保存失败');
      toast('已保存'); markDirty(uid, false); await loadVolume();
    } catch (e) { toast(e.message || String(e)); }
    finally { if (btn) { btn.disabled = false; btn.textContent = '保存'; } }
  }

  // ---- Catalog ----
  const catalogBtn = $('catalog-btn');
  if (catalogBtn) {
    catalogBtn.addEventListener('click', async () => {
      if (!currentCode || preprocessPipelineBusy) return;
      try {
        await runCatalogStep();
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        refreshButtons();
      }
    });
  }

  // ---- Parse ----
  const parseBtn = $('parse-btn');
  if (parseBtn) {
    parseBtn.addEventListener('click', async () => {
      if (!currentCode || preprocessPipelineBusy) return;
      try {
        await runParseStep();
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        if (!lastVolumeData || lastVolumeData.parse_status !== 'processing') {
          refreshButtons();
        }
      }
    });
  }

  function startParsePolling() {
    if (parsePollTimer) clearInterval(parsePollTimer);
    parsePollTimer = setInterval(async () => {
      if (!currentCode) { stopParsePolling(); return; }
      try {
        const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}`);
        const d = await r.json();
        if (!d.ok) return;
        lastVolumeData = d;
        if (d.parse_status === 'done') { stopParsePolling(); renderVolume(d); toast('页码划分完成'); }
        else if (d.parse_status === 'failed') { stopParsePolling(); renderVolume(d); toast('划分失败：' + (d.parse_error || '')); }
        else if (d.parse_status !== 'processing') { stopParsePolling(); }
        else { renderVolume(d); }
      } catch (e) { /* ignore */ }
    }, 3000);
  }

  function stopParsePolling() {
    if (parsePollTimer) {
      clearInterval(parsePollTimer);
      parsePollTimer = null;
    }
  }

  // ---- Build Pages ----
  const buildBtn = $('build-pages-btn');
  if (buildBtn) {
    buildBtn.addEventListener('click', async () => {
      if (!currentCode || preprocessPipelineBusy) return;
      try {
        await runBuildPagesStep();
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        refreshButtons();
      }
    });
  }

  // ---- Draft preprocess（仅修订版）----
  async function readApiJson(r) {
    const text = await r.text();
    try {
      return JSON.parse(text);
    } catch {
      if (r.status === 404) {
        throw new Error('接口不存在（404）。请重启 Flask 后再试');
      }
      throw new Error(`服务器返回非 JSON（HTTP ${r.status}），请查看服务端日志或重启后再试`);
    }
  }

  const draftMapBtn = $('draft-map-btn');
  if (draftMapBtn) {
    draftMapBtn.addEventListener('click', async () => {
      if (!currentCode || preprocessPipelineBusy) return;
      try {
        await runDraftMapStep();
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        refreshButtons();
      }
    });
  }

  const draftPagesBtn = $('draft-pages-btn');
  if (draftPagesBtn) {
    draftPagesBtn.addEventListener('click', async () => {
      if (!currentCode || preprocessPipelineBusy) return;
      try {
        await runDraftPagesStep();
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        refreshButtons();
      }
    });
  }

  const draftPreviewAllBtn = $('draft-preview-all-btn');
  if (draftPreviewAllBtn) {
    draftPreviewAllBtn.addEventListener('click', () => openDraftPagesPreview());
  }

  // ---- Page Preview ----
  function lockScroll() {
    previewBackdropScrollY = window.scrollY || document.documentElement.scrollTop || 0;
    document.body.classList.add('page-preview-open');
    document.body.style.top = `-${previewBackdropScrollY}px`;
  }
  function unlockScroll() {
    document.body.classList.remove('page-preview-open');
    document.body.style.top = '';
    window.scrollTo(0, previewBackdropScrollY);
  }
  function closePreview() {
    const ov = $('page-preview');
    if (!ov) return;
    ov.classList.remove('is-open'); ov.setAttribute('aria-hidden', 'true');
    previewOpenLessonUid = null;
    unlockScroll();
  }
  function openPreviewShell() {
    const ov = $('page-preview'); if (!ov) return;
    if (!ov.classList.contains('is-open')) lockScroll();
    ov.classList.add('is-open'); ov.setAttribute('aria-hidden', 'false');
  }

  const previewClose = $('preview-close');
  if (previewClose) previewClose.addEventListener('click', closePreview);
  const previewOverlay = $('page-preview');
  if (previewOverlay) previewOverlay.addEventListener('click', (e) => { if (e.target === previewOverlay) closePreview(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const ov = $('page-preview');
      if (ov && ov.classList.contains('is-open')) closePreview();
    }
  });

  function setPreviewGridLayout(grid, pageCount) {
    if (!grid) return;
    if (pageCount <= 1) grid.className = 'preview-grid preview-grid--one';
    else if (pageCount === 2) grid.className = 'preview-grid preview-grid--two';
    else grid.className = 'preview-grid preview-grid--draft';
  }

  async function openPreview(uid) {
    previewOpenLessonUid = uid;
    openPreviewShell();
    const grid = $('preview-grid');
    const title = $('preview-title');
    if (!grid || !title) return;
    grid.className = 'preview-grid';
    grid.innerHTML = '<p class="preview-empty">加载中…</p>';
    title.textContent = uid;

    const range = readRange(uid);
    if (!range) { grid.innerHTML = '<p class="preview-empty">请先填写有效起止页码</p>'; return; }

    try {
      const qs = new URLSearchParams({ page_start: String(range.page_start), page_end: String(range.page_end) });
      const r = await fetch(`${API}/lessons/${uid}/pages?${qs}`);
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '加载失败');
      title.textContent = `${d.lesson_name}（PDF ${d.page_start}–${d.page_end}）`;
      if (!d.pages?.length) { grid.innerHTML = '<p class="preview-empty">无页图</p>'; return; }
      setPreviewGridLayout(grid, d.pages.length);
      grid.innerHTML = '<p class="hint-inline preview-grid-hint">点击图片可放大查看</p>' + d.pages.map(p => `
        <div class="preview-card">
          <img src="${p.url}" alt="p${p.pdf_page ?? p.page_index}" onclick="this.classList.toggle('is-zoomed')">
          <div>PDF p${p.pdf_page ?? p.page_index}</div>
        </div>`).join('');
    } catch (e) { grid.innerHTML = `<p class="preview-empty">${e.message}</p>`; }
  }

  // ---- Init ----
  function pairedDiffCode(code) {
    const c = String(code || '').trim();
    if (c.endsWith('-DOLD')) return `${c.slice(0, -5)}-DNEW`;
    if (c.endsWith('-DNEW')) return `${c.slice(0, -5)}-DOLD`;
    return '';
  }

  function subjectIdFromVolumeCode(code) {
    const fromQuery = new URLSearchParams(window.location.search).get('subject');
    if (fromQuery) return fromQuery;
    if (typeof window.diffResolveActiveSubjectId === 'function') {
      return window.diffResolveActiveSubjectId('huaxue');
    }
    const c = String(code || '').toUpperCase();
    if (c.startsWith('TEST')) return 'test';
    if (c.startsWith('XK')) return 'xiaoke';
    if (c.startsWith('YWRJ') || c.startsWith('YW')) return 'yuwen';
    if (c.startsWith('SXJJ') || c.startsWith('SX')) return 'shuxue';
    if (c.startsWith('KXJR') || c.startsWith('KX')) return 'kexue';
    return 'huaxue';
  }

  function workbookPairBackHref(volumeCode) {
    const code = String(volumeCode || '').trim();
    const other = pairedDiffCode(code);
    if (!code || !other) {
      const sid = subjectIdFromVolumeCode(code);
      return `/textbook-diff/workbook?subject=${encodeURIComponent(sid)}`;
    }
    const oldCode = code.endsWith('-DOLD') ? code : other;
    const newCode = code.endsWith('-DNEW') ? code : other;
    const sid = subjectIdFromVolumeCode(code);
    const q = new URLSearchParams({
      subject: sid,
      old_code: oldCode,
      new_code: newCode,
    });
    return `/textbook-diff/workbook?${q}`;
  }

  const intakeBack = $('intake-back');
  if (intakeBack && window.DIFF_INTAKE_CODE) {
    intakeBack.href = workbookPairBackHref(window.DIFF_INTAKE_CODE);
  }

  const incomingCode = window.DIFF_INTAKE_CODE;
  if (incomingCode) {
    currentCode = incomingCode;
    const m = incomingCode.match(/^([A-Z]+)-(\d+)([SX])-/);
    const gradeLabels = {7:'七年级',8:'八年级',9:'九年级',10:'高一',11:'高二',12:'高三'};
    const label = m ? `${gradeLabels[parseInt(m[2], 10)] || m[2]+'年级'}${m[3]==='S'?'上册':'下册'}` : incomingCode;
    const titleEl = $('page-title');
    const subEl = $('page-sub');
    if (titleEl) titleEl.textContent = label;
    if (subEl) { subEl.hidden = false; subEl.textContent = incomingCode; }
    initDualPdfUi();
    loadVolume();
  }
})();
