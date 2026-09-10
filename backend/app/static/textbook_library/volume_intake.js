(function () {
  const API = window.LIBRARY_API_PREFIX || '/api/textbook-library';
  const LESSON_API = window.LIBRARY_LESSON_API_PREFIX || '/api/textbook-diff';

  let currentCode = window.LIBRARY_INTAKE_CODE || null;
  let lastVolumeData = null;
  let parsePollTimer = null;
  let previewOpenLessonUid = null;
  let previewRefreshTimer = null;
  let previewBackdropScrollY = 0;
  let preprocessPipelineBusy = false;
  let volumeOcrBusy = false;
  let volumeOcrPollTimer = null;
  let volumeOcrTickTimer = null;
  let volumeOcrCurrentJob = null;
  let volumeOcrLastFinishedAt = null;
  let selectedPdfFile = null;

  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  }

  function toast(msg) {
    const el = $('toast');
    if (!el) return;
    el.textContent = String(msg || '').slice(0, 200);
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 4000);
  }

  function formatBytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let v = n;
    let i = 0;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  function parseStatusLabel(s) {
    return { pending: '待解析', processing: '解析中', done: '已解析', failed: '解析失败' }[s] || s || '待解析';
  }

  function librarySlotHref(data) {
    const ed = data?.edition_id;
    if (!ed) return '/textbook-library/';
    const g = data.grade;
    const term = data.term || data.semester || '上';
    return `/textbook-library/slot?edition=${encodeURIComponent(ed)}&grade=${encodeURIComponent(g)}&term=${encodeURIComponent(term)}`;
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

  async function loadVolume() {
    if (!currentCode) return;
    try {
      const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}`);
      const d = await r.json();
      if (!d.ok) {
        toast('加载失败：' + (d.error || ''));
        return;
      }
      renderVolume(d);
      await syncVolumeOcrJobFromServer(false);
    } catch (e) {
      toast('加载失败：' + e);
    }
  }

  function renderFlowSteps(data) {
    const hasPdf = !!data.has_pdf;
    const hasLessons = (data.lesson_count || 0) > 0;
    const parseDone = isFullParseDone(data);
    const pagesDone = isFullPagesDone(data);
    const steps = [
      { id: 'flow-step-1', done: hasPdf },
      { id: 'flow-step-2', done: hasLessons },
      { id: 'flow-step-3', done: parseDone && pagesDone },
    ];
    steps.forEach(({ id, done }) => {
      const el = $(id);
      if (el) el.classList.toggle('is-done', done);
    });
  }

  function renderVolume(data) {
    lastVolumeData = data;
    const titleEl = $('page-title');
    const subEl = $('page-sub');
    if (titleEl) titleEl.textContent = data.display_title || data.volume_code;
    if (subEl) {
      subEl.innerHTML = [
        `<span class="intake-meta-chip">${escapeHtml(data.volume_code)}</span>`,
        data.version_label ? `<span class="intake-meta-chip">${escapeHtml(data.version_label)}</span>` : '',
        `<span class="intake-meta-chip">${data.lesson_count || 0} 课</span>`,
        `<span class="status-badge ${data.parse_status || 'pending'}">${parseStatusLabel(data.parse_status)}</span>`,
      ].filter(Boolean).join('');
    }

    const back = $('intake-back');
    if (back) {
      back.textContent = '← 册次格';
      back.href = librarySlotHref(data);
    }

    const layoutHint = $('page-layout-hint');
    if (layoutHint) {
      if (data.page_layout_label) {
        layoutHint.textContent = `页图模式：${data.page_layout_label}（按 PDF 宽高比自动判定）。`;
      } else if (data.has_pdf) {
        layoutHint.textContent = '页图模式将在识别目录/划分页码时按 PDF 自动判定。';
      } else {
        layoutHint.textContent = '';
      }
    }

    const pdfMeta = $('pdf-meta');
    if (pdfMeta) {
      if (data.has_pdf) {
        pdfMeta.hidden = false;
        pdfMeta.innerHTML = [
          data.pdf_filename ? `<div class="intake-info-row"><span class="label">文件</span><span>${escapeHtml(data.pdf_filename)}</span></div>` : '',
          data.pdf_size_bytes ? `<div class="intake-info-row"><span class="label">大小</span><span>${formatBytes(data.pdf_size_bytes)}</span></div>` : '',
        ].filter(Boolean).join('') || '已上传 PDF';
      } else {
        pdfMeta.hidden = true;
        pdfMeta.textContent = '';
      }
    }

    const catMeta = $('catalog-meta');
    if (catMeta) {
      if ((data.lesson_count || 0) > 0) {
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
        catMeta.textContent = '尚无目录，请点「识别目录」或「一键预处理」';
      } else {
        catMeta.hidden = true;
        catMeta.textContent = '';
      }
    }

    const pMeta = $('parse-meta');
    if (pMeta) {
      if (data.parse_status === 'done') {
        pMeta.hidden = false;
        pMeta.textContent = '页码划分已完成';
      } else if (data.parse_status === 'failed') {
        pMeta.hidden = false;
        pMeta.textContent = '划分失败：' + (data.parse_error || '');
      } else if (data.parse_status === 'processing') {
        pMeta.hidden = false;
        pMeta.textContent = '划分页码进行中…';
      } else if ((data.lesson_count || 0) > 0) {
        pMeta.hidden = false;
        pMeta.textContent = '页码尚未划分，请点「划分页码」';
      } else {
        pMeta.hidden = true;
        pMeta.textContent = '';
      }
    }

    const pagesMeta = $('pages-meta');
    if (pagesMeta) {
      const totalPages = (data.lessons || []).reduce((s, l) => s + (l.lesson_page_count || 0), 0);
      if (totalPages > 0) {
        pagesMeta.hidden = false;
        pagesMeta.textContent = `已生成 ${totalPages} 张页图`;
      } else {
        pagesMeta.hidden = true;
        pagesMeta.textContent = '';
      }
    }

    renderLessonTable(data);
    renderFlowSteps(data);
    refreshUploadButton(data);
    refreshButtons();
    if (data.parse_status === 'processing' && !preprocessPipelineBusy) startParsePolling();
    else if (data.parse_status !== 'processing') stopParsePolling();
  }

  function formatLessonLabel(les) {
    const no = String(les.lesson_no || '').trim();
    const name = String(les.lesson_name || '').trim();
    if (no && name) {
      if (no === name || no.endsWith(name) || no.includes(` ${name}`)) return no;
      if (name.startsWith(no) || name.includes(` ${no}`)) return name;
      return `${no} ${name}`;
    }
    return no || name || '—';
  }

  function renderLessonTable(data) {
    const tbody = $('lesson-rows');
    const lessons = data.lessons || [];
    const card = $('lessons-card');
    if (card) card.hidden = !lessons.length;
    if (!tbody) return;
    tbody.innerHTML = lessons.map((les) => {
      const hasRange = les.page_start && les.page_end;
      const pageImgCount = les.lesson_page_count || 0;
      const workHref = `/textbook-library/lessons/${encodeURIComponent(les.lesson_uid)}/work`;
      return `<tr data-lesson-row="${les.lesson_uid}">
        <td>${escapeHtml(les.unit_title || '')}</td>
        <td>${escapeHtml(formatLessonLabel(les))}</td>
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
        <td><a class="lesson-work-link" href="${workHref}">课时标注</a></td>
      </tr>`;
    }).join('');
    bindLessonEvents();
  }

  function lessonsWithPageImages(data) {
    return (data?.lessons || []).filter((l) => (l.lesson_page_count || 0) > 0);
  }

  function formatElapsed(ms) {
    const totalSec = Math.max(0, Math.floor(Number(ms) / 1000));
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    if (h > 0) {
      return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  function stripVolumeOcrElapsedSuffix(msg) {
    if (!msg) return '';
    const marker = '· 已用 ';
    const idx = msg.lastIndexOf(marker);
    return idx >= 0 ? msg.slice(0, idx).trim() : msg;
  }

  function renderVolumeOcrMeta() {
    const meta = $('volume-ocr-meta');
    const job = volumeOcrCurrentJob;
    if (!meta || !job) return;
    if (job.running && job.started_at) {
      const base = stripVolumeOcrElapsedSuffix(job.message || '整册 OCR+建块 进行中…');
      const startedMs = new Date(job.started_at).getTime();
      const elapsed = Number.isFinite(startedMs)
        ? formatElapsed(Date.now() - startedMs)
        : (job.elapsed_label || '0:00');
      meta.textContent = `${base} · 已用 ${elapsed}`;
      return;
    }
    meta.textContent = job.message || '整册 OCR+建块 进行中…';
  }

  function stopVolumeOcrTick() {
    if (volumeOcrTickTimer) {
      clearInterval(volumeOcrTickTimer);
      volumeOcrTickTimer = null;
    }
  }

  function startVolumeOcrTick() {
    stopVolumeOcrTick();
    const job = volumeOcrCurrentJob;
    if (!job || !job.running || !job.started_at) return;
    renderVolumeOcrMeta();
    volumeOcrTickTimer = setInterval(renderVolumeOcrMeta, 1000);
  }

  function stopVolumeOcrPolling() {
    if (volumeOcrPollTimer) {
      clearTimeout(volumeOcrPollTimer);
      volumeOcrPollTimer = null;
    }
  }

  function startVolumeOcrPolling() {
    stopVolumeOcrPolling();
    volumeOcrPollTimer = setTimeout(() => syncVolumeOcrJobFromServer(true), 2000);
  }

  function applyVolumeOcrJob(job) {
    volumeOcrCurrentJob = job;
    volumeOcrBusy = !!(job && job.running);
    const meta = $('volume-ocr-meta');
    if (meta) {
      if (job && (job.running || job.message)) {
        meta.hidden = false;
        if (job.running) {
          startVolumeOcrTick();
        } else {
          stopVolumeOcrTick();
          renderVolumeOcrMeta();
        }
      } else if (!job) {
        stopVolumeOcrTick();
        meta.hidden = true;
        meta.textContent = '';
      }
    }
    refreshButtons();
  }

  async function syncVolumeOcrJobFromServer(fromPoll) {
    if (!currentCode) return;
    try {
      const r = await fetch(
        `${API}/volumes/${encodeURIComponent(currentCode)}/volume-ocr`,
      );
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) {
        stopVolumeOcrPolling();
        if (volumeOcrBusy) {
          toast('整册任务已中断（可能因重启服务），请重新点击「整册 OCR+建块」');
        }
        volumeOcrBusy = false;
        applyVolumeOcrJob(null);
        return;
      }
      const job = d.job;
      if (!job) {
        stopVolumeOcrPolling();
        volumeOcrBusy = false;
        applyVolumeOcrJob(null);
        return;
      }
      const wasRunning = volumeOcrBusy;
      applyVolumeOcrJob(job);
      if (job.running) {
        startVolumeOcrPolling();
        return;
      }
      stopVolumeOcrPolling();
      if (job.finished_at && job.finished_at !== volumeOcrLastFinishedAt) {
        volumeOcrLastFinishedAt = job.finished_at;
        if (wasRunning || fromPoll) {
          toast(job.message || '整册 OCR+建块 完成');
          await loadVolume();
        }
      }
    } catch {
      if (fromPoll) startVolumeOcrPolling();
    }
  }

  async function runVolumeOcr() {
    if (volumeOcrBusy || preprocessPipelineBusy || !currentCode) return;
    const targets = lessonsWithPageImages(lastVolumeData);
    if (!targets.length) {
      toast('尚无带页图的课时，请先生成页图');
      return;
    }
    try {
      const r = await fetch(
        `${API}/volumes/${encodeURIComponent(currentCode)}/volume-ocr`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            skip_cached: true,
            with_blocks: true,
            replace_existing_blocks: true,
          }),
        },
      );
      const d = await r.json().catch(() => ({}));
      if (r.status === 409 && d.job) {
        applyVolumeOcrJob(d.job);
        startVolumeOcrPolling();
        toast(d.message || '整册 OCR+建块 进行中');
        return;
      }
      if (!r.ok || !d.ok || !d.job) {
        throw new Error(d.error || '启动整册 OCR+建块 失败');
      }
      volumeOcrLastFinishedAt = null;
      applyVolumeOcrJob(d.job);
      startVolumeOcrPolling();
      toast(`整册 OCR+建块 已在后台运行（${targets.length} 课），可离开本页`);
    } catch (e) {
      toast(e.message || String(e));
    }
  }

  function refreshUploadButton(data) {
    const btn = $('upload-btn');
    if (!btn) return;
    btn.classList.remove('pdf-upload-btn-idle', 'pdf-upload-btn-ready', 'pdf-upload-btn-done');
    // 已选新文件时优先可点（覆盖重传），勿被「已上传」锁死
    if (selectedPdfFile) {
      btn.disabled = false;
      btn.textContent = data?.has_pdf ? '重新上传' : '上传 PDF';
      btn.classList.add('pdf-upload-btn-ready');
      return;
    }
    if (data?.has_pdf) {
      btn.disabled = true;
      btn.textContent = '已上传';
      btn.classList.add('pdf-upload-btn-done');
      return;
    }
    btn.disabled = true;
    btn.textContent = '上传 PDF';
    btn.classList.add('pdf-upload-btn-idle');
  }

  function refreshButtons() {
    const d = lastVolumeData;
    if (!d) return;
    const hasPdf = !!d.has_pdf;
    const hasLessons = (d.lesson_count || 0) > 0;
    const hasRanges = (d.lessons || []).some((l) => l.page_start && l.page_end);
    const parseDone = d.parse_status === 'done' && hasRanges;
    const hasPageImages = (d.lessons || []).every((l) => (l.lesson_page_count || 0) > 0);
    const parseBusy = d.parse_status === 'processing';
    const pipelineBusy = preprocessPipelineBusy || volumeOcrBusy;

    function setBtn(id, state) {
      const btn = $(id);
      if (!btn) return;
      btn.classList.remove('intake-step-btn-idle', 'intake-step-btn-done', 'intake-step-btn-ready');
      if (state.busy) {
        btn.disabled = true;
        btn.textContent = state.busyText || '处理中…';
        return;
      }
      if (state.done) {
        btn.classList.add('intake-step-btn-done');
        btn.disabled = false;
        btn.textContent = state.label || '已完成';
        return;
      }
      if (state.enabled) {
        btn.classList.add('intake-step-btn-ready');
        btn.disabled = false;
        btn.textContent = state.label || '执行';
        return;
      }
      btn.classList.add('intake-step-btn-idle');
      btn.disabled = true;
      btn.textContent = state.label || '待执行';
    }

    setBtn('catalog-btn', {
      enabled: hasPdf && !pipelineBusy,
      done: hasLessons && !pipelineBusy,
      busy: pipelineBusy && !hasLessons,
      label: hasLessons ? '已识别' : '识别目录',
      busyText: '识别中…',
    });
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
    const pipelineBtn = $('pipeline-btn');
    if (pipelineBtn) {
      pipelineBtn.disabled = !hasPdf || pipelineBusy;
      pipelineBtn.textContent = preprocessPipelineBusy ? '预处理中…' : '一键预处理';
    }
    const volumeOcrBtn = $('volume-ocr-btn');
    if (volumeOcrBtn) {
      const ocrTargets = lessonsWithPageImages(d);
      const canOcr = ocrTargets.length > 0 && !preprocessPipelineBusy;
      volumeOcrBtn.disabled = !canOcr || volumeOcrBusy;
      volumeOcrBtn.textContent = volumeOcrBusy ? '整册 OCR+建块 中…' : '整册 OCR+建块';
      volumeOcrBtn.title = '对有页图的课时依次：整课 OCR → 语义/栏目建块（可离开本页）';
    }
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

  function stopParsePolling() {
    if (parsePollTimer) {
      clearInterval(parsePollTimer);
      parsePollTimer = null;
    }
  }

  function startParsePolling() {
    if (parsePollTimer) clearInterval(parsePollTimer);
    parsePollTimer = setInterval(async () => {
      if (!currentCode) {
        stopParsePolling();
        return;
      }
      try {
        const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}`);
        const d = await r.json();
        if (!d.ok) return;
        lastVolumeData = d;
        if (d.parse_status === 'done') {
          stopParsePolling();
          renderVolume(d);
          toast('页码划分完成');
        } else if (d.parse_status === 'failed') {
          stopParsePolling();
          renderVolume(d);
          toast('划分失败：' + (d.parse_error || ''));
        } else if (d.parse_status !== 'processing') {
          stopParsePolling();
        } else {
          renderVolume(d);
        }
      } catch (e) {
        /* ignore */
      }
    }, 3000);
  }

  function bindLessonEvents() {
    document.querySelectorAll('#lesson-rows .page-preview-btn').forEach((btn) => {
      btn.onclick = () => openPreview(btn.dataset.lesson);
    });
    document.querySelectorAll('.page-save-btn').forEach((btn) => {
      btn.onclick = () => savePageRange(btn.dataset.lesson);
    });
    document.querySelectorAll('.page-start-input, .page-end-input').forEach((inp) => {
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
    const s = parseInt(start.value, 10);
    const e = parseInt(end.value, 10);
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
    if (!range) {
      toast('请填写有效起止页码');
      return;
    }
    const btn = document.querySelector(`.page-save-btn[data-lesson="${uid}"]`);
    if (btn) {
      btn.disabled = true;
      btn.textContent = '保存中…';
    }
    try {
      const r = await fetch(`${LESSON_API}/lessons/${uid}/page-range`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...range, rebuild_pages: true }),
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '保存失败');
      toast('已保存');
      markDirty(uid, false);
      await loadVolume();
    } catch (e) {
      toast(e.message || String(e));
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = '保存';
      }
    }
  }

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
    ov.classList.remove('is-open');
    ov.setAttribute('aria-hidden', 'true');
    previewOpenLessonUid = null;
    unlockScroll();
  }

  function openPreviewShell() {
    const ov = $('page-preview');
    if (!ov) return;
    if (!ov.classList.contains('is-open')) lockScroll();
    ov.classList.add('is-open');
    ov.setAttribute('aria-hidden', 'false');
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
    if (!range) {
      grid.innerHTML = '<p class="preview-empty">请先填写有效起止页码</p>';
      return;
    }
    try {
      const qs = new URLSearchParams({
        page_start: String(range.page_start),
        page_end: String(range.page_end),
      });
      const r = await fetch(`${LESSON_API}/lessons/${uid}/pages?${qs}`);
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '加载失败');
      title.textContent = `${d.lesson_name}（PDF ${d.page_start}–${d.page_end}）`;
      if (!d.pages?.length) {
        grid.innerHTML = '<p class="preview-empty">无页图</p>';
        return;
      }
      if (d.pages.length <= 1) grid.className = 'preview-grid preview-grid--one';
      else if (d.pages.length === 2) grid.className = 'preview-grid preview-grid--two';
      else grid.className = 'preview-grid preview-grid--draft';
      grid.innerHTML = '<p class="hint-inline preview-grid-hint">点击图片可放大查看</p>' + d.pages.map((p) => `
        <div class="preview-card">
          <img src="${p.url}" alt="p${p.pdf_page ?? p.page_index}" onclick="this.classList.toggle('is-zoomed')">
          <div>PDF p${p.pdf_page ?? p.page_index}</div>
        </div>`).join('');
    } catch (e) {
      grid.innerHTML = `<p class="preview-empty">${escapeHtml(e.message)}</p>`;
    }
  }

  async function uploadPdf() {
    if (!currentCode || !selectedPdfFile) {
      toast('请先选择 PDF');
      return;
    }
    const btn = $('upload-btn');
    const hint = $('pdf-upload-hint');
    if (btn) {
      btn.disabled = true;
      btn.textContent = '上传中…';
    }
    if (hint) {
      hint.hidden = false;
      hint.textContent = '正在上传…';
    }
    try {
      const fd = new FormData();
      fd.append('file', selectedPdfFile);
      const r = await fetch(`${API}/volumes/${encodeURIComponent(currentCode)}/pdf`, {
        method: 'POST',
        body: fd,
      });
      const d = await r.json();
      if (!d.ok) throw new Error(d.error || '上传失败');
      toast(d.pdf_replaced ? 'PDF 已替换，正在重新预处理…' : 'PDF 上传成功');
      selectedPdfFile = null;
      const input = $('pdf-input');
      if (input) input.value = '';
      await loadVolume();
      await runFullPreprocessPipeline({ force: true });
    } catch (e) {
      toast(e.message || String(e));
      refreshUploadButton(lastVolumeData);
    } finally {
      if (hint) hint.hidden = true;
    }
  }

  const pdfInput = $('pdf-input');
  if (pdfInput) {
    pdfInput.addEventListener('change', () => {
      selectedPdfFile = pdfInput.files && pdfInput.files[0] ? pdfInput.files[0] : null;
      refreshUploadButton(lastVolumeData);
    });
  }

  const uploadBtn = $('upload-btn');
  if (uploadBtn) uploadBtn.addEventListener('click', uploadPdf);

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

  const pipelineBtn = $('pipeline-btn');
  if (pipelineBtn) {
    pipelineBtn.addEventListener('click', async () => {
      if (!currentCode || preprocessPipelineBusy) return;
      await runFullPreprocessPipeline({ force: true });
    });
  }

  const volumeOcrBtn = $('volume-ocr-btn');
  if (volumeOcrBtn) {
    volumeOcrBtn.addEventListener('click', () => {
      if (!currentCode || volumeOcrBusy || preprocessPipelineBusy) return;
      runVolumeOcr();
    });
  }

  const previewClose = $('preview-close');
  if (previewClose) previewClose.addEventListener('click', closePreview);
  const previewOverlay = $('page-preview');
  if (previewOverlay) {
    previewOverlay.addEventListener('click', (e) => {
      if (e.target === previewOverlay) closePreview();
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const ov = $('page-preview');
      if (ov && ov.classList.contains('is-open')) closePreview();
    }
  });

  if (currentCode) loadVolume();
})();
