(function () {
  const API = '/api/textbook-diff';
  const subjectId = window.diffResolveActiveSubjectId
    ? window.diffResolveActiveSubjectId('huaxue')
    : 'huaxue';
  if (window.diffSaveSubjectId) window.diffSaveSubjectId(subjectId);
  const subject = (window.diffSubjectById && window.diffSubjectById(subjectId))
    || { id: 'huaxue', label: '化学', editions: [{ id: 'renjiao', label: '人教版' }] };

  function isSandboxSubject() {
    if (typeof window.diffIsSandboxSubjectId === 'function') {
      return window.diffIsSandboxSubjectId(subjectId);
    }
    return subjectId === 'test' || subjectId === 'xiaoke'
      || subject.label === 'Test' || subject.label === '小科';
  }

  function isXiaokeSubject() {
    return subjectId === 'xiaoke' || subject.label === '小科';
  }

  // 沙箱学科：演示页隐藏红框区内容；按钮仍在 DOM，事件与一键串联逻辑不变
  if (isSandboxSubject()) {
    document.body.classList.add('wb-test-hide-chrome');
  }
  if (isXiaokeSubject()) {
    document.body.classList.add('wb-xiaoke');
    const nav = document.getElementById('wb-phase-batch-nav');
    if (nav) nav.hidden = false;
  }

  let current = { old_code: '', new_code: '', detail: null };
  /** Test 两侧独立进度：互不覆盖 */
  const sideJobUi = {
    old: { busy: false, status: '', phase: '', error: false, track: '' },
    new: { busy: false, status: '', phase: '', error: false, track: '' },
  };
  /** Test：按最近一次上传选择锁定图2清单 full | draft */
  const sideViewMode = { old: 'full', new: 'full' };
  let pipelinePollTimer = null;
  let pipelineSeenDone = new Set(); // job_id 已提示完成
  let pipelineSeeded = false;
  let pipelinePollGen = 0; // 防止过期 status 请求关掉轮询 / 重绘表格
  let pipelineHadRunning = false;
  /** 上一轮整册/课时任务快照（用于失败重启） */
  let lastPipelineSnap = null;

  const params = new URLSearchParams(window.location.search);
  const deepOld = params.get('old_code');
  const deepNew = params.get('new_code');
  const isPairPage = !!(deepOld && deepNew);

  function xiaokeEditionIdFromVolumeCode(code) {
    const u = String(code || '').toUpperCase();
    if (!u || !isXiaokeSubject()) return null;
    let best = null;
    for (const e of (subject.editions || [])) {
      const p = String(e.code_prefix || '').toUpperCase();
      if (!p) continue;
      if (u === p || u.startsWith(`${p}-`)) {
        if (!best || p.length > best.len) best = { id: e.id, len: p.length };
      }
    }
    return best ? best.id : null;
  }

  function resolveXiaokeEditionId() {
    if (!isXiaokeSubject()) return null;
    const fromUrl = params.get('edition');
    if (fromUrl) {
      const hit = (subject.editions || []).find(
        (e) => e.id === fromUrl || e.label === fromUrl,
      );
      if (hit) return hit.id;
      return fromUrl;
    }
    return xiaokeEditionIdFromVolumeCode(deepNew || deepOld);
  }

  function workbookListUrl() {
    const q = new URLSearchParams(
      window.diffSubjectQuery
        ? window.diffSubjectQuery(subjectId)
        : `subject=${encodeURIComponent(subjectId)}`,
    );
    if (isXiaokeSubject()) {
      const ed = resolveXiaokeEditionId();
      if (ed) q.set('edition', ed);
    }
    return `/textbook-diff/workbook?${q.toString()}`;
  }

  function workbookPairUrl(oldCode, newCode, editionId) {
    const q = new URLSearchParams();
    q.set('subject', subjectId);
    q.set('old_code', oldCode);
    q.set('new_code', newCode);
    const ed = editionId
      || resolveXiaokeEditionId()
      || xiaokeEditionIdFromVolumeCode(newCode || oldCode);
    if (ed) q.set('edition', ed);
    return `/textbook-diff/workbook?${q.toString()}`;
  }

  const lead = document.getElementById('wb-lead');
  if (lead) {
    lead.textContent = isPairPage
      ? `${subject.label} — 本册工作页：预处理 → 粗分 → 对比`
      : `${subject.label} — 整册预处理 → 课时粗分 → 逐课对比`;
  }
  const back = document.getElementById('wb-back-home');
  if (back) {
    if (isPairPage) {
      back.href = workbookListUrl();
      back.textContent = '← 返回本册列表';
    } else if (window.diffSubjectQuery) {
      back.href = `/textbook-diff/?${window.diffSubjectQuery(subjectId)}`;
    }
  }
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('subject', subjectId);
    if (isPairPage && isXiaokeSubject()) {
      const ed = resolveXiaokeEditionId();
      if (ed) url.searchParams.set('edition', ed);
    }
    window.history.replaceState({}, '', url.pathname + '?' + url.searchParams.toString());
  } catch (e) { /* ignore */ }

  const editionSel = document.getElementById('f-edition');
  if (editionSel) {
    (subject.editions || []).forEach((e) => {
      const opt = document.createElement('option');
      opt.value = e.label;
      opt.textContent = e.label;
      editionSel.appendChild(opt);
    });
  }
  const gradeInput = document.getElementById('f-grade');
  if (gradeInput) {
    const defaultGrade = { huaxue: '9', yuwen: '7', shuxue: '8', kexue: '5', test: '9', xiaoke: '5' };
    gradeInput.value = defaultGrade[subject.id] || '7';
  }

  function toast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, 4000);
  }

  async function readJson(r) {
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function sideModeStorageKey(sideKey, volumeCode) {
    return `textbook_diff_test_view_${sideKey}_${volumeCode || ''}`;
  }

  function loadSideViewMode(sideKey, volumeCode, side) {
    try {
      const saved = localStorage.getItem(sideModeStorageKey(sideKey, volumeCode));
      if (saved === 'full' || saved === 'draft') return saved;
    } catch (e) { /* ignore */ }
    if (side && side.draft_only) return 'draft';
    if (side && side.has_preview_pdf && !side.has_pdf) return 'draft';
    return 'full';
  }

  function setSideViewMode(sideKey, mode, volumeCode) {
    if (mode !== 'full' && mode !== 'draft') return;
    sideViewMode[sideKey] = mode;
    if (sideJobUi[sideKey]) sideJobUi[sideKey].track = mode;
    try {
      if (volumeCode) localStorage.setItem(sideModeStorageKey(sideKey, volumeCode), mode);
    } catch (e) { /* ignore */ }
  }

  function statusItems(side, { mode } = {}) {
    const s = side || {};
    const view = mode === 'draft' || mode === 'full'
      ? mode
      : (s.draft_only || (s.has_preview_pdf && !s.has_pdf) ? 'draft' : 'full');
    const rows = view === 'draft'
      ? [
          ['修订版 PDF', !!s.has_preview_pdf],
          [
            `印刷页码 ${s.draft_page_mapped || 0}/${s.draft_page_count || 0}`,
            (s.draft_page_mapped || 0) > 0,
          ],
          [
            `修订页图 ${s.draft_pages_count || 0}`,
            !!s.draft_pages_ready,
          ],
        ]
      : [
          ['定稿 PDF', s.has_pdf],
          [`目录课时 ${s.lesson_count || 0}`, s.catalog_ready],
          [
            `页码划分 ${s.lessons_with_page_range || 0}/${s.lesson_count || 0}`,
            s.page_range_ready,
          ],
          [`页图 ${s.page_image_count || 0}`, s.page_images_ready],
        ];
    return rows.map(([lab, ok]) =>
      `<li><span>${lab}</span><span class="${ok ? 'ok' : 'no'}">${ok ? '✓' : '待完成'}</span></li>`
    ).join('');
  }

  function renderSide(el, sideKey, side) {
    const isOld = sideKey === 'old';
    const code = side.volume_code || '';
    const intakePath = isOld ? 'old' : 'new';
    const q = window.diffSubjectQuery ? `&${window.diffSubjectQuery(subjectId)}` : '';
    const sideName = isOld ? '旧' : '新';
    const job = sideJobUi[sideKey] || {};
    const jobBusy = !!job.busy;
    const sandbox = isSandboxSubject();
    const xiaokeOld = isXiaokeSubject() && isOld;
    const viewMode = sandbox
      ? (sideViewMode[sideKey] || loadSideViewMode(sideKey, code, side))
      : '';
    const showProgress = !!(jobBusy || (job.status && String(job.status).trim()));
    const progressHtml = showProgress
      ? `<div class="side-job-progress${job.error ? ' is-error' : ''}${jobBusy ? ' is-busy' : ''}" data-side-progress="${sideKey}">
          <span class="side-job-dot" aria-hidden="true"></span>
          <span class="side-job-text">${esc(job.status || '处理中…')}</span>
        </div>`
      : '';
    let modeHint = '';
    if (xiaokeOld) {
      modeHint = '<p class="hint side-mode-hint">小科不上传旧 PDF：请先在旧库建设对应册次点「载入基准目录」，粗分才可用。</p>';
    } else if (sandbox) {
      modeHint = `<p class="hint side-mode-hint">${viewMode === 'draft' ? '当前清单：不完整修订版（无目录识别）' : '当前清单：完整版（目录 → 页码 → 页图）'}</p>`;
    }
    let cta = '';
    if (xiaokeOld) {
      cta = '<a class="side-cta" href="/old-library/">去旧库载入基准目录 →</a>';
    } else if (sandbox) {
      cta = `<button type="button" class="side-cta" data-test-upload-side="${sideKey}" data-volume-code="${esc(code)}"${(jobBusy || (window.WorkbookTestUpload && window.WorkbookTestUpload.isRecognizeBusy())) ? ' disabled' : ''}>
          ${jobBusy ? `${sideName}侧处理中…` : `上传${sideName}侧 PDF →`}
        </button>`;
    } else {
      cta = `<a class="side-cta" href="/textbook-diff/${intakePath}/intake?code=${encodeURIComponent(code)}${q}">
          进入${sideName}侧预处理 →
        </a>`;
    }
    const shelfAdd = isChemShelfSubject()
      ? `<button type="button" class="btn-shelf-add" data-shelf-add="${isOld ? 'old' : 'new'}">上架${sideName}教材</button>`
      : '';
    el.innerHTML = `
      <h3>${isOld ? '旧教材' : '新教材'} · ${side.display_title || code}</h3>
      ${modeHint}
      <ul class="workbook-status-list">${statusItems(side, { mode: sandbox ? viewMode : undefined })}</ul>
      ${progressHtml}
      <div class="workbook-side-actions">${cta}${shelfAdd}</div>`;
    if (sandbox && !xiaokeOld) {
      el.querySelector('[data-test-upload-side]')?.addEventListener('click', () => {
        openTestUploadModal(sideKey, code);
      });
    }
    el.querySelector('[data-shelf-add]')?.addEventListener('click', (ev) => {
      const role = ev.currentTarget.getAttribute('data-shelf-add');
      chemShelfAdd(role);
    });
  }

  function sideHasUploadForMode(side, mode) {
    if (!side) return false;
    if (mode === 'draft') return !!side.has_preview_pdf;
    return !!side.has_pdf;
  }

  let xiaokeRecognizeBusy = false;
  let xiaokeParseBusy = false;

  function newSideHasCatalog(detail) {
    const n = detail && detail.new;
    return !!(n && ((n.lesson_count || 0) > 0 || (n.lessons || []).length > 0));
  }

  function newSideParseDone(detail) {
    const n = detail && detail.new;
    if (!n) return false;
    if (n.parse_status === 'done') {
      return (n.lessons_with_page_range || 0) > 0
        || (n.lessons || []).some((l) => l.page_start);
    }
    return false;
  }

  function updateRecognizeBar() {
    const xiaokeRoot = document.getElementById('xiaoke-new-intake');
    const block = document.getElementById('wb-preprocess-block');
    const bar = document.getElementById('wb-recognize-bar');
    const btn = document.getElementById('btn-one-click-recognize');
    const preprocessTitle = document.getElementById('wb-preprocess-title');
    const hint = document.getElementById('wb-recognize-hint');
    const btnClearCache = document.getElementById('btn-test-clear-cache');
    const btnClearDb = document.getElementById('btn-test-clear-db');
    const btnFlushDb = document.getElementById('btn-test-flush-db');
    const sideNew = document.getElementById('side-new');

    // 小科：新库建设同款「上传 + 整册预处理」，不再用一键识别条
    if (isXiaokeSubject()) {
      if (block) block.hidden = true;
      if (sideNew) sideNew.hidden = true;
      if (xiaokeRoot && window.WorkbookXiaokeIntake) {
        window.WorkbookXiaokeIntake.render();
      } else if (xiaokeRoot) {
        xiaokeRoot.hidden = false;
      }
      return;
    }

    if (xiaokeRoot) xiaokeRoot.hidden = true;
    if (sideNew) sideNew.hidden = false;
    if (!bar || !btn) return;
    if (!isSandboxSubject()) {
      if (block) block.hidden = true;
      return;
    }
    if (block) block.hidden = false;
    if (preprocessTitle) preprocessTitle.hidden = true;

    const pairReady = !!(current.old_code && current.new_code);
    const recognizing = !!(window.WorkbookTestUpload && window.WorkbookTestUpload.isRecognizeBusy());
    if (btnClearCache) btnClearCache.disabled = !pairReady || recognizing;
    if (btnClearDb) btnClearDb.disabled = !pairReady || recognizing;
    if (btnFlushDb) btnFlushDb.disabled = !pairReady || recognizing;
    const d = current.detail;
    const oldMode = sideViewMode.old || 'full';
    const newMode = sideViewMode.new || 'full';
    const oldOk = d ? sideHasUploadForMode(d.old, oldMode) : false;
    const newOk = d ? sideHasUploadForMode(d.new, newMode) : false;
    if (!d) {
      btn.disabled = true;
      if (hint) {
        hint.textContent = '请先完成旧、新两侧 PDF 上传，再点一键识别（依次旧→新）';
      }
      return;
    }
    const uploading = !!(sideJobUi.old.busy || sideJobUi.new.busy);
    const ready = oldOk && newOk;
    btn.disabled = !ready || uploading || recognizing;
    btn.textContent = recognizing ? '识别中…' : '一键识别';
    if (hint) {
      if (recognizing) {
        hint.textContent = '正在依次识别旧侧 → 新侧…';
      } else if (uploading) {
        hint.textContent = '有一侧仍在上传，请稍候';
      } else if (!oldOk && !newOk) {
        hint.textContent = '请先上传旧、新两侧 PDF，再点一键识别（依次旧→新）';
      } else if (!oldOk) {
        hint.textContent = `请先上传旧侧${oldMode === 'draft' ? '修订版' : '完整版'} PDF`;
      } else if (!newOk) {
        hint.textContent = `请先上传新侧${newMode === 'draft' ? '修订版' : '完整版'} PDF`;
      } else {
        const fmt = window.WorkbookTestUpload && window.WorkbookTestUpload.formatDuration;
        const etaFull = (window.WorkbookTestUpload && window.WorkbookTestUpload.ETA_SEC)
          ? (window.WorkbookTestUpload.ETA_SEC.full.catalog
            + window.WorkbookTestUpload.ETA_SEC.full.parse
            + window.WorkbookTestUpload.ETA_SEC.full.pages)
          : 660;
        const etaDraft = (window.WorkbookTestUpload && window.WorkbookTestUpload.ETA_SEC)
          ? (window.WorkbookTestUpload.ETA_SEC.draft.map + window.WorkbookTestUpload.ETA_SEC.draft.pages)
          : 600;
        const rough = (oldMode === 'draft' ? etaDraft : etaFull)
          + (newMode === 'draft' ? etaDraft : etaFull);
        const roughLabel = fmt ? fmt(rough) : `${Math.round(rough / 60)} 分钟`;
        hint.textContent =
          `两侧已上传，可点一键识别（识别完成后自动接粗分→整册对比；经验预计识别约 ${roughLabel}）`;
      }
    }
  }

  function applyRecognizeEta(payload) {
    const eta = document.getElementById('wb-recognize-eta');
    if (!eta) return;
    const text = payload.label || payload.message || '';
    if (!text) {
      if (!payload.busy) return;
      eta.hidden = true;
      return;
    }
    eta.hidden = false;
    eta.textContent = text;
    eta.classList.toggle('is-done', payload.phase === 'done');
    eta.classList.toggle('is-error', payload.phase === 'error');
    updateRecognizeBar();
  }

  async function startXiaokeCatalogRecognize() {
    if (!current.detail || !current.old_code || !current.new_code) {
      toast('本册未就绪');
      return;
    }
    if (!sideHasUploadForMode(current.detail.new, 'full')) {
      toast('请先上传新侧完整版 PDF');
      return;
    }
    if (xiaokeRecognizeBusy || xiaokeParseBusy) {
      toast('识别进行中，请稍候');
      return;
    }
    const needParse = !newSideParseDone(current.detail);
    if (!confirm(
      '将执行新侧一键预处理（仅小科本页）：\n'
      + '① 识别新侧目录\n'
      + '② 与旧库目录粗分\n'
      + (needParse
        ? '③ 划分新侧页码（约 2～5 分钟）\n\n'
        : '③ 页码已划分，将跳过本步\n\n')
      + '不生成页图、不跑整册对比。继续？'
    )) return;

    xiaokeRecognizeBusy = true;
    updateRecognizeBar();
    applyRecognizeEta({
      busy: true,
      phase: 'catalog',
      label: '正在识别新侧目录…',
    });
    applySideProgress('new', {
      busy: true,
      phase: 'catalog',
      track: 'full',
      status: '识别目录中…',
    });
    try {
      const r = await fetch(`${API}/workbook/xiaoke/recognize-catalog`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: current.old_code,
          new_code: current.new_code,
          replace: true,
          compare: true,
        }),
      });
      const d = await readJson(r);
      applyRecognizeEta({
        busy: true,
        phase: 'coarse',
        label: '目录已写入，正在刷新对比结果…',
      });
      await refreshPairAfterRecognize();
      applySideProgress('new', {
        busy: true,
        phase: 'catalog',
        track: 'full',
        status: '目录识别与粗分完成，准备划分页码…',
      });
      toast(d.message || '目录识别与粗分完成');

      xiaokeRecognizeBusy = false;
      if (needParse || !newSideParseDone(current.detail)) {
        await runXiaokeParsePages({ skipConfirm: true });
      } else {
        applySideProgress('new', {
          busy: false,
          phase: 'done',
          track: 'full',
          status: '新侧预处理完成',
        });
        applyRecognizeEta({
          busy: false,
          phase: 'done',
          label: d.message || '新侧预处理完成',
        });
      }
    } catch (e) {
      const msg = e.message || String(e);
      applySideProgress('new', {
        busy: false,
        phase: 'error',
        track: 'full',
        status: `识别失败：${msg}`,
        error: true,
      });
      applyRecognizeEta({
        busy: false,
        phase: 'error',
        label: `识别中断：${msg}`,
        message: msg,
      });
      toast(msg);
    } finally {
      xiaokeRecognizeBusy = false;
      updateRecognizeBar();
    }
  }

  async function waitXiaokeParseDone(volumeCode) {
    const maxMs = 20 * 60 * 1000;
    const started = Date.now();
    while (Date.now() - started < maxMs) {
      await new Promise((r) => setTimeout(r, 3000));
      const r = await fetch(`${API}/volumes/${encodeURIComponent(volumeCode)}`);
      const d = await readJson(r);
      const waited = Math.round((Date.now() - started) / 1000);
      applyRecognizeEta({
        busy: true,
        phase: 'parse',
        label: `划分页码进行中（已等待 ${waited} 秒）…`,
      });
      applySideProgress('new', {
        busy: true,
        phase: 'parse',
        track: 'full',
        status: `划分页码中（已等待 ${waited} 秒）…`,
      });
      if (d.parse_status === 'done') return d;
      if (d.parse_status === 'failed') {
        throw new Error(d.parse_error || d.error || '划分页码失败');
      }
      if (d.parse_status !== 'processing') {
        throw new Error(d.parse_error || '划分状态异常，请刷新后重试');
      }
    }
    throw new Error('划分仍在进行，请稍后刷新查看结果');
  }

  async function runXiaokeParsePages({ skipConfirm = false } = {}) {
    if (!isXiaokeSubject()) return;
    if (!current.detail || !current.new_code) {
      toast('本册未就绪');
      return;
    }
    if (!sideHasUploadForMode(current.detail.new, 'full')) {
      toast('请先上传新侧完整版 PDF');
      return;
    }
    if (!newSideHasCatalog(current.detail)) {
      toast('请先完成目录识别');
      return;
    }
    if (xiaokeParseBusy || xiaokeRecognizeBusy) {
      toast('有任务进行中，请稍候');
      return;
    }
    const rerun = newSideParseDone(current.detail);
    if (!skipConfirm) {
      if (!confirm(
        rerun
          ? '将重新划分新侧页码（保留目录，约 2～5 分钟）。继续？'
          : '将对新侧 PDF 划分页码（校准目录印刷页 ↔ PDF 物理页，约 2～5 分钟）。\n'
            + '完成后可进入对比查看新侧页图。继续？'
      )) return;
    }

    xiaokeParseBusy = true;
    updateRecognizeBar();
    applyRecognizeEta({
      busy: true,
      phase: 'parse',
      label: '正在启动划分页码…',
    });
    applySideProgress('new', {
      busy: true,
      phase: 'parse',
      track: 'full',
      status: '划分页码中…',
    });
    try {
      const r = await fetch(`${API}/workbook/xiaoke/parse-pages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_code: current.new_code }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false) {
        throw new Error(d.error || `HTTP ${r.status}`);
      }
      if (d.parse_status === 'done' && d.already_done) {
        await refreshPairAfterRecognize();
        applySideProgress('new', {
          busy: false,
          phase: 'done',
          track: 'full',
          status: d.message || '页码已划分',
        });
        applyRecognizeEta({
          busy: false,
          phase: 'done',
          label: d.message || '页码已划分',
        });
        toast(d.message || '页码已划分');
        return;
      }
      const done = await waitXiaokeParseDone(current.new_code);
      await refreshPairAfterRecognize();
      const ranged = (done.lessons || []).filter((l) => l.page_start).length;
      const total = (done.lessons || []).length;
      const msg = `新侧页码划分完成（${ranged}/${total}）`;
      applySideProgress('new', {
        busy: false,
        phase: 'done',
        track: 'full',
        status: msg,
      });
      applyRecognizeEta({
        busy: false,
        phase: 'done',
        label: msg,
      });
      toast(msg);
    } catch (e) {
      const msg = e.message || String(e);
      applySideProgress('new', {
        busy: false,
        phase: 'error',
        track: 'full',
        status: `划分失败：${msg}`,
        error: true,
      });
      applyRecognizeEta({
        busy: false,
        phase: 'error',
        label: `划分中断：${msg}`,
        message: msg,
      });
      toast(msg);
      try {
        await refreshPairAfterRecognize();
      } catch (_) { /* ignore */ }
    } finally {
      xiaokeParseBusy = false;
      updateRecognizeBar();
    }
  }

  async function startXiaokeParsePages() {
    await runXiaokeParsePages({ skipConfirm: false });
  }

  async function startOneClickRecognize() {
    if (isXiaokeSubject()) {
      await startXiaokeCatalogRecognize();
      return;
    }
    if (!window.WorkbookTestUpload) {
      toast('识别脚本未加载');
      return;
    }
    if (!current.detail || !current.old_code || !current.new_code) {
      toast('本册未就绪');
      return;
    }
    const sides = [
      { sideKey: 'old', volumeCode: current.old_code, track: sideViewMode.old || 'full' },
      { sideKey: 'new', volumeCode: current.new_code, track: sideViewMode.new || 'full' },
    ];
    const d = current.detail;
    for (const s of sides) {
      const side = s.sideKey === 'old' ? d.old : d.new;
      if (!sideHasUploadForMode(side, s.track)) {
        toast(`${s.sideKey === 'old' ? '旧' : '新'}侧尚未上传对应 PDF`);
        return;
      }
    }
    const chainVolume = isSandboxSubject() && !isDraftCoarse(current.detail);
    if (!confirm(
      chainVolume
        ? '将依次执行：\n① 一键识别（旧→新）\n② 课时粗分\n③ 整册对比\n\n三步串行。可离开本页，后续任务在后台继续。继续？'
        : '将依次识别旧侧 → 新侧。继续？'
    )) return;
    updateRecognizeBar();
    const ok = await window.WorkbookTestUpload.runRecognizeBoth({
      sides,
      toast,
      onProgress: (sk, payload) => applySideProgress(sk, payload),
      onDone: async (sk) => {
        await refreshOneSide(sk);
        updateRecognizeBar();
      },
      onEta: applyRecognizeEta,
      onFinished: () => {
        updateRecognizeBar();
      },
    });
    await refreshOneSide('old');
    await refreshOneSide('new');
    updateRecognizeBar();
    if (!ok) return;
    if (chainVolume && !isDraftCoarse(current.detail)) {
      toast('识别完成，开始整册一键（粗分→对比）…');
      await startTestVolumeCombo({ skipConfirm: true });
    }
  }

  function applySideProgress(sideKey, payload) {
    const job = sideJobUi[sideKey];
    if (!job) return;
    job.busy = !!payload.busy;
    job.status = payload.status || '';
    job.phase = payload.phase || '';
    job.error = !!payload.error;
    if (payload.track === 'full' || payload.track === 'draft') {
      job.track = payload.track;
      const code = sideKey === 'old' ? current.old_code : current.new_code;
      setSideViewMode(sideKey, payload.track, code);
    }

    const side = current.detail
      ? (sideKey === 'old' ? current.detail.old : current.detail.new)
      : null;
    const el = document.getElementById(sideKey === 'old' ? 'side-old' : 'side-new');
    if (el && side) {
      renderSide(el, sideKey, side);
    }
    updateRecognizeBar();
  }

  async function refreshOneSide(sideKey) {
    if (!current.old_code || !current.new_code) return;
    try {
      const r = await fetch(
        `${API}/workbook/pair?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
      );
      const d = await readJson(r);
      current.detail = d;
      if (isXiaokeSubject() && sideKey === 'old') {
        await renderXiaokeOldCatalog(d);
      } else {
        const side = sideKey === 'old' ? d.old : d.new;
        const el = document.getElementById(sideKey === 'old' ? 'side-old' : 'side-new');
        if (el) renderSide(el, sideKey, side || {});
      }
      updateCoarseChrome(d);
      updateRecognizeBar();
    } catch (e) {
      toast(e.message || String(e));
    }
  }

  /** 识别/粗分后整页刷新：小科旧侧保持旧库目录面板 */
  async function refreshPairAfterRecognize() {
    if (!current.old_code || !current.new_code) return;
    const r = await fetch(
      `${API}/workbook/pair?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
    );
    const d = await readJson(r);
    current.detail = d;
    if (isXiaokeSubject()) {
      await renderXiaokeOldCatalog(d);
    } else {
      renderSide(document.getElementById('side-old'), 'old', d.old || {});
      renderSide(document.getElementById('side-new'), 'new', d.new || {});
    }
    renderCoarse(d);
    updateRecognizeBar();
  }

  function openTestUploadModal(sideKey, code) {
    if (!window.WorkbookTestUpload) {
      toast('上传弹窗脚本未加载');
      return;
    }
    if (window.WorkbookTestUpload.isSideBusy(sideKey)) {
      toast(`${sideKey === 'old' ? '旧' : '新'}侧正在处理中`);
      return;
    }
    window.WorkbookTestUpload.bindOnce();
    const isOld = sideKey === 'old';
    const xiaoke = isXiaokeSubject();
    window.WorkbookTestUpload.open({
      sideKey,
      volumeCode: code,
      sideLabel: isOld ? '旧教材' : '新教材',
      toast,
      onProgress: (sk, payload) => applySideProgress(sk, payload),
      onDone: async (sk) => {
        // 只刷新本侧，另一侧并发任务不受影响
        await refreshOneSide(sk);
        if (xiaoke) updateRecognizeBar();
        // 小科新侧上传后自动串联：单册预处理（#xk-preprocess-card 动画）→ 整册一键
        if (xiaoke && sk === 'new' && window.WorkbookXiaokeIntake) {
          toast('上传完成，开始自动整册预处理…');
          try {
            await window.WorkbookXiaokeIntake.runPipeline();
            // 预处理完成后自动接整册一键（OCR → 合成比对 → 环节整理）
            if (current.old_code && current.new_code) {
              toast('预处理完成，启动整册一键…');
              startXiaokeVolumeFull({ skipConfirm: true }).catch((e) => {
                toast(`整册一键启动失败：${e.message || e}`);
              });
            }
          } catch (e) {
            toast(`自动预处理失败：${e.message || e}`);
          }
        }
      },
      ...(xiaoke
        ? {
            pendingStatusFull: '完整版已上传，正在自动预处理…',
            pendingStatusDraft: '修订版已上传（小科目录识别请用完整版）',
            uploadDoneToast: '新侧 PDF 已上传，已自动开始整册预处理',
          }
        : {}),
    });
  }

  function lessonLabel(les) {
    if (!les) return '';
    if (les.lesson_label) return String(les.lesson_label);
    const no = String(les.lesson_no || '').trim();
    const name = String(les.lesson_name || '').trim();
    if (no && name) return `${no} ${name}`;
    return name || no || '';
  }

  function newLessonPageOffsetHtml(n) {
    const uid = String(n.lesson_uid || '').trim();
    const unit = n.unit_title || '';
    const ps = Number(n.page_start || 0);
    const pe = Number(n.page_end || n.page_start || 0);
    if (!uid || ps < 1) {
      return `<span class="hint">${esc(unit)} · p?-?</span>`;
    }
    const range = pe && pe !== ps ? `p${ps}–${pe}` : `p${ps}`;
    return `<span class="hint">${esc(unit)}</span>`
      + `<span class="wb-page-offset" data-lesson-uid="${esc(uid)}" data-page-start="${ps}" data-page-end="${pe || ps}">`
      + `<button type="button" class="wb-page-offset-btn" data-delta="-1" title="本课页码前移 1 页"${ps <= 1 ? ' disabled' : ''}>−</button>`
      + `<span class="wb-page-offset-range">${esc(range)}</span>`
      + `<button type="button" class="wb-page-offset-btn" data-delta="1" title="本课页码后移 1 页">+</button>`
      + `</span>`;
  }

  function volumePageOffsetHeaderHtml(items) {
    const ranged = (items || []).filter((it) => Number((it.new || {}).page_start) >= 1);
    if (!ranged.length) return '新教材课时';
    const minStart = Math.min(...ranged.map((it) => Number(it.new.page_start)));
    return '新教材课时'
      + `<span class="wb-page-offset wb-page-offset--volume" title="整册新侧已划分课时一起平移">`
      + `<span class="wb-page-offset-kicker">整册偏移</span>`
      + `<button type="button" class="wb-page-offset-btn" data-volume-delta="-1" title="整册页码前移 1 页"${minStart <= 1 ? ' disabled' : ''}>−</button>`
      + `<button type="button" class="wb-page-offset-btn" data-volume-delta="1" title="整册页码后移 1 页">+</button>`
      + `</span>`;
  }

  async function reloadCoarseAfterPageOffset() {
    if (!current.old_code || !current.new_code) return;
    const r = await fetch(
      `${API}/workbook/pair?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
    );
    const d = await readJson(r);
    current.detail = d;
    renderCoarse(d);
  }

  async function shiftLessonPageOffset(uid, start, end, delta) {
    const nextStart = Number(start) + delta;
    const nextEnd = Number(end) + delta;
    if (nextStart < 1) throw new Error('起始页不能小于 1');
    const r = await fetch(`${API}/lessons/${encodeURIComponent(uid)}/page-range`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        page_start: nextStart,
        page_end: nextEnd,
        rebuild_pages: false,
      }),
    });
    return readJson(r);
  }

  async function shiftVolumePageOffset(delta) {
    if (!current.new_code) throw new Error('缺少新侧册次');
    const r = await fetch(`${API}/volumes/${encodeURIComponent(current.new_code)}/page-offset`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ delta }),
    });
    return readJson(r);
  }

  function bindPageOffsetControls(wrap) {
    wrap.querySelectorAll('.wb-page-offset[data-lesson-uid] .wb-page-offset-btn').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const box = btn.closest('.wb-page-offset');
        const uid = box?.dataset.lessonUid;
        const delta = Number(btn.dataset.delta || 0);
        if (!uid || !delta) return;
        const start = Number(box.dataset.pageStart);
        const end = Number(box.dataset.pageEnd);
        wrap.querySelectorAll('.wb-page-offset-btn').forEach((b) => { b.disabled = true; });
        try {
          const d = await shiftLessonPageOffset(uid, start, end, delta);
          await reloadCoarseAfterPageOffset();
          toast(d.message || `已改为 p${d.page_start}–${d.page_end}`);
        } catch (e) {
          toast(e.message || String(e));
          wrap.querySelectorAll('.wb-page-offset-btn').forEach((b) => { b.disabled = false; });
        }
      });
    });
    wrap.querySelectorAll('.wb-page-offset--volume .wb-page-offset-btn').forEach((btn) => {
      btn.addEventListener('click', async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const delta = Number(btn.dataset.volumeDelta || 0);
        if (!delta) return;
        wrap.querySelectorAll('.wb-page-offset-btn').forEach((b) => { b.disabled = true; });
        try {
          const d = await shiftVolumePageOffset(delta);
          await reloadCoarseAfterPageOffset();
          toast(d.message || '整册页码已偏移');
        } catch (e) {
          toast(e.message || String(e));
          wrap.querySelectorAll('.wb-page-offset-btn').forEach((b) => { b.disabled = false; });
        }
      });
    });
  }

  function lessonOptionLabel(les) {
    if (!les) return '';
    if (les.option_label) return String(les.option_label);
    const unit = String(les.unit_title || '').trim();
    const label = lessonLabel(les);
    const ps = les.page_start;
    const pe = les.page_end;
    let page = '';
    if (ps) page = (!pe || pe === ps) ? `p${ps}` : `p${ps}-${pe}`;
    const vol = String(les.volume_label || '').trim();
    // 课名放最前，窄宽度时优先露出全名；跨年级匹配补年级册次
    return [label, unit, page, vol].filter(Boolean).join(' · ');
  }

  function matchMethodLabel(m) {
    const map = {
      name_exact: '完全匹配',
      name_similar: '相似匹配',
      name_edition: '跨年级匹配',
      name_llm: '大模型匹配',
      none: '未匹配',
      title_anchor: '课名匹配',
      lesson_no: '课号匹配',
      index: '顺序对齐',
      manual: '手工改对',
      footer: '印刷页码',
      position: '页序估计',
      content_weak: '内容弱匹配',
    };
    return map[m] || m || '—';
  }

  function matchMethodClass(m) {
    const map = {
      name_exact: 'wb-match-exact',
      name_similar: 'wb-match-similar',
      name_edition: 'wb-match-edition',
      name_llm: 'wb-match-llm',
      none: 'wb-match-none',
    };
    return map[m] || '';
  }

  function pairStatusLabel(st) {
    const map = {
      suggested: '待对比',
      pending_review: '待确认',
      confirmed: '已确认',
      rejected: '无对应',
    };
    return map[st] || st || '—';
  }

  function compareHref(it) {
    let url = it.compare_url || '';
    if (!url) return '';
    if (!url.includes('from=')) {
      url += (url.includes('?') ? '&' : '?') + 'from=workbook';
    }
    if (subject?.id && !url.includes('subject=')) {
      url += (url.includes('?') ? '&' : '?') + `subject=${encodeURIComponent(subject.id)}`;
    }
    return url;
  }

  function isTestSubject() {
    // Test 沙箱专属（不含小科：小科整册一键要走环节整理+复用建议）
    return isSandboxSubject() && !isXiaokeSubject();
  }

  function isDraftCoarse(detail) {
    const n = detail && detail.new;
    if (n && (n.draft_only || (n.has_preview_pdf && !n.has_pdf))) return true;
    const coarse = detail && detail.coarse;
    return !!(coarse && coarse.mode === 'draft_pages');
  }

  function changeBadgeClass(change) {
    const c = String(change || '');
    if (c.includes('基本一致') || c === '一致' || c === '没变化') return 'wb-change-ok';
    if (c.includes('标点')) return 'wb-change-soft';
    if (c.includes('局部') || c.includes('改写') || c.includes('文字')) return 'wb-change-diff';
    if (c.includes('结构') || c.includes('大幅') || c.includes('分页')) return 'wb-change-big';
    return 'wb-change-unknown';
  }

  function renderChapterOverviewHtml(overview, { draft } = {}) {
    if (!overview) return '';
    const changedN = Number(overview.changed_chapter_count || 0);
    const totalN = Number(overview.chapter_count || 0);
    const pagesChanged = Number(overview.pages_changed || 0);
    const blocks = Number(overview.text_changed_blocks || 0);
    if (!totalN && !(overview.chapters || []).length) return '';
    const headBits = [
      `<strong>${changedN}</strong> / ${totalN || (overview.chapters || []).length} 个章节有变动`,
    ];
    if (!draft && pagesChanged > 0) headBits.push(`变动 ${pagesChanged} 页`);
    if (!draft && blocks > 0) headBits.push(`文字差异块 ${blocks}`);
    const pinyinPages = Number(overview.pinyin_attention_pages || 0);
    const pinyinCh = Number(overview.pinyin_attention_chapter_count || 0);
    if (!draft && (pinyinPages > 0 || pinyinCh > 0)) {
      headBits.push(
        pinyinCh > 0
          ? `音标变动注意 ${pinyinCh} 课 / ${pinyinPages} 页`
          : `音标变动注意 ${pinyinPages} 页`
      );
    }
    // 只保留总统计条，不再展开各章节卡片
    return `<div class="wb-chapter-overview wb-chapter-overview--stats">
      <div class="wb-chapter-overview-head">${headBits.join(' · ')}</div>
    </div>`;
  }

  function coarseReadyHint(detail) {
    if (!detail) return '请先打开一本';
    const oldS = detail.old || {};
    const newS = detail.new || {};
    if (isDraftCoarse(detail)) {
      const miss = [];
      if (!oldS.has_pdf || !oldS.catalog_ready) miss.push('旧侧目录识别');
      if (!(newS.draft_pages_ready || (newS.draft_page_mapped || 0) > 0)) {
        miss.push('新侧解析印刷页码/生成页图');
      }
      return miss.length ? `未就绪：缺 ${miss.join('、')}` : '';
    }
    if (isXiaokeSubject()) {
      const lib = detail.old_library || {};
      const miss = [];
      if (!(oldS.catalog_ready || lib.catalog_ready)) {
        miss.push('旧目录（旧库尚无该册，请先在旧库建设载入）');
      }
      if (!newS.catalog_ready) miss.push('新侧目录识别');
      return miss.length ? `未就绪：缺 ${miss.join('、')}` : '';
    }
    if (!oldS.catalog_ready || !newS.catalog_ready) {
      return '未就绪：请先完成两侧目录识别';
    }
    return '';
  }

  function failedStorageKey() {
    if (!current.old_code || !current.new_code) return '';
    return `wb-failed-lessons:${current.old_code}:${current.new_code}`;
  }

  function rememberFailedLessonUids(uids) {
    const key = failedStorageKey();
    if (!key) return;
    try {
      if (uids && uids.length) localStorage.setItem(key, JSON.stringify(uids));
      else localStorage.removeItem(key);
    } catch (_) { /* ignore */ }
  }

  function loadRememberedFailedLessonUids() {
    const key = failedStorageKey();
    if (!key) return [];
    try {
      const raw = localStorage.getItem(key);
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.map(String).filter(Boolean) : [];
    } catch (_) {
      return [];
    }
  }

  function failedLessonUidsFromSnap(snap) {
    if (snap) {
      if (Array.isArray(snap.failed_lesson_uids) && snap.failed_lesson_uids.length) {
        return snap.failed_lesson_uids.map(String);
      }
      const lessons = snap.lessons || {};
      const fromLessons = Object.keys(lessons).filter(
        (uid) => (lessons[uid] || {}).status === 'error'
      );
      if (fromLessons.length) return fromLessons;
    }
    return loadRememberedFailedLessonUids();
  }

  function updateRetryFailedButton({ busy } = {}) {
    const retryBtn = document.getElementById('btn-volume-retry-failed');
    if (!retryBtn) return;
    const failed = failedLessonUidsFromSnap(lastPipelineSnap);
    const running = !!(lastPipelineSnap && Number(lastPipelineSnap.running_count || 0) > 0);
    const show = !busy && !running && failed.length > 0 && !isDraftCoarse(current.detail);
    retryBtn.hidden = !show;
    retryBtn.disabled = !show;
    retryBtn.textContent = failed.length ? `失败重启（${failed.length} 课）` : '失败重启';
    retryBtn.title = failed.length
      ? `只重跑失败的 ${failed.length} 个课时（有缓存的页会跳过）`
      : '上一轮没有失败课时';
  }

  function updateCoarseChrome(detail, { busy } = {}) {
    const title = document.getElementById('coarse-title');
    const lead = document.getElementById('coarse-lead');
    const btn = document.getElementById('btn-coarse');
    const volumeBtn = document.getElementById('btn-volume-pipeline');
    const exportBtn = document.getElementById('btn-export-xlsx');
    const exportCoarseBtn = document.getElementById('btn-export-coarse-xlsx');
    const draft = isDraftCoarse(detail);
    const testCombo = isTestSubject() && !draft;
    if (title) title.textContent = draft ? '③ 页级粗分' : '③ 课时粗分';
    if (lead) {
      lead.textContent = draft
        ? '页级粗分：修订 PDF 页 ↔ 旧书 PDF 页（按印刷页码挂接）。点「进入对比」打开并排页对比。'
        : isXiaokeSubject()
          ? '按课名匹配：完全 → 同年级相似 → 同社跨年级相似 → 大模型兜底。点「进入对比」会拉取旧库/新侧对应课 PDF 页图（新侧首次可能需划分页码，稍候）。'
          : testCombo
            ? 'Test：一键识别完成后自动接粗分→整册对比；对比过程只写本地 JSON，跑完后可「提交到数据库」。失败可用「失败重启」只重跑失败课时。'
            : '目录拆完后的初步课对课匹配；可改对后点「进入对比」。跑完比对后为「待确认」，对比页人工确认后为「已确认」。';
    }
    const blocked = coarseReadyHint(detail);
    if (btn) {
      // Test 完整版：粗分合入整册一键，隐藏单独「运行粗分」
      btn.hidden = !!testCombo;
      btn.disabled = !!busy;
      btn.textContent = busy
        ? (draft ? '页级粗分中…' : '粗分中…')
        : '运行粗分';
      btn.title = blocked || (draft ? '运行页级粗分（约需数秒到几十秒）' : '运行课对课粗分');
    }
    const hasItems = !!(detail && detail.coarse && (detail.coarse.items || []).length);
    const phaseBatchBtn = document.getElementById('btn-phase-batch');
    if (phaseBatchBtn) {
      phaseBatchBtn.hidden = !isXiaokeSubject() || !!draft;
    }
    if (volumeBtn) {
      if (testCombo) {
        volumeBtn.hidden = !!busy;
        volumeBtn.disabled = !!busy;
        volumeBtn.textContent = busy ? '粗分中…' : '整册一键（粗分→对比）';
        volumeBtn.title = blocked
          || '先与其它学科相同地运行课时粗分，完成后再启动整册对比';
      } else if (isXiaokeSubject()) {
        volumeBtn.textContent = '整册一键';
        volumeBtn.title = '对本册已匹配的每一课：整课一键 + 环节整理 + 复用建议（模型先判，再按环节对比加权），并写回本表下拉框';
        // 有粗分课对就显示；勿默认 hidden（旧缓存 JS 曾一直不揭开）
        volumeBtn.hidden = !!draft || !!busy;
        volumeBtn.disabled = !!busy || !hasItems;
      } else {
        volumeBtn.textContent = '整册一键对比';
        volumeBtn.title = '仅「待对比」课对：逐页跑 ①→④（页级并行，有缓存跳过）';
        volumeBtn.hidden = draft || !hasItems || !!busy;
        volumeBtn.disabled = !!busy;
      }
    }
    if (exportCoarseBtn) {
      exportCoarseBtn.hidden = draft || !hasItems || !!busy;
      exportCoarseBtn.disabled = !!busy || !hasItems;
      exportCoarseBtn.title = draft
        ? '修订版页级模式暂不支持粗分导出'
        : '下载课时粗分 Excel：出版社、新旧课名/年级/册次/单元课时，匹配方式夹在新旧之间';
    }
    if (exportBtn) {
      const items = (detail && detail.coarse && detail.coarse.items) || [];
      const hasCompared = items.some((it) => {
        const cs = (it && it.change_stats) || {};
        return Number(cs.pages_compared || 0) > 0;
      });
      const overviewPages = Number(
        (detail && detail.coarse && detail.coarse.chapter_overview
          && detail.coarse.chapter_overview.pages_changed) || 0
      );
      // 有粗分即可显示；无比对结果时点击会由后端报错提示
      exportBtn.hidden = draft || !hasItems || !!busy;
      exportBtn.disabled = !!busy || !hasItems;
      exportBtn.title = draft
        ? '修订版页级模式暂不支持整册导出'
        : (hasCompared || overviewPages > 0
          ? '下载 Excel：总表（章节变动总览）+ 各单元表（页级文字/图片对比）'
          : '下载 Excel（若尚未比对完成，可能无页级明细）');
    }
    updateRetryFailedButton({ busy });
  }

  function setPipelineStatus(text, { busy, eta, doneElapsed } = {}) {
    const el = document.getElementById('pipeline-status');
    if (!el) return;
    if (!text && !eta && !doneElapsed) {
      el.hidden = true;
      el.textContent = '';
      el.innerHTML = '';
      el.classList.remove('is-busy', 'is-done');
      return;
    }
    el.hidden = false;
    el.classList.toggle('is-busy', !!busy);
    el.classList.toggle('is-done', !!doneElapsed && !busy);
    const etaText = (eta || '').trim();
    const doneText = (doneElapsed || '').trim();
    const parts = [];
    if (text) parts.push(`<span class="pipeline-status-main">${esc(text)}</span>`);
    if (etaText && text && !String(text).includes('预计还需')) {
      parts.push(`<span class="pipeline-status-eta">${esc(etaText)}</span>`);
    } else if (etaText && !text) {
      parts.push(`<span class="pipeline-status-eta">${esc(etaText)}</span>`);
    }
    if (doneText) {
      parts.push(`<span class="pipeline-status-elapsed">实际耗时 ${esc(doneText)}</span>`);
    }
    if (parts.length) el.innerHTML = parts.join('');
    else el.textContent = text || etaText || doneText;
  }

  function stopPipelinePoll() {
    pipelinePollGen += 1;
    if (pipelinePollTimer) {
      clearInterval(pipelinePollTimer);
      pipelinePollTimer = null;
    }
  }

  function showLessonProgressOptimistic(newLessonUid, totalHint) {
    if (!newLessonUid) return;
    document.querySelectorAll('.lesson-pipe-progress').forEach((el) => {
      if (el.dataset.newUid !== newLessonUid) return;
      el.hidden = false;
      el.dataset.status = 'running';
      const bar = el.querySelector('.lesson-pipe-bar > i');
      const label = el.querySelector('.lesson-pipe-label');
      if (bar) bar.style.width = '0%';
      if (label) label.textContent = totalHint > 0 ? `0/${totalHint} · 启动中` : '启动中…';
    });
  }

  function updateLessonProgressBars(snap) {
    const lessons = (snap && snap.lessons) || {};
    const runningUids = new Set(snap?.running_lesson_uids || []);
    const volumeRunning = !!snap?.volume_running;
    const anyRunning = (snap?.running_count || 0) > 0;

    document.querySelectorAll('.lesson-pipe-progress').forEach((el) => {
      const uid = el.dataset.newUid || '';
      const info = lessons[uid];
      const bar = el.querySelector('.lesson-pipe-bar > i');
      const label = el.querySelector('.lesson-pipe-label');
      if (!info) {
        // 整册跑时其它课可能暂无条目，保持隐藏
        if (!(volumeRunning && anyRunning)) el.hidden = true;
        return;
      }
      const done = Number(info.done || 0);
      const total = Math.max(1, Number(info.total || 1));
      const st = info.status || 'pending';
      // 进行中：显示「已完成页/总页」，不要用当前页号（否则最后一页一开始就像 2/2 跑完）
      let showDone = done;
      let showLabel = `${done}/${total}`;
      if (st === 'running') {
        const pageI = Number(info.lesson_page_index || 0);
        const pageN = Number(info.lesson_page_count || 0);
        if (pageI > 0 && pageN > 0) {
          showDone = Math.max(0, Math.min(done, pageI - 1));
          const phase = (info.page_phase_label || info.page_phase || '').trim();
          showLabel = phase
            ? `${showDone}/${total} · ${phase}`
            : `${showDone}/${total} · 第${pageI}页`;
        } else {
          showLabel = `${showDone}/${total} · 进行中`;
        }
      }
      const pct = st === 'done' || st === 'error'
        ? 100
        : Math.min(99, Math.round((showDone / total) * 100));
      el.hidden = !(anyRunning || st === 'done' || st === 'error' || done > 0);
      el.dataset.status = st;
      if (bar) bar.style.width = `${pct}%`;
      if (label) label.textContent = st === 'done' ? `完成 ${total}/${total}` : showLabel;
    });

    // 仅禁用：整册跑时全部；或正在跑的那几课；整册按钮在有任意任务时禁用
    const volBtn = document.getElementById('btn-volume-pipeline');
    if (volBtn) volBtn.disabled = anyRunning;
    const retryBtn = document.getElementById('btn-volume-retry-failed');
    if (retryBtn) retryBtn.disabled = anyRunning || retryBtn.hidden;
    document.querySelectorAll('.btn-lesson-pipeline').forEach((btn) => {
      const uid = btn.dataset.newUid || '';
      btn.disabled = volumeRunning || runningUids.has(uid);
    });
  }

  async function refreshPipelineStatus() {
    if (!current.old_code || !current.new_code) return null;
    const gen = pipelinePollGen;
    try {
      const r = await fetch(
        `${API}/workbook/pipeline/status?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
      );
      if (gen !== pipelinePollGen) return null; // 过期请求
      const d = await readJson(r);
      if (gen !== pipelinePollGen) return null;
      const snap = d.snapshot || d.job;
      if (!snap || !(snap.job_count || snap.jobs?.length || snap.status)) {
        if (!pipelinePollTimer && gen === pipelinePollGen) {
          setPipelineStatus('', { busy: false });
        }
        return null;
      }
      lastPipelineSnap = snap;
      const msg = snap.message || '';
      const runningCount = Number(snap.running_count || 0);
      if (runningCount === 0 && (snap.status === 'done' || snap.status === 'done_with_errors' || snap.status === 'error')) {
        const failedNow = failedLessonUidsFromSnap(snap);
        // 成功清空记忆；有失败则记下，便于刷新后仍可「失败重启」
        if (snap.status === 'done') rememberFailedLessonUids([]);
        else if (failedNow.length) rememberFailedLessonUids(failedNow);
      }
      updateLessonProgressBars(snap);
      updateRetryFailedButton({ busy: runningCount > 0 });

      // 首次拉取：记下已有终态，避免刷新页狂弹 toast
      (snap.jobs || []).forEach((j) => {
        const id = j.job_id || '';
        const js = j.status || '';
        if (!id) return;
        if (!pipelineSeeded) {
          if (js !== 'running') pipelineSeenDone.add(id);
          return;
        }
        if (js === 'running' || pipelineSeenDone.has(id)) return;
        if (js === 'done' || js === 'done_with_errors' || js === 'error' || js === 'cancelled') {
          pipelineSeenDone.add(id);
          const label = j.scope === 'volume'
            ? '整册'
            : (j.current_label || j.new_lesson_uid || '本课时');
          const cost = j.elapsed_label
            || (j.elapsed_sec ? `${Math.round(Number(j.elapsed_sec))} 秒` : '');
          const costSuffix = cost ? `，实际耗时 ${cost}` : '';
          if (js === 'done') toast(`${label}：一键对比完成${costSuffix}`);
          else if (js === 'done_with_errors') toast(`${label}：完成（有失败页）${costSuffix}`);
          else if (js === 'error') toast(`${label}：失败 ${j.message || ''}`);
        }
      });
      pipelineSeeded = true;

      if (runningCount > 0) {
        pipelineHadRunning = true;
        const rawMsg = String(snap.message || '');
        const mainMsg = rawMsg.split(/ · 预计还需/)[0] || '一键对比进行中…';
        const bits = [mainMsg];
        if (snap.pages_total) {
          bits.push(`已完成 ${snap.pages_done || 0}/${snap.pages_total} 页`);
        }
        if (Number(snap.workers || 0) > 1) {
          bits.push(`${snap.workers} 路并行`);
        }
        const seen = new Set();
        const base = bits.filter((b) => {
          const k = String(b).replace(/\s/g, '');
          if (seen.has(k)) return false;
          seen.add(k);
          return true;
        }).join(' · ');
        setPipelineStatus(base, {
          busy: true,
          eta: snap.eta_label || '',
        });
      } else {
        const doneElapsed = snap.elapsed_label
          || (snap.elapsed_sec ? `${Math.round(Number(snap.elapsed_sec))} 秒` : '');
        let doneMsg = String(msg || '').replace(/\s*·\s*实际耗时[^·]*/g, '').trim();
        if (!doneMsg && doneElapsed) doneMsg = '一键对比已完成';
        setPipelineStatus(doneMsg, {
          busy: false,
          doneElapsed: (msg && String(msg).includes('实际耗时')) ? '' : doneElapsed,
        });
        // 只有本轮确实跑过才停轮询并重绘；进页时空闲 status 不得掐掉刚启动的轮询
        if (pipelineHadRunning && gen === pipelinePollGen) {
          pipelineHadRunning = false;
          stopPipelinePoll();
          const settleGen = pipelinePollGen;
          updateCoarseChrome(current.detail);
          try {
            const pr = await fetch(
              `${API}/workbook/pair?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
            );
            const pd = await readJson(pr);
            current.detail = pd;
            // 结算期间又开了新任务：不拆 DOM，留给新轮询刷进度
            if (pipelinePollTimer || pipelinePollGen !== settleGen) return snap;
            renderCoarse(pd);
            updateLessonProgressBars(snap);
            // 结算后保留耗时条，避免 render 后被清掉
            if (doneMsg || doneElapsed) {
              setPipelineStatus(doneMsg || '一键对比已完成', {
                busy: false,
                doneElapsed: (msg && String(msg).includes('实际耗时')) ? '' : doneElapsed,
              });
            }
          } catch (_) { /* ignore */ }
        }
      }
      return snap;
    } catch (e) {
      return null;
    }
  }

  function startPipelinePoll() {
    stopPipelinePoll();
    pipelineHadRunning = true;
    refreshPipelineStatus();
    const gen = pipelinePollGen;
    pipelinePollTimer = setInterval(() => {
      if (gen !== pipelinePollGen) return;
      refreshPipelineStatus();
    }, 1500);
  }

  let xiaokeFullPollTimer = null;
  function stopXiaokeLessonFullPoll() {
    if (xiaokeFullPollTimer) {
      clearInterval(xiaokeFullPollTimer);
      xiaokeFullPollTimer = null;
    }
  }

  async function reloadCoarseQuietly() {
    if (!current.old_code || !current.new_code) return;
    const pr = await fetch(
      `${API}/workbook/pair?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
    );
    const pd = await readJson(pr);
    current.detail = pd;
    renderCoarse(pd);
  }

  function startXiaokeLessonFullPoll(newLessonUid) {
    stopXiaokeLessonFullPoll();
    const uid = String(newLessonUid || '');
    const tick = async () => {
      if (!uid || !current.old_code || !current.new_code) return;
      try {
        const r = await fetch(
          `${API}/workbook/xiaoke/lesson-full/status?old_code=${encodeURIComponent(current.old_code)}`
          + `&new_code=${encodeURIComponent(current.new_code)}`
          + `&new_lesson_uid=${encodeURIComponent(uid)}`
        );
        const d = await readJson(r);
        const job = d.job;
        if (!job) return;
        const done = Number(job.done || 0);
        const total = Number(job.total || 1);
        const st = String(job.status || '');
        const running = st === 'running';
        updateLessonProgressBars({
          running_count: running ? 1 : 0,
          volume_running: false,
          running_lesson_uids: running ? [uid] : [],
          lessons: {
            [uid]: {
              done,
              total,
              status: running ? 'running' : (st === 'done' ? 'done' : (st === 'error' || st === 'done_with_errors' ? 'error' : 'pending')),
              page_phase_label: job.message || job.phase || '',
            },
          },
        });
        setPipelineStatus(job.message || '', { busy: running });
        if (!running) {
          stopXiaokeLessonFullPoll();
          if (st === 'done') toast('整课一键完成');
          else if (st === 'done_with_errors') toast('整课一键完成（有失败）');
          else if (st === 'error') toast(`整课一键失败：${job.last_error || job.message || ''}`);
          try {
            await reloadCoarseQuietly();
          } catch (_) { /* ignore */ }
        }
      } catch (e) {
        /* keep polling */
      }
    };
    tick();
    xiaokeFullPollTimer = setInterval(tick, 2000);
  }

  let xiaokeVolumePollTimer = null;
  function stopXiaokeVolumeFullPoll() {
    if (xiaokeVolumePollTimer) {
      clearInterval(xiaokeVolumePollTimer);
      xiaokeVolumePollTimer = null;
    }
  }

  function matchedXiaokeLessonUids() {
    const items = (current.detail && current.detail.coarse && current.detail.coarse.items) || [];
    return items
      .filter((it) => {
        const n = it.new || {};
        const o = it.old || {};
        return n.lesson_uid && o.lesson_uid && (it.pair_status || '') !== 'rejected';
      })
      .map((it) => ({
        uid: String(it.new.lesson_uid),
        label: lessonLabel(it.new || {}),
      }));
  }

  function startXiaokeVolumeFullPoll() {
    stopXiaokeVolumeFullPoll();
    let lastLessonsDone = -1;
    let lastAdviceCount = -1;
    const tick = async () => {
      if (!current.old_code || !current.new_code) return;
      try {
        const r = await fetch(
          `${API}/workbook/xiaoke/volume-full/status?old_code=${encodeURIComponent(current.old_code)}`
          + `&new_code=${encodeURIComponent(current.new_code)}`
        );
        const d = await readJson(r);
        const job = d.job;
        if (!job) return;
        const st = String(job.status || '');
        const running = st === 'running';
        const lessonsDone = Number(job.lessons_done || job.done || 0);
        const lessonsTotal = Number(job.lessons_total || job.total || 1);
        const adviceCount = Number(job.advice_count || 0);
        const curUid = String(job.current_lesson_uid || '');
        const curJob = job.current_lesson_job || null;
        const lessonsSnap = {};
        const results = job.lesson_results || {};
        Object.keys(results).forEach((uid) => {
          const row = results[uid] || {};
          const rs = String(row.status || '');
          lessonsSnap[uid] = {
            done: rs === 'done' ? 1 : 0,
            total: 1,
            status: rs === 'done' ? 'done' : (rs === 'error' || rs === 'done_with_errors' ? 'error' : 'pending'),
            page_phase_label: row.last_error || rs || '',
          };
        });
        if (curUid && running) {
          lessonsSnap[curUid] = {
            done: Number((curJob && curJob.done) || 0),
            total: Number((curJob && curJob.total) || 1),
            status: 'running',
            page_phase_label: (curJob && (curJob.message || curJob.phase)) || job.message || '',
          };
        }
        const shouldReload = lessonsDone !== lastLessonsDone
          || adviceCount !== lastAdviceCount
          || !running;
        lastLessonsDone = lessonsDone;
        lastAdviceCount = adviceCount;
        if (shouldReload) {
          try {
            await reloadCoarseQuietly();
          } catch (_) { /* ignore */ }
        }
        updateLessonProgressBars({
          running_count: running ? 1 : 0,
          volume_running: running,
          running_lesson_uids: curUid ? [curUid] : [],
          lessons: lessonsSnap,
        });
        const cancelBtn = document.getElementById('btn-volume-cancel');
        if (cancelBtn) cancelBtn.hidden = !running;
        setPipelineStatus(
          job.message || `整册 ${lessonsDone}/${lessonsTotal}`,
          { busy: running }
        );
        if (!running) {
          stopXiaokeVolumeFullPoll();
          document.querySelectorAll(
            '.btn-lesson-pipeline, #btn-volume-pipeline, #btn-volume-retry-failed'
          ).forEach((b) => { b.disabled = false; });
          if (cancelBtn) cancelBtn.hidden = true;
          if (st === 'done') {
            toast(adviceCount
              ? `整册一键完成（${lessonsDone} 课，${adviceCount} 条复用建议已写入下拉框）`
              : `整册一键完成（${lessonsDone} 课）`);
          } else if (st === 'done_with_errors') toast(`整册一键完成（有失败）`);
          else if (st === 'cancelled') toast(job.message || '已停止整册一键');
          else if (st === 'error') toast(`整册一键失败：${job.last_error || job.message || ''}`);
        }
      } catch (e) {
        /* keep polling */
      }
    };
    tick();
    xiaokeVolumePollTimer = setInterval(tick, 2000);
  }

  async function startXiaokeVolumeFull({ skipConfirm, newLessonUids } = {}) {
    if (!current.old_code || !current.new_code) {
      toast('请先打开一本');
      return false;
    }
    if (isDraftCoarse(current.detail)) {
      toast('修订版页级模式暂请用对比页「整课一键」');
      return false;
    }
    const wantUids = Array.isArray(newLessonUids)
      ? newLessonUids.map(String).filter(Boolean)
      : [];
    const matched = wantUids.length
      ? matchedXiaokeLessonUids().filter((m) => wantUids.includes(m.uid))
      : matchedXiaokeLessonUids();
    if (!matched.length) {
      toast('没有已匹配旧课的课时，请先粗分并选择旧课对应');
      return false;
    }
    const plan = `整册一键（小科）：共 ${matched.length} 课\n`
      + '对每课依次：整课一键（旧/新 OCR → 合成比对）+ 教学环节整理 + 复用建议\n'
      + '复用建议与表格章节环节整理相同：模型先判，再按环节对比加权；\n'
      + '只在证据足时把「直接复用」改为「新制」，不改人工选择。\n'
      + '完成后，复用建议会显示在本页对应课时的下拉框中。\n'
      + '已有页缓存会跳过。可离开本页，任务在后台继续。';
    if (!skipConfirm && !confirm(`${plan}\n\n继续？`)) return false;
    try {
      stopPipelinePoll();
      setPipelineStatus('整册一键启动中…', { busy: true });
      const r = await fetch(`${API}/workbook/xiaoke/volume-full`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: current.old_code,
          new_code: current.new_code,
          skip_cached: true,
          ...(wantUids.length ? { new_lesson_uids: wantUids } : {}),
        }),
      });
      const d = await readJson(r);
      toast(`小科整册一键已启动（${matched.length} 课）`);
      setPipelineStatus((d.job && d.job.message) || '整册一键进行中…', { busy: true });
      pipelineHadRunning = true;
      document.querySelectorAll(
        '.btn-lesson-pipeline, #btn-volume-pipeline, #btn-volume-retry-failed'
      ).forEach((b) => { b.disabled = true; });
      const cancelBtn = document.getElementById('btn-volume-cancel');
      if (cancelBtn) {
        cancelBtn.hidden = false;
        cancelBtn.disabled = false;
      }
      matched.forEach((m) => showLessonProgressOptimistic(m.uid, 1));
      startXiaokeVolumeFullPoll();
      return true;
    } catch (e) {
      toast(e.message || String(e));
      return false;
    }
  }

  async function startPipeline({
    scope,
    newLessonUid,
    label,
    skipConfirm,
    onlyNewLessonUids,
  } = {}) {
    if (!current.old_code || !current.new_code) {
      toast('请先打开一本');
      return false;
    }
    if (isDraftCoarse(current.detail)) {
      toast('修订版页级模式暂请用对比页「整课一键」');
      return false;
    }

    const retryUids = Array.isArray(onlyNewLessonUids)
      ? onlyNewLessonUids.map(String).filter(Boolean)
      : [];

    // 小科整册必须走整课一键+环节整理+复用建议，不能用语文那套页级 ①→④
    if (isXiaokeSubject() && scope === 'volume') {
      return startXiaokeVolumeFull({
        skipConfirm,
        newLessonUids: retryUids.length ? retryUids : undefined,
      });
    }

    // 小科本课时：分侧 OCR 再比对（旧全页 → 新全页 → 共有页序比对）
    if (isXiaokeSubject() && scope === 'lesson' && newLessonUid) {
      const plan = `本课时「${label || ''}」整课一键（小科）\n`
        + '1) 旧侧全部页 OCR\n2) 新侧全部页 OCR\n'
        + '3) 合成整课识别结果：全文差异 + 跨页原子差异\n'
        + '4) 教学环节整理 + 按简介做新旧改动对照\n'
        + '页数不一致也可对齐内容。已有侧缓存会跳过。';
      if (!skipConfirm && !confirm(`${plan}\n\n可离开本页，任务在后台继续。继续？`)) return false;
      try {
        const r = await fetch(`${API}/workbook/xiaoke/lesson-full`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            old_code: current.old_code,
            new_code: current.new_code,
            new_lesson_uid: newLessonUid,
            skip_cached: true,
          }),
        });
        const d = await readJson(r);
        toast('小科整课一键已启动');
        setPipelineStatus((d.job && d.job.message) || '整课一键进行中…', { busy: true });
        pipelineHadRunning = true;
        document.querySelectorAll('.btn-lesson-pipeline').forEach((b) => {
          if (b.dataset.newUid === newLessonUid) b.disabled = true;
        });
        const totalHint = Number((d.job && d.job.total) || 0);
        showLessonProgressOptimistic(newLessonUid, totalHint);
        if (d.job) {
          updateLessonProgressBars({
            running_count: 1,
            volume_running: false,
            running_lesson_uids: [newLessonUid],
            lessons: {
              [newLessonUid]: {
                done: 0,
                total: totalHint || 1,
                status: 'running',
                page_phase_label: '启动中',
              },
            },
          });
        }
        startXiaokeLessonFullPoll(newLessonUid);
        return true;
      } catch (e) {
        toast(e.message || String(e));
        return false;
      }
    }

    const isRetry = scope === 'volume' && retryUids.length > 0;
    const plan = isRetry
      ? `失败重启：只重跑 ${retryUids.length} 个失败课时，逐页 ①→④（有缓存跳过）\n整册运行时不可再开本课时/整册一键`
      : (scope === 'volume'
        ? '整册：只跑状态为「待对比」的课对，逐页 ①→④（有缓存跳过；页级并行）\n已是「待确认 / 已确认」的课会跳过。整册运行时不可再开本课时/整册一键'
        : `本课时「${label || ''}」：逐页跑完整 ①→④（有缓存跳过；页级并行）\n可同时点开多课并行`);
    if (!skipConfirm && !confirm(`${plan}\n\n可离开本页，任务在后台继续。继续？`)) return false;
    try {
      const body = {
        old_code: current.old_code,
        new_code: current.new_code,
        scope,
      };
      if (newLessonUid) body.new_lesson_uid = newLessonUid;
      if (isRetry) body.new_lesson_uids = retryUids;
      const r = await fetch(`${API}/workbook/pipeline/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await readJson(r);
      toast(
        isRetry
          ? `失败重启已启动（${retryUids.length} 课）`
          : (scope === 'volume' ? '整册一键已启动' : '本课时一键已启动（可继续点其它课）')
      );
      setPipelineStatus((d.job && d.job.message) || '一键对比进行中…', { busy: true });
      pipelineHadRunning = true;
      // 立刻禁用本课按钮 + 露出进度，避免等下一次轮询才有反馈
      if (scope === 'lesson' && newLessonUid) {
        document.querySelectorAll('.btn-lesson-pipeline').forEach((b) => {
          if (b.dataset.newUid === newLessonUid) b.disabled = true;
        });
        const volBtn = document.getElementById('btn-volume-pipeline');
        if (volBtn) volBtn.disabled = true;
        const retryBtn = document.getElementById('btn-volume-retry-failed');
        if (retryBtn) retryBtn.disabled = true;
        const totalHint = Number((d.job && d.job.total) || 0);
        showLessonProgressOptimistic(newLessonUid, totalHint);
        // 用 start 返回的快照先刷一版（若有）
        if (d.job) {
          updateLessonProgressBars({
            running_count: 1,
            volume_running: false,
            running_lesson_uids: [newLessonUid],
            lessons: {
              [newLessonUid]: {
                done: 0,
                total: totalHint || 1,
                status: 'running',
                page_phase_label: '启动中',
              },
            },
          });
        }
      } else if (scope === 'volume') {
        document.querySelectorAll(
          '.btn-lesson-pipeline, #btn-volume-pipeline, #btn-volume-retry-failed'
        ).forEach((b) => {
          b.disabled = true;
        });
      }
      startPipelinePoll();
      return true;
    } catch (e) {
      toast(e.message || String(e));
      return false;
    }
  }

  /** 与其它学科相同的课时粗分 API（不改匹配逻辑）。 */
  async function runCoarseMatchRequest() {
    const r = await fetch(`${API}/workbook/coarse-match`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        old_code: current.old_code,
        new_code: current.new_code,
        replace: true,
      }),
    });
    return readJson(r);
  }

  /** Test：先粗分，完成后再整册对比（串行）。 */
  async function startTestVolumeCombo({ skipConfirm } = {}) {
    if (!current.old_code || !current.new_code) {
      toast('请先打开一本');
      return;
    }
    const blocked = coarseReadyHint(current.detail);
    if (blocked) {
      toast(blocked);
      return;
    }
    if (isDraftCoarse(current.detail)) {
      toast('修订版请先用「运行粗分」做页级粗分');
      return;
    }
    if (!skipConfirm && !confirm(
      '将依次执行：\n'
      + '① 课时粗分（与其它学科相同）\n'
      + '② 粗分完成后再启动整册对比\n\n'
      + '两步串行，不会同时进行。继续？'
    )) return;

    const wrap = document.getElementById('coarse-table');
    updateCoarseChrome(current.detail, { busy: true });
    if (wrap) wrap.innerHTML = '<p class="hint">① 正在课对课粗分，请稍候…</p>';
    setPipelineStatus('① 课时粗分进行中…', { busy: true });
    toast('① 粗分进行中…');
    try {
      const d = await runCoarseMatchRequest();
      current.detail.coarse = d.coarse;
      renderCoarse(current.detail);
      toast(`① 粗分完成：${d.created || 0} 对 · 即将开始整册对比`);
      setPipelineStatus('② 粗分已完成，正在启动整册对比…', { busy: true });
      const started = await startPipeline({
        scope: 'volume',
        label: '整册',
        skipConfirm: true,
      });
      if (!started) {
        setPipelineStatus('粗分已完成；整册对比未启动', { busy: false });
        updateCoarseChrome(current.detail);
      }
    } catch (e) {
      renderCoarse(current.detail);
      setPipelineStatus('', {});
      updateCoarseChrome(current.detail);
      toast(e.message || String(e));
    }
  }

  async function startFailedVolumeRetry() {
    const failed = failedLessonUidsFromSnap(lastPipelineSnap);
    if (!failed.length) {
      toast('没有失败课时可重启');
      return;
    }
    if (isXiaokeSubject()) {
      const plan = `失败重启（小科）：只重跑 ${failed.length} 个失败课时\n`
        + '整课一键（OCR→比对→环节整理+对照）；已有页缓存会跳过。';
      if (!confirm(`${plan}\n\n继续？`)) return;
      try {
        const r = await fetch(`${API}/workbook/xiaoke/volume-full`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            old_code: current.old_code,
            new_code: current.new_code,
            skip_cached: true,
            new_lesson_uids: failed,
          }),
        });
        const d = await readJson(r);
        toast(`小科失败重启已启动（${failed.length} 课）`);
        setPipelineStatus((d.job && d.job.message) || '失败重启进行中…', { busy: true });
        pipelineHadRunning = true;
        rememberFailedLessonUids(failed);
        document.querySelectorAll(
          '.btn-lesson-pipeline, #btn-volume-pipeline, #btn-volume-retry-failed'
        ).forEach((b) => { b.disabled = true; });
        failed.forEach((uid) => showLessonProgressOptimistic(uid, 1));
        startXiaokeVolumeFullPoll();
      } catch (e) {
        toast(e.message || String(e));
      }
      return;
    }
    const started = await startPipeline({
      scope: 'volume',
      label: '失败重启',
      onlyNewLessonUids: failed,
    });
    if (started) rememberFailedLessonUids(failed);
  }

  function oldLessonOptions(detail, selectedUid) {
    let lessons = (detail && detail.old && detail.old.lessons) || [];
    // 小科：旧目录在旧库，不在 DOLD
    if (isXiaokeSubject()) {
      const libLessons = (detail && detail.old_library && detail.old_library.lessons) || [];
      if (libLessons.length) lessons = libLessons;
    }
    const sel = String(selectedUid || '').trim();
    // 已匹配课时若不在列表中（极端情况），补一条以免下拉显示「无对应」
    if (sel && !lessons.some((les) => les.lesson_uid === sel)) {
      const fromItems = ((detail && detail.coarse && detail.coarse.items) || [])
        .map((it) => it.old)
        .find((o) => o && o.lesson_uid === sel);
      if (fromItems) lessons = lessons.concat([fromItems]);
    }
    const opts = ['<option value="">— 无对应 —</option>'].concat(
      lessons.map((les) => {
        const selected = les.lesson_uid === sel ? ' selected' : '';
        const text = lessonOptionLabel(les);
        return `<option value="${les.lesson_uid}"${selected} title="${esc(text)}">${esc(text)}</option>`;
      })
    );
    return opts.join('');
  }

  const CHANGE_ADVICE_OPTIONS = [
    { value: '', label: '无数据' },
    { value: '新制', label: '新制' },
    { value: '直接复用', label: '直接复用' },
    { value: '视频剪辑复用', label: '视频剪辑复用' },
    { value: '部分补录', label: '部分补录' },
    { value: '课件部分素材，视频重录', label: '课件部分素材，视频重录' },
  ];

  function changeAdviceOptionsHtml(current) {
    const cur = String(current || '').trim();
    const known = new Set(CHANGE_ADVICE_OPTIONS.map((o) => o.value));
    const opts = CHANGE_ADVICE_OPTIONS.slice();
    if (cur && !known.has(cur)) {
      opts.splice(1, 0, { value: cur, label: cur });
    }
    return opts.map((o) => {
      const selected = o.value === cur ? ' selected' : '';
      return `<option value="${esc(o.value)}"${selected}>${esc(o.label)}</option>`;
    }).join('');
  }

  function renderDraftCoarse(detail) {
    const wrap = document.getElementById('coarse-table');
    const coarse = (detail && detail.coarse) || {};
    const items = coarse.items || [];
    if (coarse.error && !items.length) {
      wrap.innerHTML = `<p class="hint">${esc(coarse.error)}</p>`;
      return;
    }
    if (!items.length) {
      const blocked = coarseReadyHint(detail);
      wrap.innerHTML = blocked
        ? `<p class="hint">${esc(blocked)}</p>`
        : '<p class="hint">尚未页级粗分。新侧修订页就绪后点「运行粗分」（首次需扫描页脚，请稍候）。</p>';
      return;
    }
    const overviewHtml = renderChapterOverviewHtml(coarse.chapter_overview, { draft: true });
    const byChange = (coarse.summary && coarse.summary.by_change) || {};
    const summaryPills = Object.keys(byChange).map((k) =>
      `<span class="wb-change-pill ${changeBadgeClass(k)}">${esc(k)} ${byChange[k]} 页</span>`
    ).join('');
    wrap.innerHTML = `
      ${overviewHtml}
      ${summaryPills ? `<div class="wb-change-summary-row">${summaryPills}</div>` : ''}
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>修订 PDF 页</th>
            <th>印刷页</th>
            <th>旧书页</th>
            <th>变动</th>
            <th>方式</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          ${items.map((it) => {
            const href = compareHref(it);
            const printed = it.printed_page != null ? it.printed_page : '—';
            const oldPage = it.old_page != null ? it.old_page : '—';
            const label = it.label ? `<br><span class="hint">${esc(it.label)}</span>` : '';
            const change = it.change
              ? `<span class="wb-change-pill ${changeBadgeClass(it.change)}">${esc(it.change)}</span>${it.change_summary ? `<br><span class="hint">${esc(it.change_summary)}</span>` : ''}`
              : '<span class="hint">—</span>';
            return `<tr>
              <td>${it.sort_order || ''}</td>
              <td><strong>p${it.new_page != null ? it.new_page : '?'}</strong>${label}</td>
              <td>${printed}</td>
              <td>${oldPage}${it.old_lesson_label ? `<br><span class="hint">${esc(it.old_lesson_label)}</span>` : ''}</td>
              <td>${change}</td>
              <td>${esc(matchMethodLabel(it.match_method))}${it.comparable === false ? '<br><span class="hint">跳过</span>' : ''}</td>
              <td>
                ${href ? `<a class="btn btn-sm" href="${esc(href)}">进入对比</a>` : '<span class="hint">—</span>'}
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
      <p class="hint">可对比 ${coarse.comparable_count || 0} / ${coarse.pair_count || 0} 页 · 结果缓存在预览对比，不写入课对课</p>`;
  }

  function renderLessonCoarse(detail) {
    const wrap = document.getElementById('coarse-table');
    const coarse = (detail && detail.coarse) || {};
    const items = coarse.items || [];
    if (!items.length) {
      const blocked = coarseReadyHint(detail);
      wrap.innerHTML = blocked
        ? `<p class="hint">${esc(blocked)}</p>`
        : '<p class="hint">尚未粗分。两侧目录就绪后点「运行粗分」。</p>';
      return;
    }
    const overviewHtml = renderChapterOverviewHtml(coarse.chapter_overview, { draft: false });
    const showChangeCol = !!(coarse.chapter_overview
      && (Number(coarse.chapter_overview.chapter_count || 0) > 0
        || (coarse.chapter_overview.chapters || []).length));
    const tableHeader = `
      <table class="wb-lesson-coarse-table">
        <thead>
          <tr>
            <th class="wb-col-idx">#</th>
            <th class="wb-col-new">${volumePageOffsetHeaderHtml(items)}</th>
            <th class="wb-col-old">旧教材对应</th>
            <th class="wb-col-match">匹配方式</th>
            <th class="wb-col-status">状态</th>
            ${showChangeCol ? '<th class="wb-col-change">变动</th>' : ''}
            <th class="wb-col-actions"></th>
          </tr>
        </thead>`;
    // 数学按章节（unit_title）分组渲染；其他学科平铺
    const useChapterGroup = isMathSubject();
    const renderRow = (it) => {
            const n = it.new || {};
            const o = it.old || {};
            const st = it.pair_status || 'suggested';
            const href = compareHref(it);
            const cs = it.change_stats || {};
            let changeInner = '<span class="hint">尚未比对</span>';
            if (cs.lesson_verdict) {
              changeInner = cs.has_change
                ? `<span class="wb-change-pill wb-change-diff">${esc(cs.lesson_verdict)}</span>`
                : `<span class="wb-change-pill wb-change-ok">${esc(cs.lesson_verdict)}</span>`;
            } else if (cs.has_change) {
              changeInner = `<div class="wb-change-cell">`
                + `<span class="wb-change-pill wb-change-diff">变动 ${cs.pages_changed || 0} 页</span>`
                + (cs.text_changed_blocks
                  ? `<span class="wb-change-sub">差异块 ${cs.text_changed_blocks}</span>`
                  : '')
                + (cs.has_pinyin_attention || cs.pinyin_attention_pages
                  ? `<span class="wb-change-pill wb-change-pinyin">音标变动注意${cs.pinyin_attention_pages ? ` ${cs.pinyin_attention_pages}` : ''}</span>`
                  : '')
                + `</div>`;
            } else if (cs.pages_compared) {
              changeInner = '<span class="wb-change-pill wb-change-ok">无实质变动</span>';
            }
            const changeCell = showChangeCol
              ? `<td class="wb-col-change">${changeInner}</td>`
              : '';
            const advice = String(it.change_advice || '').trim();
            const adviceReason = String(it.change_advice_reason || '').trim();
            const adviceOpts = changeAdviceOptionsHtml(advice);
            return `<tr data-pair-id="${it.id}">
              <td class="wb-col-idx">${it.sort_order || ''}</td>
              <td class="workbook-lesson-cell wb-col-new"><strong>${esc(lessonLabel(n))}</strong>${newLessonPageOffsetHtml(n)}</td>
              <td class="wb-col-old">
                <select class="old-lesson-select" title="${esc(lessonOptionLabel(o) || '选择旧课')}">${oldLessonOptions(detail, o.lesson_uid || '')}</select>
              </td>
              <td class="wb-col-match"><span class="wb-match-text ${matchMethodClass(it.match_method)}">${esc(matchMethodLabel(it.match_method))}${it.match_method === 'name_edition' && (it.old && it.old.volume_label) ? `<br><span class="hint">${esc(it.old.volume_label)}</span>` : ''}</span></td>
              <td class="wb-col-status"><span class="badge-status badge-${st}">${esc(pairStatusLabel(st))}</span></td>
              ${changeCell}
              <td class="wb-col-actions">
                <div class="workbook-row-actions">
                  ${n.lesson_uid
                    ? `<div class="lesson-pipe-progress" data-new-uid="${esc(n.lesson_uid)}" hidden>
                        <div class="lesson-pipe-bar" aria-hidden="true"><i></i></div>
                        <span class="lesson-pipe-label">0/0</span>
                      </div>`
                    : ''}
                  <select class="change-advice-select${advice ? '' : ' is-empty'}" title="${esc(adviceReason || '改动建议')}" data-pair-id="${esc(it.id)}" data-prev="${esc(advice)}">${adviceOpts}</select>
                  ${href ? `<a class="btn btn-sm" href="${esc(href)}">进入对比</a>` : '<span class="hint">请先选择旧课</span>'}
                </div>
              </td>
            </tr>`;
    };
    const rowsHtml = items.map(renderRow).join('');
    let tableHtml;
    if (useChapterGroup) {
      // 按 unit_title 分组，每章一个可折叠区块
      const groups = new Map();
      for (const it of items) {
        const key = (it.new && it.new.unit_title) || (it.old && it.old.unit_title) || '未分组';
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(it);
      }
      const groupHtmls = [];
      let gi = 0;
      for (const [unitTitle, groupItems] of groups) {
        gi++;
        const groupChanged = groupItems.filter((it) => {
          const cs = it.change_stats || {};
          return cs.has_change || cs.lesson_verdict;
        }).length;
        const groupDone = groupItems.filter((it) => it.pair_status === 'confirmed').length;
        groupHtmls.push(`
          <details class="wb-chapter-group"${gi <= 2 ? ' open' : ''}>
            <summary class="wb-chapter-group-head">
              <span class="wb-chapter-toggle" aria-hidden="true">▾</span>
              <strong>${esc(unitTitle)}</strong>
              <span class="hint">${groupItems.length} 课 · 已确认 ${groupDone} · 有变动 ${groupChanged}</span>
            </summary>
            ${tableHeader}
            <tbody>${groupItems.map(renderRow).join('')}</tbody>
            </table>
          </details>`);
      }
      tableHtml = groupHtmls.join('');
    } else {
      tableHtml = `${tableHeader}<tbody>${rowsHtml}</tbody></table>`;
    }
    wrap.innerHTML = `
      ${overviewHtml}
      ${tableHtml}
      <p class="hint">已确认 ${coarse.confirmed_count || 0} / ${coarse.pair_count || 0} · 状态：待对比 → 待确认（跑完比对）→ 已确认（对比页人工点确认）</p>`;

    wrap.querySelectorAll('tr[data-pair-id]').forEach((tr) => {
      const pairId = tr.dataset.pairId;
      const sel = tr.querySelector('.old-lesson-select');
      sel.addEventListener('change', async () => {
        try {
          const body = { old_lesson_uid: sel.value || '' };
          if (!sel.value) body.clear_old = true;
          const r = await fetch(`${API}/workbook/coarse-pairs/${encodeURIComponent(pairId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          const d = await readJson(r);
          current.detail.coarse = d.coarse;
          renderCoarse(current.detail);
          toast('已更新课对');
        } catch (e) {
          toast(e.message || String(e));
        }
      });
    });

    wrap.querySelectorAll('.change-advice-select').forEach((sel) => {
      sel.addEventListener('change', async () => {
        const pairId = sel.dataset.pairId || sel.closest('tr')?.dataset.pairId;
        const prev = sel.dataset.prev || '';
        if (!pairId) return;
        try {
          const r = await fetch(`${API}/workbook/coarse-pairs/${encodeURIComponent(pairId)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ change_advice: sel.value || '' }),
          });
          const d = await readJson(r);
          current.detail.coarse = d.coarse;
          sel.dataset.prev = sel.value || '';
          sel.classList.toggle('is-empty', !sel.value);
          toast(sel.value ? `已保存改动建议：${sel.value}` : '已清空改动建议');
        } catch (e) {
          sel.value = prev;
          toast(e.message || String(e));
        }
      });
    });

    bindPageOffsetControls(wrap);
  }

  function renderCoarse(detail) {
    updateCoarseChrome(detail);
    if (isDraftCoarse(detail)) {
      renderDraftCoarse(detail);
      return;
    }
    renderLessonCoarse(detail);
  }

  function paintXiaokeOldCatalog(sideOld, lib) {
    const lessons = lib.lessons || [];
    let listHtml;
    if (!lessons.length) {
      listHtml = `<p class="hint">${esc(lib.hint || '旧库暂无该册目录')}</p>
        <p class="hint">目录来自旧库建设只读拉取。请先在旧库对该版本·年级载入基准目录。</p>`;
    } else {
      let curUnit = null;
      const rows = [];
      lessons.forEach((les) => {
        const unit = les.unit_title || '未命名单元';
        if (unit !== curUnit) {
          curUnit = unit;
          rows.push(`<div class="xiaoke-catalog-unit">${esc(unit)}</div>`);
        }
        const pages = les.page_count != null ? ` · ${les.page_count} 页` : '';
        rows.push(
          `<div class="xiaoke-catalog-lesson"><span class="xiaoke-catalog-no">${esc(les.lesson_no || '')}</span>`
          + `<span>${esc(les.lesson_name || '')}</span>`
          + `<span class="hint">${pages}</span></div>`
        );
      });
      listHtml = `<div class="xiaoke-catalog-list">${rows.join('')}</div>`;
    }
    const meta = lib.catalog_ready
      ? `${lib.lesson_count || 0} 课 · ${esc(lib.library_code || '')} · 只读（旧库）`
      : `未找到 · ${esc(lib.library_code || '—')} · 只读`;
    sideOld.innerHTML = `
      <h3>旧教材目录</h3>
      <p class="hint side-mode-hint">${meta}</p>
      ${listHtml}
      <button type="button" class="btn btn-ghost xiaoke-catalog-refresh" id="xiaoke-catalog-refresh-inline">刷新目录</button>`;
    sideOld.querySelector('#xiaoke-catalog-refresh-inline')?.addEventListener('click', () => {
      if (current.old_code && current.new_code) {
        openPair(current.old_code, current.new_code).catch((e) => toast(e.message || String(e)));
      }
    });
  }

  /** 复用旧库 GET /api/old-library/volumes/<code> 拉目录 */
  async function fetchOldLibraryCatalog(libraryCode) {
    const code = String(libraryCode || '').trim();
    if (!code) return null;
    const r = await fetch(`/api/old-library/volumes/${encodeURIComponent(code)}`);
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      return {
        library_code: code,
        catalog_ready: false,
        lesson_count: 0,
        lessons: [],
        hint: d.error || `旧库未找到册次 ${code}`,
      };
    }
    return {
      library_code: d.volume_code || code,
      edition_id: d.edition_id || '',
      catalog_ready: (d.lesson_count || 0) > 0 || (d.lessons || []).length > 0,
      lesson_count: d.lesson_count || (d.lessons || []).length || 0,
      display_title: d.display_title,
      lessons: (d.lessons || []).map((les) => ({
        lesson_uid: les.lesson_uid,
        unit_no: les.unit_no,
        unit_title: les.unit_title,
        lesson_no: les.lesson_no,
        lesson_name: les.lesson_name,
        page_count: les.page_count,
        page_start: les.page_start,
        page_end: les.page_end,
      })),
      hint: null,
    };
  }

  /** 同社全年级旧库目录：优先用 pair 详情；缺失时再拉专用接口 */
  async function fetchXiaokeEditionCatalogs(oldCode) {
    const code = String(oldCode || '').trim();
    if (!code) return null;
    const r = await fetch(
      `${API}/workbook/xiaoke/edition-catalogs?old_code=${encodeURIComponent(code)}`
    );
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) {
      return {
        edition_label: '本社',
        volumes: [],
        ready_count: 0,
        volume_count: 0,
        hint: d.error || '同社全年级目录拉取失败',
      };
    }
    const { ok: _ok, ...payload } = d;
    return payload;
  }

  async function renderXiaokeOldCatalog(detail) {
    const panel = document.getElementById('xiaoke-old-catalog-panel');
    const sideOld = document.getElementById('side-old');
    if (panel) {
      panel.hidden = true;
      panel.style.display = 'none';
    }
    if (!isXiaokeSubject()) {
      document.body.classList.remove('wb-xiaoke-pair');
      const edRoot = document.getElementById('xiaoke-edition-catalogs');
      if (edRoot) edRoot.hidden = true;
      if (sideOld) sideOld.hidden = false;
      return;
    }
    document.body.classList.add('wb-xiaoke-pair');
    if (!sideOld) return;
    sideOld.hidden = false;
    sideOld.innerHTML = '<h3>旧教材目录</h3><p class="hint">正在从旧库拉取…</p>';

    let lib = (detail && detail.old_library) || {};
    // 若 pair 接口未带上目录，直接复用旧库 volume API
    if (!lib.catalog_ready || !(lib.lessons || []).length) {
      const code = lib.library_code
        || (detail && detail.old && detail.old.library_code)
        || '';
      // 从 diff 码推算：XKDX-1S-DOLD → DX-1S-OLD
      let guess = code;
      if (!guess && detail && detail.old_code) {
        const raw = String(detail.old_code).toUpperCase();
        if (raw.startsWith('XK') && raw.endsWith('-DOLD')) {
          guess = `${raw.slice(2, -5)}-OLD`;
        }
      }
      if (guess) {
        try {
          const fetched = await fetchOldLibraryCatalog(guess);
          if (fetched && (fetched.catalog_ready || fetched.library_code)) {
            lib = { ...lib, ...fetched };
            if (detail) detail.old_library = lib;
          }
        } catch (e) {
          lib = {
            ...lib,
            library_code: guess,
            hint: e.message || String(e),
          };
        }
      }
    }
    paintXiaokeOldCatalog(sideOld, lib);
    // 同社全年级：pair 未带齐时自动补拉（新教材常需对照同社多年级旧册）
    let editionPayload = (detail && detail.old_library_edition) || {};
    if (!(editionPayload.volumes || []).length && detail && detail.old_code) {
      try {
        const fetchedEd = await fetchXiaokeEditionCatalogs(detail.old_code);
        if (fetchedEd && (fetchedEd.volumes || []).length) {
          editionPayload = fetchedEd;
          detail.old_library_edition = fetchedEd;
        } else if (fetchedEd) {
          editionPayload = fetchedEd;
          detail.old_library_edition = fetchedEd;
        }
      } catch (e) {
        editionPayload = {
          volumes: [],
          ready_count: 0,
          volume_count: 0,
          hint: e.message || String(e),
        };
        if (detail) detail.old_library_edition = editionPayload;
      }
    }
    renderXiaokeEditionCatalogs(detail);
  }

  function buildXiaokeCatalogListHtml(lessons) {
    if (!lessons || !lessons.length) {
      return '<p class="hint">该册暂无目录</p>';
    }
    let curUnit = null;
    const rows = [];
    lessons.forEach((les) => {
      const unit = les.unit_title || '未命名单元';
      if (unit !== curUnit) {
        curUnit = unit;
        rows.push(`<div class="xiaoke-catalog-unit">${esc(unit)}</div>`);
      }
      const pages = les.page_count != null ? ` · ${les.page_count} 页` : '';
      rows.push(
        `<div class="xiaoke-catalog-lesson"><span class="xiaoke-catalog-no">${esc(les.lesson_no || '')}</span>`
        + `<span>${esc(les.lesson_name || '')}</span>`
        + `<span class="hint">${pages}</span></div>`
      );
    });
    return `<div class="xiaoke-catalog-list xiaoke-edition-list">${rows.join('')}</div>`;
  }

  function renderXiaokeEditionCatalogs(detail) {
    const root = document.getElementById('xiaoke-edition-catalogs');
    const tabsEl = document.getElementById('xiaoke-edition-tabs');
    const bodyEl = document.getElementById('xiaoke-edition-body');
    const metaEl = document.getElementById('xiaoke-edition-catalogs-meta');
    if (!root || !tabsEl || !bodyEl) return;
    if (!isXiaokeSubject()) {
      root.hidden = true;
      root.style.display = 'none';
      return;
    }
    const ed = (detail && detail.old_library_edition) || {};
    const volumes = ed.volumes || [];
    root.hidden = false;
    root.style.display = '';
    if (metaEl) {
      metaEl.textContent = ed.hint
        || `${ed.edition_label || '本社'} · 已载入目录 ${ed.ready_count || 0}/${ed.volume_count || volumes.length} 册（只读）`;
    }
    if (!volumes.length) {
      tabsEl.innerHTML = '';
      bodyEl.innerHTML = `<p class="hint">${esc(ed.hint || '暂无全年级目录')}</p>`;
      return;
    }

    const current = volumes.find((v) => v.is_current);
    const firstReady = volumes.find((v) => v.catalog_ready);
    const pick = current || firstReady || volumes[0];
    let activeKey = `${pick.grade}-${pick.term}`;

    function paintBody(key) {
      const vol = volumes.find((v) => `${v.grade}-${v.term}` === key) || volumes[0];
      if (!vol) {
        bodyEl.innerHTML = '<p class="hint">—</p>';
        return;
      }
      const badge = vol.is_current ? '<span class="xiaoke-edition-current">本册对照</span>' : '';
      const status = vol.catalog_ready
        ? `${vol.lesson_count || 0} 课 · ${esc(vol.library_code || '')}`
        : (vol.in_db ? '已建册、暂无课时' : '旧库未载入');
      bodyEl.innerHTML = `
        <div class="xiaoke-edition-vol-meta">
          <strong>${esc(vol.tab_label || '')}</strong>
          ${badge}
          <span class="hint">${status}</span>
        </div>
        ${vol.catalog_ready
          ? buildXiaokeCatalogListHtml(vol.lessons || [])
          : `<p class="hint">${esc(vol.library_code || '')} 尚无目录，请先在旧库建设载入该册基准目录。</p>`}`;
    }

    tabsEl.innerHTML = volumes.map((v) => {
      const key = `${v.grade}-${v.term}`;
      const cls = [
        'xiaoke-edition-tab',
        key === activeKey ? 'is-active' : '',
        v.catalog_ready ? 'is-ready' : 'is-empty',
        v.is_current ? 'is-current' : '',
      ].filter(Boolean).join(' ');
      return `<button type="button" class="${cls}" role="tab" data-key="${esc(key)}" aria-selected="${key === activeKey ? 'true' : 'false'}">${esc(v.tab_label || key)}${v.catalog_ready ? '' : ' ·'}</button>`;
    }).join('');

    tabsEl.querySelectorAll('.xiaoke-edition-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        activeKey = btn.dataset.key || '';
        tabsEl.querySelectorAll('.xiaoke-edition-tab').forEach((b) => {
          const on = b.dataset.key === activeKey;
          b.classList.toggle('is-active', on);
          b.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        paintBody(activeKey);
      });
    });
    paintBody(activeKey);
  }


  function isChemShelfSubject() {
    return subjectId === 'huaxue' || subject.label === '化学';
  }

  function parseGradeTermFromDiffCode(code) {
    const m = String(code || '').trim().match(/^([A-Z0-9]+)-(\d+)([SX])-/i);
    if (!m) return null;
    return {
      prefix: m[1].toUpperCase(),
      grade: Number(m[2]),
      term: m[3].toUpperCase() === 'X' ? '下' : '上',
    };
  }

  function chemShelfIntakeHref(volumeCode, role) {
    const side = role === 'old' ? 'old' : 'new';
    return `/textbook-diff/${side}/intake?code=${encodeURIComponent(volumeCode)}`;
  }

  function fillChemShelfSelects(items, preferOld, preferNew) {
    const oldSel = document.getElementById('chem-shelf-old-select');
    const newSel = document.getElementById('chem-shelf-new-select');
    if (!oldSel || !newSel) return;
    const opts = (items || []).map((it) => {
      const label = it.name || it.version_label || it.display_title || it.volume_code;
      return `<option value="${esc(it.volume_code)}">${esc(label)} (${esc(it.volume_code)})</option>`;
    }).join('');
    oldSel.innerHTML = opts || '<option value="">—</option>';
    newSel.innerHTML = opts || '<option value="">—</option>';
    if (preferOld) oldSel.value = preferOld;
    if (preferNew) newSel.value = preferNew;
    if (!oldSel.value && items.length) {
      const dold = items.find((i) => String(i.volume_code).endsWith('-DOLD'));
      oldSel.value = (dold || items[0]).volume_code;
    }
    if (!newSel.value && items.length) {
      const dnew = items.find((i) => /DNEW/.test(i.volume_code) && i.volume_code !== oldSel.value);
      newSel.value = (dnew || items[items.length - 1]).volume_code;
    }
    syncChemSpineSelection();
  }

  function syncChemSpineSelection() {
    const oldCode = document.getElementById('chem-shelf-old-select')?.value || '';
    const newCode = document.getElementById('chem-shelf-new-select')?.value || '';
    document.querySelectorAll('#chem-shelf-list .arc-spine').forEach((el) => {
      const code = el.getAttribute('data-code') || '';
      el.classList.toggle('is-old', !!oldCode && code === oldCode);
      el.classList.toggle('is-new', !!newCode && code === newCode);
      const badge = el.querySelector('.arc-spine-badge');
      if (badge) {
        if (code === oldCode && code === newCode) badge.textContent = '两侧';
        else if (code === oldCode) badge.textContent = '旧侧';
        else if (code === newCode) badge.textContent = '新侧';
        else badge.textContent = '';
        badge.hidden = !badge.textContent;
      }
    });
    renderChemPicked();
  }

  function shortSpineName(it) {
    const raw = String((it && (it.name || it.version_label || it.display_title || it.volume_code)) || '');
    const m = raw.match(/^\d{4}-\d{2}-\d{2}\s*[·•]\s*(.+)$/);
    const base = (m ? m[1] : raw).trim();
    if (base.length <= 10) return base;
    return base.slice(0, 10);
  }

  function renderChemPicked() {
    const el = document.getElementById('chem-shelf-picked');
    const oldSel = document.getElementById('chem-shelf-old-select');
    const newSel = document.getElementById('chem-shelf-new-select');
    if (!el || !oldSel || !newSel) return;
    const oldOpt = oldSel.selectedOptions[0];
    const newOpt = newSel.selectedOptions[0];
    const oldLabel = shortSpineName({ name: oldOpt ? oldOpt.textContent.split(' (')[0] : '旧侧' });
    const newLabel = shortSpineName({ name: newOpt ? newOpt.textContent.split(' (')[0] : '新侧' });
    if (!oldSel.value && !newSel.value) {
      el.innerHTML = '';
      return;
    }
    el.innerHTML =
      `<div class="arc-picked-book"><span>${esc(oldLabel || '旧侧')}</span></div>` +
      `<div class="arc-picked-vs">↔</div>` +
      `<div class="arc-picked-book is-new"><span>${esc(newLabel || '新侧')}</span></div>`;
  }

  function renderChemArchives(archives) {
    const el = document.getElementById('chem-shelf-archives');
    if (!el) return;
    const rows = archives || [];
    if (!rows.length) {
      el.innerHTML = '<p class="arc-empty">尚无对比档案。<br>粗分落库后会出现在此。</p>';
      return;
    }
    el.innerHTML = rows.map((a) => (
      `<button type="button" class="arc-archive" data-old="${esc(a.old_code)}" data-new="${esc(a.new_code)}" title="${esc(a.label || '')}">` +
      `<div class="arc-archive-pair">${esc(shortSpineName({ name: a.old_name }))}↔${esc(shortSpineName({ name: a.new_name }))}</div>` +
      `<div class="arc-archive-meta">${a.pair_count || 0}课</div>` +
      `</button>`
    )).join('');
  }

  function renderChemSpines(items, preferOld, preferNew) {
    const listEl = document.getElementById('chem-shelf-list');
    if (!listEl) return;
    if (!items.length) {
      listEl.innerHTML = '<p class="arc-empty">书架空空。<br>请在下方旧/新栏点「上架」。</p>';
      return;
    }
    listEl.innerHTML = items.map((it, idx) => {
      const name = shortSpineName(it);
      const tone = idx % 6;
      const bound = it.blob_bound ? '已绑' : '待传';
      const idxLabel = String(idx + 1).padStart(2, '0');
      return (
        `<div class="arc-spine arc-spine-tone-${tone}" tabindex="0" role="button" data-code="${esc(it.volume_code)}" title="${esc(it.name || it.volume_code)}">` +
        `<span class="arc-spine-badge" hidden></span>` +
        `<span class="arc-spine-idx">${esc(idxLabel)}</span>` +
        `<span class="arc-spine-name">${esc(name)}</span>` +
        `<span class="arc-spine-meta">${esc(bound)}</span>` +
        `<div class="arc-spine-menu" role="menu">` +
        `<button type="button" data-spine-act="old">设为旧侧</button>` +
        `<button type="button" data-spine-act="new">设为新侧</button>` +
        `<button type="button" data-spine-act="rename">改名</button>` +
        `<button type="button" data-spine-act="open">打开预处理</button>` +
        `</div></div>`
      );
    }).join('');
    fillChemShelfSelects(items, preferOld, preferNew);
  }

  async function refreshChemShelf(preferOld, preferNew) {
    const panel = document.getElementById('chem-shelf');
    const listEl = document.getElementById('chem-shelf-list');
    if (!panel || !listEl) return;
    if (!isChemShelfSubject()) {
      panel.hidden = true;
      return;
    }
    const gt = parseGradeTermFromDiffCode(preferNew || preferOld || current.new_code || current.old_code);
    if (!gt || gt.prefix !== 'HXRJ') {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    listEl.innerHTML = '<p class="arc-empty">整理书架中…</p>';
    try {
      const r = await fetch(
        `${API}/workbook/shelf?prefix=${encodeURIComponent(gt.prefix)}&grade=${gt.grade}&term=${encodeURIComponent(gt.term)}`
      );
      const d = await readJson(r);
      const items = d.items || [];
      renderChemSpines(items, preferOld || current.old_code, preferNew || current.new_code);
      renderChemArchives(d.archives || []);
    } catch (e) {
      listEl.innerHTML = `<p class="arc-empty">${esc(e.message || '书架加载失败')}</p>`;
    }
  }

  async function chemShelfAdd(role) {
    const gt = parseGradeTermFromDiffCode(current.new_code || current.old_code);
    if (!gt) {
      toast('无法解析本册年级册次');
      return;
    }
    try {
      const r = await fetch(`${API}/workbook/shelf/items`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          prefix: gt.prefix,
          grade: gt.grade,
          term: gt.term,
          role,
        }),
      });
      const d = await readJson(r);
      toast(d.created ? `已上架：${d.name || d.volume_code}` : (d.message || '已存在'));
      await refreshChemShelf(current.old_code, current.new_code);
      if (d.created && d.volume_code) {
        window.location.href = chemShelfIntakeHref(d.volume_code, role);
      } else if (d.volume_code && role === 'old') {
        const oldSel = document.getElementById('chem-shelf-old-select');
        if (oldSel) {
          oldSel.value = d.volume_code;
          syncChemSpineSelection();
        }
      } else if (d.volume_code && role === 'new') {
        const newSel = document.getElementById('chem-shelf-new-select');
        if (newSel) {
          newSel.value = d.volume_code;
          syncChemSpineSelection();
        }
      }
    } catch (e) {
      toast(e.message || String(e));
    }
  }

  async function chemShelfStartCompare(oldCode, newCode) {
    if (!oldCode || !newCode) {
      toast('请选择旧侧与新侧教材');
      return;
    }
    if (oldCode === newCode) {
      toast('两侧不能是同一本');
      return;
    }
    try {
      const r = await fetch(`${API}/workbook/compare-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_code: oldCode, new_code: newCode }),
      });
      await readJson(r);
      const url = new URL(window.location.href);
      url.searchParams.set('old_code', oldCode);
      url.searchParams.set('new_code', newCode);
      window.history.replaceState({}, '', url.toString());
      await openPair(oldCode, newCode);
    } catch (e) {
      toast(e.message || String(e));
    }
  }

  function wireChemShelf() {
    const start = document.getElementById('chem-shelf-start-compare');
    const listEl = document.getElementById('chem-shelf-list');
    const archivesEl = document.getElementById('chem-shelf-archives');
    const oldSel = document.getElementById('chem-shelf-old-select');
    const newSel = document.getElementById('chem-shelf-new-select');
    if (start) {
      start.onclick = () => chemShelfStartCompare(
        document.getElementById('chem-shelf-old-select')?.value,
        document.getElementById('chem-shelf-new-select')?.value,
      );
    }
    if (oldSel) oldSel.addEventListener('change', syncChemSpineSelection);
    if (newSel) newSel.addEventListener('change', syncChemSpineSelection);
    if (archivesEl) {
      archivesEl.addEventListener('click', (ev) => {
        const btn = ev.target.closest('.arc-archive');
        if (!btn) return;
        chemShelfStartCompare(btn.getAttribute('data-old'), btn.getAttribute('data-new'));
      });
    }
    if (listEl) {
      listEl.addEventListener('click', async (ev) => {
        const actBtn = ev.target.closest('[data-spine-act]');
        const spine = ev.target.closest('.arc-spine');
        if (actBtn && spine) {
          ev.stopPropagation();
          const code = spine.getAttribute('data-code');
          const act = actBtn.getAttribute('data-spine-act');
          spine.classList.remove('is-open');
          if (act === 'old') {
            const sel = document.getElementById('chem-shelf-old-select');
            if (sel) { sel.value = code; syncChemSpineSelection(); }
            return;
          }
          if (act === 'new') {
            const sel = document.getElementById('chem-shelf-new-select');
            if (sel) { sel.value = code; syncChemSpineSelection(); }
            return;
          }
          if (act === 'open') {
            const role = /DOLD$/.test(code) ? 'old' : 'new';
            window.location.href = chemShelfIntakeHref(code, role);
            return;
          }
          if (act === 'rename') {
            const next = window.prompt('输入书脊显示名');
            if (next == null) return;
            try {
              const r = await fetch(`${API}/workbook/shelf/items/${encodeURIComponent(code)}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ version_label: next }),
              });
              await readJson(r);
              toast('已改名');
              await refreshChemShelf(current.old_code, current.new_code);
            } catch (e) {
              toast(e.message || String(e));
            }
          }
          return;
        }
        if (!spine) return;
        document.querySelectorAll('#chem-shelf-list .arc-spine.is-open').forEach((el) => {
          if (el !== spine) el.classList.remove('is-open');
        });
        spine.classList.toggle('is-open');
      });
    }
    document.addEventListener('click', (ev) => {
      if (ev.target.closest('#chem-shelf-list .arc-spine')) return;
      document.querySelectorAll('#chem-shelf-list .arc-spine.is-open').forEach((el) => {
        el.classList.remove('is-open');
      });
    });
  }

  async function openPair(oldCode, newCode) {
    const coarseWrap = document.getElementById('coarse-table');
    if (coarseWrap) coarseWrap.innerHTML = '<p class="hint">加载本册状态中…</p>';
    updateCoarseChrome(null, { busy: true });
    stopPipelinePoll();
    pipelineSeenDone = new Set();
    pipelineSeeded = false;
    pipelineHadRunning = false;
    const r = await fetch(
      `${API}/workbook/pair?old_code=${encodeURIComponent(oldCode)}&new_code=${encodeURIComponent(newCode)}`
    );
    const d = await readJson(r);
    current = { old_code: oldCode, new_code: newCode, detail: d };
    if (isSandboxSubject()) {
      sideViewMode.old = loadSideViewMode('old', oldCode, d.old);
      sideViewMode.new = loadSideViewMode('new', newCode, d.new);
      sideJobUi.old.track = sideViewMode.old;
      sideJobUi.new.track = sideViewMode.new;
    }
    const listPanel = document.getElementById('list-panel');
    if (listPanel) listPanel.hidden = true;
    document.getElementById('pair-panel').hidden = false;
    document.getElementById('coarse-panel').hidden = false;
    document.getElementById('pair-meta').textContent =
      `${d.subject} · ${d.edition} · ${d.grade}年级${d.semester} · ${oldCode} ↔ ${newCode}`;
    if (lead) {
      const title = `${d.edition || ''}${d.subject || ''}${d.grade || ''}年级${d.semester || ''}`;
      lead.textContent = isXiaokeSubject()
        ? `${title} — 上传新侧 PDF → 整册预处理（同新库建设：目录·页码·页图·粗分）`
        : (subjectId === 'shuxue'
          ? `${title} — 上传旧/新 PDF → 自动识别 → 按章对比`
          : `${title} — 预处理 → 粗分 → 对比`);
    }
    const pairTitle = document.getElementById('pair-section-title')
      || document.querySelector('#pair-panel .diff-section-title');
    if (pairTitle) {
      pairTitle.textContent = isXiaokeSubject()
        ? '① 旧库目录 · 新教材预处理'
        : (subjectId === 'shuxue'
          ? '① 上传旧/新教材 PDF'
          : '② 整册预处理（旧 / 新 各做一遍）');
    }
    if (isXiaokeSubject()) {
      await renderXiaokeOldCatalog(d);
      // 新侧改用新库建设同款上传+整册预处理卡片
      const sideNew = document.getElementById('side-new');
      if (sideNew) sideNew.hidden = true;
    } else if (subjectId === 'shuxue') {
      renderMathPairPanel(d);
    } else {
      renderSide(document.getElementById('side-old'), 'old', d.old);
      renderSide(document.getElementById('side-new'), 'new', d.new);
    }
    renderCoarse(d);
    updateRecognizeBar();
    await refreshChemShelf(oldCode, newCode);
  }

  // ===== 数学 pair 页：上传即识别 + 章节呈现 =====
  let mathPipelineBusy = false;
  let mathActiveStep = null;

  function isMathSubject() {
    return subjectId === 'shuxue' || subject.label === '数学';
  }

  const MATH_PIPELINE_SEGS = [
    { key: 'upload', name: '上传' },
    { key: 'catalog', name: '目录' },
    { key: 'parse', name: '划分' },
    { key: 'pages', name: '页图' },
    { key: 'coarse', name: '粗分' },
    { key: 'compare', name: '对比' },
  ];

  function mathSegStatusClass(status) {
    if (status === 'running') return 'is-running';
    if (status === 'done') return 'is-done';
    if (status === 'error') return 'is-error';
    return 'is-pending';
  }

  function mathSegTrackHtml(segments, label) {
    const cols = MATH_PIPELINE_SEGS.map((s) => {
      const st = segments[s.key] || 'pending';
      const cls = mathSegStatusClass(st);
      return (
        `<div class="block-batch-segcol">` +
        `<span class="block-batch-seg seg-${s.key} ${cls}"></span>` +
        `<span class="block-batch-seg-name ${cls}">${esc(s.name)}</span>` +
        `</div>`
      );
    }).join('');
    return (
      `<div class="block-batch-progress-row">` +
      `<div class="block-batch-segwrap" role="progressbar">` +
      `<div class="block-batch-segtrack">${cols}</div>` +
      `</div>` +
      (label ? `<span class="cw-batch-progress-label">${esc(label)}</span>` : '') +
      `</div>`
    );
  }

  function mathDeriveSegments(detail) {
    const oldS = detail.old || {};
    const newS = detail.new || {};
    const segs = {
      upload: 'pending',
      catalog: 'pending',
      parse: 'pending',
      pages: 'pending',
      coarse: 'pending',
      compare: 'pending',
    };
    const oldPdf = !!oldS.has_pdf;
    const newPdf = !!newS.has_pdf;
    if (oldPdf || newPdf) segs.upload = 'done';
    if (oldPdf && newPdf) segs.upload = 'done';
    else if (oldPdf || newPdf) segs.upload = 'running';
    if (oldS.catalog_ready && newS.catalog_ready) segs.catalog = 'done';
    else if (oldPdf && newPdf) segs.catalog = 'pending';
    if (oldS.parse_status === 'done' && newS.parse_status === 'done') segs.parse = 'done';
    else if (oldS.parse_status === 'processing' || newS.parse_status === 'processing') segs.parse = 'running';
    else if (oldS.parse_status === 'failed' || newS.parse_status === 'failed') segs.parse = 'error';
    if (oldS.page_images_ready && newS.page_images_ready) segs.pages = 'done';
    const coarse = detail.coarse || {};
    if ((coarse.items || []).length > 0) segs.coarse = 'done';
    if (coarse.chapter_overview && (coarse.chapter_overview.chapter_count || 0) > 0) segs.compare = 'done';
    if (mathActiveStep) segs[mathActiveStep] = 'running';
    return segs;
  }

  function mathDeriveLabel(detail) {
    if (mathActiveStep === 'catalog') return '识别目录中…';
    if (mathActiveStep === 'parse') return '划分页码中…';
    if (mathActiveStep === 'pages') return '生成页图中…';
    if (mathActiveStep === 'coarse') return '粗分对照中…';
    if (mathActiveStep === 'compare') return '整册对比中…';
    const oldS = detail.old || {};
    const newS = detail.new || {};
    if (!oldS.has_pdf || !newS.has_pdf) return '请上传两侧 PDF';
    if (!oldS.catalog_ready || !newS.catalog_ready) return '待识别目录';
    if (oldS.parse_status === 'processing' || newS.parse_status === 'processing') return '划分页码中…';
    if (oldS.parse_status === 'failed' || newS.parse_status === 'failed') return '划分失败，请重试';
    if (!oldS.page_images_ready || !newS.page_images_ready) return '待生成页图';
    const coarse = detail.coarse || {};
    if (!(coarse.items || []).length) return '待粗分';
    if (mathPipelineBusy) return '整册对比进行中…';
    return '全部完成';
  }

  function renderMathPairPanel(detail) {
    const pairPanel = document.getElementById('pair-panel');
    if (!pairPanel) return;
    const oldS = detail.old || {};
    const newS = detail.new || {};
    const oldPdf = !!oldS.has_pdf;
    const newPdf = !!newS.has_pdf;
    const bothUploaded = oldPdf && newPdf;

    // 渲染两侧上传 slot + 进度
    const sideOld = document.getElementById('side-old');
    const sideNew = document.getElementById('side-new');
    const sides = [['old', '旧教材', oldS, oldPdf], ['new', '新教材', newS, newPdf]];
    [sideOld, sideNew].forEach((el, i) => {
      if (!el) return;
      const [key, name, side, hasPdf] = sides[i];
      const code = side.volume_code || '';
      el.hidden = false;
      el.innerHTML = `
        <h3>${name} · ${esc(side.display_title || code)}</h3>
        ${hasPdf
          ? `<p class="hint">已上传 PDF${side.lesson_count ? ` · 目录 ${side.lesson_count} 课` : ''}</p>`
          : '<p class="hint">请上传 PDF</p>'}
        <div class="pdf-upload-actions">
          <label class="btn secondary pdf-file-btn">
            <input type="file" accept="application/pdf,.pdf" data-math-upload="${key}" data-code="${esc(code)}">
            ${hasPdf ? '重新上传' : `上传${name} PDF`}
          </label>
        </div>`;
    });

    // 隐藏非数学的预处理块
    const prepBlock = document.getElementById('wb-preprocess-block');
    if (prepBlock) prepBlock.hidden = true;

    // 渲染进度条
    let progEl = document.getElementById('math-pipeline-progress');
    if (!progEl) {
      progEl = document.createElement('div');
      progEl.id = 'math-pipeline-progress';
      progEl.className = 'intake-layer-progress intake-layer-progress--volume';
      const sidesWrap = document.querySelector('.workbook-sides');
      if (sidesWrap) sidesWrap.after(progEl);
    }
    const segs = mathDeriveSegments(detail);
    const label = mathDeriveLabel(detail);
    const anyActive = Object.values(segs).some((s) => s === 'done' || s === 'running');
    progEl.hidden = !anyActive && !mathActiveStep;
    if (!progEl.hidden) progEl.innerHTML = mathSegTrackHtml(segs, label);

    // 绑定上传事件
    pairPanel.querySelectorAll('[data-math-upload]').forEach((input) => {
      if (input.dataset.bound) return;
      input.dataset.bound = '1';
      input.addEventListener('change', async (ev) => {
        const file = ev.target.files[0];
        if (!file) return;
        const sideKey = input.dataset.upload;
        const code = input.dataset.code;
        await mathUploadPdf(code, sideKey, file);
      });
    });

    // 两侧都有 PDF 且未开始过 → 自动串联
    if (bothUploaded && !mathPipelineBusy && !mathActiveStep) {
      const coarse = detail.coarse || {};
      const needWork = !oldS.catalog_ready || !newS.catalog_ready
        || oldS.parse_status !== 'done' || newS.parse_status !== 'done'
        || !oldS.page_images_ready || !newS.page_images_ready
        || !(coarse.items || []).length;
      if (needWork) {
        mathRunPipeline(detail).catch((e) => toast(e.message || String(e)));
      }
    }
  }

  async function mathUploadPdf(volumeCode, sideKey, file) {
    const sizeMb = (file.size / 1024 / 1024).toFixed(1);
    toast(`上传中（${sizeMb} MB）…`);
    try {
      const fd = new FormData();
      fd.append('pdf', file);
      fd.append('pdf_role', 'full');
      const r = await fetch(`${API}/volumes/${encodeURIComponent(volumeCode)}/pdf`, {
        method: 'POST',
        body: fd,
      });
      const d = await readJson(r);
      toast(d.ok !== false ? `${sideKey === 'old' ? '旧' : '新'}侧 PDF 已上传` : (d.error || '上传失败'));
      await mathRefreshPair();
    } catch (e) {
      toast(e.message || String(e));
    }
  }

  async function mathRefreshPair() {
    if (!current.old_code || !current.new_code) return;
    const r = await fetch(
      `${API}/workbook/pair?old_code=${encodeURIComponent(current.old_code)}&new_code=${encodeURIComponent(current.new_code)}`
    );
    const d = await readJson(r);
    current.detail = d;
    renderMathPairPanel(d);
    renderCoarse(d);
  }

  async function mathWaitParseDone(volumeCode, sideLabel) {
    const maxMs = 20 * 60 * 1000;
    const started = Date.now();
    while (Date.now() - started < maxMs) {
      await new Promise((r) => setTimeout(r, 3000));
      const r = await fetch(`${API}/volumes/${encodeURIComponent(volumeCode)}`);
      const d = await readJson(r);
      if (d.parse_status === 'done') return d;
      if (d.parse_status === 'failed') {
        throw new Error(d.parse_error || `${sideLabel}侧划分失败`);
      }
      const waited = Math.round((Date.now() - started) / 1000);
      const progEl = document.getElementById('math-pipeline-progress');
      if (progEl) progEl.innerHTML = mathSegTrackHtml(mathDeriveSegments(current.detail), `${sideLabel}侧划分页码中… 已等待 ${waited} 秒`);
    }
    throw new Error(`${sideLabel}侧划分仍在进行，请稍后查看`);
  }

  async function mathRunPipeline(detail) {
    if (mathPipelineBusy) return;
    mathPipelineBusy = true;
    try {
      const oldCode = current.old_code;
      const newCode = current.new_code;
      const oldS = detail.old || {};
      const newS = detail.new || {};

      // 目录识别
      if (!oldS.catalog_ready || !newS.catalog_ready) {
        mathActiveStep = 'catalog';
        renderMathPairPanel(current.detail);
        for (const [code, label] of [[oldCode, '旧'], [newCode, '新']]) {
          const r = await fetch(`${API}/volumes/${encodeURIComponent(code)}/catalog-from-pdf`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ replace: true }),
          });
          await readJson(r);
        }
        await mathRefreshPair();
      }

      // 页码划分
      const cur = current.detail;
      const oldS2 = cur.old || {};
      const newS2 = cur.new || {};
      if (oldS2.parse_status !== 'done' || newS2.parse_status !== 'done') {
        mathActiveStep = 'parse';
        renderMathPairPanel(current.detail);
        for (const [code, label] of [[oldCode, '旧'], [newCode, '新']]) {
          const v = current.detail[label === '旧' ? 'old' : 'new'];
          if (v.parse_status === 'done') continue;
          const r = await fetch(`${API}/volumes/${encodeURIComponent(code)}/parse`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ replace_lessons: false, force_recalibrate: false }),
          });
          const d = await r.json().catch(() => ({}));
          if (d.parse_status === 'done' && d.already_done) continue;
          await mathWaitParseDone(code, label);
        }
        await mathRefreshPair();
      }

      // 页图
      const cur2 = current.detail;
      const oldS3 = cur2.old || {};
      const newS3 = cur2.new || {};
      if (!oldS3.page_images_ready || !newS3.page_images_ready) {
        mathActiveStep = 'pages';
        renderMathPairPanel(current.detail);
        for (const [code] of [[oldCode], [newCode]]) {
          const r = await fetch(`${API}/volumes/${encodeURIComponent(code)}/lesson-pages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({}),
          });
          await readJson(r);
        }
        await mathRefreshPair();
      }

      // 粗分
      const cur3 = current.detail;
      const coarse = cur3.coarse || {};
      if (!(coarse.items || []).length) {
        mathActiveStep = 'coarse';
        renderMathPairPanel(current.detail);
        const r = await fetch(`${API}/workbook/coarse-match`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ old_code: oldCode, new_code: newCode, replace: true }),
        });
        await readJson(r);
        await mathRefreshPair();
      }

      // 整册一键对比
      mathActiveStep = 'compare';
      renderMathPairPanel(current.detail);
      toast('整册对比已启动…');
      await startPipeline({ scope: 'volume', skipConfirm: true });

      mathActiveStep = null;
      await mathRefreshPair();
      toast('整册识别与对比完成');
    } catch (e) {
      toast(e.message || String(e));
    } finally {
      mathActiveStep = null;
      mathPipelineBusy = false;
      await mathRefreshPair();
    }
  }

  let xiaokeEditions = [];
  let xiaokeActiveSystem = '63';
  let xiaokeActiveEditionId = null;

  function xiaokeTermToken(term) {
    return term === '下' ? 'X' : 'S';
  }

  /** 前端 subjects.js 目录为权威版本列表；API 只补齐册次落库状态 */
  function seedXiaokeEditionsFromSubject() {
    return (subject.editions || []).map((e) => {
      const prefix = String(e.code_prefix || '').trim().toUpperCase();
      return {
        edition_id: e.id,
        label: e.label,
        school_system: e.school_system || '63',
        diff_prefix: prefix,
        volumes: (e.grades || []).map((g) => {
          const token = `${g.grade}${xiaokeTermToken(g.term)}`;
          const basePrefix = prefix.startsWith('XK') ? prefix.slice(2) : prefix;
          return {
            grade: g.grade,
            term: g.term,
            old_code: prefix ? `${prefix}-${token}-DOLD` : '',
            new_code: prefix ? `${prefix}-${token}-DNEW` : '',
            library_code: basePrefix ? `${basePrefix}-${token}-OLD` : '',
            library_lesson_count: 0,
            library_catalog_ready: false,
            in_db: false,
            old_lesson_count: 0,
            new_lesson_count: 0,
            old_ready: false,
            new_ready: false,
          };
        }),
      };
    });
  }

  function mergeXiaokeEditions(seed, apiRows) {
    const apiById = new Map((apiRows || []).map((e) => [e.edition_id, e]));
    // subjects.js 为权威目录（含沪科技五四等）；API 只补齐册次落库状态
    const seedList = seed && seed.length ? seed : (apiRows || []);
    return seedList.map((base) => {
      const api = apiById.get(base.edition_id);
      if (!api) return base;
      const volumes = (api.volumes || []).map((v) => {
        const libN = Number(v.library_lesson_count || 0);
        return {
          ...v,
          grade: Number(v.grade),
          term: v.term === '下' ? '下' : '上',
          library_lesson_count: libN,
          library_catalog_ready: !!(v.library_catalog_ready || libN > 0),
          old_ready: !!(v.old_ready || v.library_catalog_ready || libN > 0),
        };
      });
      return {
        ...base,
        ...api,
        label: base.label || api.label,
        school_system: String(base.school_system || api.school_system || '63'),
        volumes: volumes.length ? volumes : (base.volumes || []),
      };
    });
  }

  /** 再叠一层旧库 /editions，与旧库建设页同源绑定「有无目录」 */
  async function overlayOldLibraryEditionStatus(editions) {
    try {
      const r = await fetch('/api/old-library/editions');
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.ok === false || !Array.isArray(d.editions)) return editions;
      const byId = new Map(d.editions.map((e) => [e.edition_id, e]));
      return (editions || []).map((ed) => {
        const oldEd = byId.get(ed.edition_id);
        if (!oldEd) return ed;
        const volMap = new Map(
          (oldEd.volumes || []).map((v) => [`${Number(v.grade)}-${v.term === '下' ? '下' : '上'}`, v])
        );
        return {
          ...ed,
          volumes: (ed.volumes || []).map((v) => {
            const key = `${Number(v.grade)}-${v.term === '下' ? '下' : '上'}`;
            const hit = volMap.get(key);
            if (!hit) return v;
            const libN = Number(hit.lesson_count || 0);
            return {
              ...v,
              library_code: hit.volume_code || v.library_code,
              library_lesson_count: libN,
              library_catalog_ready: libN > 0,
              old_ready: libN > 0,
            };
          }),
        };
      });
    } catch (e) {
      return editions;
    }
  }

  function xiaokeVolTitle(v) {
    return `${v.grade}年级${v.term === '上' ? '上册' : '下册'}`;
  }

  function xiaokeEditionTabLabel(ed) {
    if (ed.school_system === '54' && ed.label === '青岛版') return '青岛版（五四）';
    if (ed.school_system === '54' && (ed.label === '沪科技版' || ed.edition_id === 'hukexue_54')) {
      return '沪科技版（五四）';
    }
    return ed.label;
  }

  function xiaokeFormatVolStatus(status) {
    const m = String(status || '').match(/^(\d+ 课) · (.+)$/);
    if (m) return `<strong>${m[1]}</strong> · ${m[2]}`;
    return esc(status);
  }

  function xiaokeHasLibraryCatalog(v) {
    return !!(v && (v.library_catalog_ready || Number(v.library_lesson_count || 0) > 0));
  }

  function xiaokeParseStatus(v) {
    const libN = Number(v.library_lesson_count || 0);
    const newN = Number(v.new_lesson_count || 0);
    if (!xiaokeHasLibraryCatalog(v)) return '旧库暂无目录（只读，请先在旧库建设载入）';
    if (!newN) return `已有目录 · ${libN} 课`;
    if (v.new_ready) return `已有目录 · 新侧 ${newN} 课 · 可粗分`;
    return `已有目录 · 新侧 ${newN} 课`;
  }

  function xiaokeProgressBar(v) {
    const steps = [
      { label: '旧目录', done: xiaokeHasLibraryCatalog(v) },
      { label: '新识别', done: !!(v.new_ready || (v.new_lesson_count || 0) > 0) },
      { label: '可对比', done: !!(xiaokeHasLibraryCatalog(v) && v.new_ready) },
    ];
    if (!steps.some((s) => s.done)) return '';
    const segs = steps.map((s) =>
      `<span class="vol-progress-seg${s.done ? ' done' : ''}"></span>`
    ).join('');
    const labels = steps.map((s) =>
      `<span class="${s.done ? 'is-done' : ''}">${s.label}</span>`
    ).join('');
    return `<div class="vol-progress-bar" aria-hidden="true">${segs}</div>
      <div class="vol-step-labels">${labels}</div>`;
  }

  function syncXiaokeEditionUrl() {
    try {
      const u = new URL(window.location.href);
      if (xiaokeActiveEditionId) u.searchParams.set('edition', xiaokeActiveEditionId);
      else u.searchParams.delete('edition');
      const next = u.pathname + u.search;
      const cur = window.location.pathname + window.location.search;
      if (next !== cur) history.replaceState(null, '', next);
    } catch (e) { /* ignore */ }
  }

  /** 小科列表页：整页改用旧库建设同款 wb-hero / wb-main / wb-panel 分布 */
  function applyXiaokeWorkbenchShell() {
    document.body.classList.remove('diff-page', 'workbook-page');
    document.body.classList.add('workbench-page', 'workbench-old', 'wb-xiaoke');
    document.title = '小科建设 · 新旧教材对比';

    const hero = document.querySelector('header.diff-hero, header.wb-hero');
    if (hero) {
      hero.className = 'wb-hero';
      const backQ = window.diffSubjectQuery
        ? window.diffSubjectQuery(subjectId)
        : `subject=${encodeURIComponent(subjectId)}`;
      hero.innerHTML = `
        <div class="wb-hero-inner">
          <a class="wb-back" href="/textbook-diff/?${backQ}">← 选择学科</a>
          <h1>小科建设</h1>
          <p class="wb-lead">小学科学对比沙箱 · 只读拉取旧库已有目录，进入本册后上传新教材对照</p>
          <ol class="wb-flow" aria-label="建设流程">
            <li><span class="wb-flow-num">1</span>选版本 · 点册次进入</li>
            <li><span class="wb-flow-num">2</span>左侧看旧目录 · 右侧传新 PDF</li>
            <li><span class="wb-flow-num">3</span>一键识别 · 粗分对比</li>
          </ol>
        </div>`;
    }

    const main = document.querySelector('main.diff-main, main.wb-main');
    if (main) main.className = 'wb-main';

    const listPanel = document.getElementById('list-panel');
    if (listPanel) {
      listPanel.className = 'wb-panel';
      listPanel.hidden = false;
    }
    const hideEl = (id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.hidden = true;
      el.style.display = 'none';
    };
    hideEl('list-panel-title');
    hideEl('list-panel-lead');
    hideEl('create-form');
    hideEl('pair-list');
    hideEl('pair-panel');
    hideEl('coarse-panel');
    hideEl('xiaoke-old-catalog-panel');
    hideEl('xiaoke-edition-catalogs');

    const gate = document.getElementById('xiaoke-editions') || document.getElementById('editions');
    if (gate) {
      gate.hidden = false;
      gate.style.display = '';
      gate.className = '';
      gate.id = 'editions';
    }
  }

  function countXiaokeMissingCatalog(ed) {
    return (ed.volumes || []).filter((v) => !xiaokeHasLibraryCatalog(v)).length;
  }

  async function refreshXiaokeEditionsStatus() {
    const seed = seedXiaokeEditionsFromSubject();
    try {
      const r = await fetch(`${API}/workbook/xiaoke/editions`);
      const d = await readJson(r);
      xiaokeEditions = mergeXiaokeEditions(seed, d.editions || []);
    } catch (e) {
      xiaokeEditions = seed;
      throw e;
    }
    // 与旧库建设页同源再覆盖一遍目录状态
    xiaokeEditions = await overlayOldLibraryEditionStatus(xiaokeEditions);
    renderXiaokeGate();
  }

  let xkBatchFiles = [];
  let xkBatchPlan = [];
  let xkBatchEditionId = null;
  const xkBatchProgress = new Map();
  let xkBatchUploading = false;
  let xkPreprocessPollTimer = null;
  let xkPreprocessEditionId = null;

  function assertXiaokeBatchEdition() {
    if (!xiaokeActiveEditionId) throw new Error('请先选择版本');
    if (xkBatchEditionId && xkBatchEditionId !== xiaokeActiveEditionId) {
      throw new Error('已切换版本，请重新选择本版 PDF 后再上传');
    }
  }

  function parkXiaokeBatchPanel() {
    const panel = document.getElementById('xk-batch-panel');
    const park = document.getElementById('xk-batch-park');
    if (panel && park && panel.parentElement !== park) park.appendChild(panel);
  }

  function placeXiaokeBatchPanel() {
    const panel = document.getElementById('xk-batch-panel');
    const mount = document.getElementById('xk-batch-mount');
    if (panel && mount) mount.appendChild(panel);
  }

  function parkXiaokePreprocessPanel() {
    const panel = document.getElementById('xk-prep-panel');
    const park = document.getElementById('xk-batch-park');
    if (panel && park && panel.parentElement !== park) park.appendChild(panel);
  }

  function placeXiaokePreprocessPanel() {
    const panel = document.getElementById('xk-prep-panel');
    const mount = document.getElementById('xk-batch-mount');
    if (panel && mount) mount.appendChild(panel);
  }

  function parkXiaokeCoarseExportPanel() {
    const panel = document.getElementById('xk-coarse-export-panel');
    const park = document.getElementById('xk-batch-park');
    if (panel && park && panel.parentElement !== park) park.appendChild(panel);
  }

  function placeXiaokeCoarseExportPanel() {
    const panel = document.getElementById('xk-coarse-export-panel');
    const mount = document.getElementById('xk-coarse-export-mount');
    if (panel && mount) mount.appendChild(panel);
  }

  function xiaokeActiveVolumes() {
    const ed = xiaokeEditions.find((e) => e.edition_id === xiaokeActiveEditionId);
    return ed?.volumes || [];
  }

  function xiaokeVolumeSelectHtml(selectedCode) {
    const vols = xiaokeActiveVolumes();
    const opts = ['<option value="">— 未匹配 —</option>'];
    for (const v of vols) {
      const code = v.new_code || '';
      const title = xiaokeVolTitle(v);
      const sel = code && code === selectedCode ? ' selected' : '';
      opts.push(`<option value="${esc(code)}"${sel}>${esc(title)} (${esc(code)})</option>`);
    }
    return opts.join('');
  }

  function syncXiaokeBatchBar() {
    const previewBtn = document.getElementById('xk-batch-preview-btn');
    const uploadBtn = document.getElementById('xk-batch-upload-btn');
    const hasFiles = xkBatchFiles.length > 0;
    const mapped = xkBatchPlan.filter((p) => p.volume_code).length;
    if (previewBtn) previewBtn.disabled = !hasFiles || xkBatchUploading;
    if (uploadBtn) uploadBtn.disabled = !hasFiles || !mapped || xkBatchUploading;
  }

  function renderXiaokeBatchPanel() {
    const panel = document.getElementById('xk-batch-panel');
    const tbody = document.getElementById('xk-batch-rows');
    const summary = document.getElementById('xk-batch-summary');
    if (!panel || !tbody) return;
    if (!xkBatchFiles.length) {
      panel.hidden = true;
      tbody.innerHTML = '';
      if (summary) summary.textContent = '';
      syncXiaokeBatchBar();
      return;
    }
    placeXiaokeBatchPanel();
    panel.hidden = false;
    const planByName = new Map(xkBatchPlan.map((p) => [p.filename, p]));
    const matched = xkBatchPlan.filter((p) => p.volume_code).length;
    if (summary) summary.textContent = `已选 ${xkBatchFiles.length} 个 · 已匹配 ${matched} 个`;
    tbody.innerHTML = xkBatchFiles.map((file) => {
      const plan = planByName.get(file.name) || {};
      const prog = xkBatchProgress.get(file.name) || { percent: 0, state: 'idle', label: '待上传' };
      const statusClass = plan.volume_code ? 'tb-batch-ok' : 'tb-batch-warn';
      const statusText = plan.volume_code
        ? (plan.has_pdf ? '已有 PDF' : '可上传')
        : (plan.reason || '未匹配');
      return `<tr>
        <td class="tb-batch-filename" title="${esc(file.name)}">${esc(file.name.length > 36 ? `${file.name.slice(0, 35)}…` : file.name)}</td>
        <td><select class="tb-batch-vol-select" data-filename="${esc(file.name)}">${xiaokeVolumeSelectHtml(plan.volume_code || '')}</select></td>
        <td>${plan.score != null ? Number(plan.score).toFixed(2) : '—'}</td>
        <td class="${statusClass}">${esc(statusText)}</td>
        <td class="tb-batch-progress-cell">
          <div class="tb-batch-progress tb-batch-progress--${prog.state}" data-progress-file="${esc(file.name)}">
            <div class="tb-batch-progress-bar" style="width:${prog.percent || 0}%"></div>
          </div>
          <span class="tb-batch-progress-label tb-batch-progress--${prog.state}">${esc(prog.label || '')}</span>
        </td>
      </tr>`;
    }).join('');
    tbody.querySelectorAll('.tb-batch-vol-select').forEach((sel) => {
      sel.addEventListener('change', () => {
        const fn = sel.dataset.filename;
        let row = xkBatchPlan.find((p) => p.filename === fn);
        if (!row) {
          row = { filename: fn };
          xkBatchPlan.push(row);
        }
        row.volume_code = sel.value || '';
        row.score = sel.value ? 1 : 0;
        row.reason = sel.value ? '手动指定' : '';
        syncXiaokeBatchBar();
      });
    });
    syncXiaokeBatchBar();
  }

  async function previewXiaokeBatchMatch() {
    assertXiaokeBatchEdition();
    if (!xkBatchFiles.length) {
      toast('请先选择 PDF');
      return;
    }
    const btn = document.getElementById('xk-batch-preview-btn');
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(
        `${API}/workbook/xiaoke/editions/${encodeURIComponent(xiaokeActiveEditionId)}/import-textbooks?preview=1`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filenames: xkBatchFiles.map((f) => f.name) }),
        },
      );
      const d = await readJson(r);
      xkBatchPlan = (d.matches || []).map((m) => ({ ...m }));
      for (const u of d.unmatched || []) {
        if (!xkBatchPlan.find((p) => p.filename === u.filename)) {
          xkBatchPlan.push({ filename: u.filename, volume_code: '', reason: u.reason || '未匹配' });
        }
      }
      renderXiaokeBatchPanel();
      const unmatchedN = (d.unmatched || []).length;
      toast(
        unmatchedN
          ? `匹配完成：${(d.matches || []).length} 个，未匹配 ${unmatchedN} 个（需文件名含版本或册次码）`
          : `匹配完成：${(d.matches || []).length} 个`,
      );
    } catch (e) {
      toast(e.message || String(e));
    } finally {
      syncXiaokeBatchBar();
    }
  }

  function uploadXiaokeOneFile(file, volumeCode) {
    return new Promise((resolve, reject) => {
      const fd = new FormData();
      fd.append('pdf', file, file.name);
      fd.append('only_missing', '1');
      fd.append('mapping', JSON.stringify([{ filename: file.name, volume_code: volumeCode }]));
      const xhr = new XMLHttpRequest();
      xhr.open(
        'POST',
        `${API}/workbook/xiaoke/editions/${encodeURIComponent(xiaokeActiveEditionId)}/import-textbooks`,
      );
      xhr.upload.onprogress = (ev) => {
        if (!ev.lengthComputable) return;
        const percent = Math.round((ev.loaded / ev.total) * 100);
        xkBatchProgress.set(file.name, { percent, state: 'uploading', label: `${percent}%` });
        renderXiaokeBatchPanel();
      };
      xhr.onload = () => {
        try {
          const d = JSON.parse(xhr.responseText || '{}');
          if (xhr.status >= 400 || d.ok === false) {
            reject(new Error(d.error || `HTTP ${xhr.status}`));
            return;
          }
          if ((d.error_count || 0) > 0 && (d.errors || []).length) {
            reject(new Error(d.errors[0].error || '上传失败'));
            return;
          }
          if ((d.imported_count || 0) > 0) {
            resolve({ ...d, outcome: 'imported' });
            return;
          }
          if ((d.skipped_count || 0) > 0) {
            resolve({ ...d, outcome: 'skipped' });
            return;
          }
          reject(new Error('未导入任何文件'));
        } catch (e) {
          reject(e);
        }
      };
      xhr.onerror = () => reject(new Error('网络错误'));
      xhr.send(fd);
    });
  }

  async function uploadXiaokeBatch() {
    assertXiaokeBatchEdition();
    const mapped = xkBatchPlan.filter((p) => p.volume_code);
    if (!mapped.length) throw new Error('请先预览匹配或手动指定册次');
    // 同一册只保留首个文件，避免后写静默跳过
    const seenCodes = new Set();
    const deduped = [];
    for (const plan of mapped) {
      if (seenCodes.has(plan.volume_code)) continue;
      seenCodes.add(plan.volume_code);
      deduped.push(plan);
    }
    xkBatchUploading = true;
    syncXiaokeBatchBar();
    let ok = 0;
    let skip = 0;
    let fail = 0;
    try {
      for (const plan of deduped) {
        const file = xkBatchFiles.find((f) => f.name === plan.filename);
        if (!file) continue;
        try {
          xkBatchProgress.set(file.name, { percent: 0, state: 'uploading', label: '上传中…' });
          renderXiaokeBatchPanel();
          const d = await uploadXiaokeOneFile(file, plan.volume_code);
          if (d.outcome === 'skipped') {
            xkBatchProgress.set(file.name, { percent: 100, state: 'done', label: '已有 PDF，已跳过' });
            skip += 1;
          } else {
            xkBatchProgress.set(file.name, { percent: 100, state: 'done', label: '完成' });
            ok += 1;
          }
        } catch (e) {
          xkBatchProgress.set(file.name, { percent: 0, state: 'error', label: e.message || '失败' });
          fail += 1;
        }
        renderXiaokeBatchPanel();
      }
      toast(`上传完成：成功 ${ok} · 跳过 ${skip} · 失败 ${fail}`);
      await refreshXiaokeEditionsStatus();
    } finally {
      xkBatchUploading = false;
      syncXiaokeBatchBar();
    }
  }

  function bindXiaokeBatchUi() {
    const input = document.getElementById('xk-batch-input');
    const previewBtn = document.getElementById('xk-batch-preview-btn');
    const uploadBtn = document.getElementById('xk-batch-upload-btn');
    const closeBtn = document.getElementById('xk-batch-close');
    if (!input || input.dataset.bound === '1') return;
    input.dataset.bound = '1';
    input.addEventListener('change', () => {
      xkBatchFiles = Array.from(input.files || []).filter((f) => f && f.name);
      xkBatchPlan = [];
      xkBatchProgress.clear();
      xkBatchEditionId = xiaokeActiveEditionId || null;
      input.value = '';
      if (!xkBatchFiles.length) {
        toast('未选择 PDF 文件');
        renderXiaokeBatchPanel();
        return;
      }
      if (!xkBatchEditionId) {
        toast('请先选择版本再上传');
        xkBatchFiles = [];
        renderXiaokeBatchPanel();
        return;
      }
      toast(`已选中 ${xkBatchFiles.length} 个 PDF，正在自动匹配…`);
      renderXiaokeBatchPanel();
      previewXiaokeBatchMatch();
    });
    previewBtn?.addEventListener('click', () => {
      if (!xkBatchUploading) previewXiaokeBatchMatch();
    });
    uploadBtn?.addEventListener('click', async () => {
      if (xkBatchUploading) return;
      try {
        await uploadXiaokeBatch();
      } catch (e) {
        toast(e.message || String(e));
      }
    });
    closeBtn?.addEventListener('click', () => {
      if (xkBatchUploading) return;
      xkBatchFiles = [];
      xkBatchPlan = [];
      xkBatchEditionId = null;
      xkBatchProgress.clear();
      renderXiaokeBatchPanel();
    });
  }

  async function refreshXiaokePreprocessJob(editionId) {
    const r = await fetch(
      `${API}/workbook/xiaoke/editions/${encodeURIComponent(editionId)}/preprocess-all`,
    );
    const d = await readJson(r);
    return d.job;
  }

  function hideXiaokePreprocessPanel() {
    const panel = document.getElementById('xk-prep-panel');
    if (panel) panel.hidden = true;
  }

  function stopXiaokePreprocessPolling() {
    if (xkPreprocessPollTimer) {
      clearInterval(xkPreprocessPollTimer);
      xkPreprocessPollTimer = null;
    }
  }

  function renderXiaokePreprocessJobPanel(job) {
    const mount = document.getElementById('xk-batch-mount');
    let panel = document.getElementById('xk-prep-panel');
    if (!job || (job.status !== 'running' && job.status !== 'cancelling')) {
      hideXiaokePreprocessPanel();
      return;
    }
    if (!panel) {
      panel = document.createElement('div');
      panel.id = 'xk-prep-panel';
      panel.className = 'ed-parse-panel';
      panel.innerHTML = `
        <div class="ed-parse-panel-head">
          <div class="ed-parse-panel-title">
            <strong>本版新教材预处理</strong>
            <span class="hint" id="xk-prep-summary"></span>
          </div>
          <div class="ed-parse-panel-actions">
            <button type="button" class="btn btn-sm secondary" id="xk-prep-cancel-btn">取消排队</button>
          </div>
        </div>
        <ul class="ed-parse-list" id="xk-prep-list"></ul>
        <p class="ed-parse-note hint">后台串行对齐单册四步：目录 → 划页 → 页图 → 粗分（缺什么补什么）。可离开本页；若重启 Flask，进度面板会清空，但已写入数据库的结果仍保留。</p>
      `;
      (mount || document.body).appendChild(panel);
      document.getElementById('xk-prep-cancel-btn')?.addEventListener('click', async () => {
        if (!xkPreprocessEditionId) return;
        try {
          await fetch(
            `${API}/workbook/xiaoke/editions/${encodeURIComponent(xkPreprocessEditionId)}/preprocess-all/cancel`,
            { method: 'POST' },
          );
          toast('已请求取消（当前正在处理的册会跑完）');
        } catch (e) {
          toast(e.message || String(e));
        }
      });
    }
    placeXiaokePreprocessPanel();
    panel.hidden = false;
    const summary = document.getElementById('xk-prep-summary');
    const list = document.getElementById('xk-prep-list');
    const vols = Object.values(job.volumes || {});
    if (summary) {
      const phase = job.status === 'cancelling' ? '取消中' : '进行中';
      const total = job.volume_count || vols.length || 0;
      const cur = job.current_index || 0;
      summary.textContent = `${phase} 第 ${cur}/${total} 册`
        + (job.current_volume ? ` · ${job.current_volume}` : '');
    }
    if (list) {
      list.innerHTML = vols.map((v) => {
        const termLabel = v.term === '下' || v.term === 'xia' ? '下' : '上';
        return `<li class="ed-parse-item ed-parse-item--${esc(v.state || 'queued')}">
          <strong>${v.grade}年级${termLabel}册</strong>
          <span>${esc(v.label || v.state || '')}</span>
        </li>`;
      }).join('');
    }
  }

  function startXiaokePreprocessPolling(editionId, { autoAfterDone = false } = {}) {
    xkPreprocessEditionId = editionId;
    stopXiaokePreprocessPolling();
    const tick = async () => {
      try {
        const job = await refreshXiaokePreprocessJob(editionId);
        renderXiaokePreprocessJobPanel(job);
        if (!job || (job.status !== 'running' && job.status !== 'cancelling')) {
          stopXiaokePreprocessPolling();
          if (job?.status === 'done' || job?.status === 'cancelled') {
            toast(job.message || '本版预处理结束');
            refreshXiaokeEditionsStatus().catch(() => {});
          }
        }
      } catch (_) { /* ignore */ }
    };
    tick();
    xkPreprocessPollTimer = setInterval(tick, 3000);
  }

  async function preprocessXiaokeEditionAll(editionId, { skipConfirm = false } = {}) {
    try {
      const existing = await refreshXiaokePreprocessJob(editionId);
      if (existing?.status === 'running' || existing?.status === 'cancelling') {
        toast(existing.status === 'cancelling'
          ? '本版预处理正在取消，请稍候…'
          : '本版预处理已在后台进行中');
        startXiaokePreprocessPolling(editionId, { autoAfterDone: skipConfirm });
        return;
      }
    } catch (_) { /* ignore */ }

    if (!skipConfirm && !confirm(
      '将对已上传新侧 PDF、尚未完成预处理的册在后台依次执行：\n'
      + '按缺口补跑四步：目录 → 划页 → 页图 → 粗分。\n'
      + '可离开本页；重启服务后进度面板会丢，库内结果仍在。继续？',
    )) return;

    const r = await fetch(
      `${API}/workbook/xiaoke/editions/${encodeURIComponent(editionId)}/preprocess-all`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ only_pending: true }),
      },
    );
    const d = await readJson(r);
    const job = d.job;
    if (!job || job.status === 'idle') {
      toast(job?.message || '没有待预处理的册');
      return;
    }
    toast(d.already_running
      ? '本版预处理已在后台进行中'
      : `本版预处理已启动（${job.volume_count || '?'} 册）`);
    renderXiaokePreprocessJobPanel(job);
    startXiaokePreprocessPolling(editionId, { autoAfterDone: skipConfirm });
  }

  function syncXiaokeCoarseExportDlBtn() {
    const btn = document.getElementById('xk-coarse-export-dl');
    if (!btn) return;
    const matched = document.getElementById('xk-coarse-include-matched')?.checked;
    const unmatched = document.getElementById('xk-coarse-include-unmatched')?.checked;
    const checked = document.querySelectorAll('#xk-coarse-export-rows input[type="checkbox"]:checked');
    btn.disabled = !(matched || unmatched) || checked.length === 0;
  }

  async function openXiaokeCoarseExportPanel(editionId) {
    const panel = document.getElementById('xk-coarse-export-panel');
    const tbody = document.getElementById('xk-coarse-export-rows');
    const summary = document.getElementById('xk-coarse-export-summary');
    if (!panel || !tbody) return;
    placeXiaokeCoarseExportPanel();
    panel.hidden = false;
    tbody.innerHTML = '<tr><td colspan="5" class="hint">加载册次状态…</td></tr>';
    syncXiaokeCoarseExportDlBtn();
    try {
      const r = await fetch(
        `${API}/workbook/xiaoke/editions/${encodeURIComponent(editionId)}/export-coarse-targets`,
      );
      const d = await readJson(r);
      const vols = d.volumes || [];
      const ready = vols.filter((v) => v.has_coarse);
      if (summary) {
        summary.textContent = ready.length
          ? `${ready.length}/${vols.length} 册已有粗分 · 文件名 ${d.edition_label || ''}.xlsx`
          : '本版尚无粗分结果';
      }
      tbody.innerHTML = vols.map((v) => {
        const disabled = v.has_coarse ? '' : ' disabled';
        const checked = v.has_coarse ? ' checked' : '';
        const st = v.has_coarse ? `已粗分 ${v.pair_count || 0} 课` : '尚无粗分';
        return `<tr class="${v.has_coarse ? '' : 'is-muted'}">
          <td><input type="checkbox" data-old="${esc(v.old_code)}" data-new="${esc(v.new_code)}"${checked}${disabled}></td>
          <td>${esc(v.label || '')}</td>
          <td>${v.matched_count ?? 0}</td>
          <td>${v.unmatched_count ?? 0}</td>
          <td>${esc(st)}</td>
        </tr>`;
      }).join('') || '<tr><td colspan="5" class="hint">本版无册次</td></tr>';
      tbody.querySelectorAll('input[type="checkbox"]').forEach((el) => {
        el.addEventListener('change', syncXiaokeCoarseExportDlBtn);
      });
      syncXiaokeCoarseExportDlBtn();
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5">${esc(e.message || String(e))}</td></tr>`;
      toast(e.message || String(e));
      syncXiaokeCoarseExportDlBtn();
    }
  }

  async function downloadXiaokeEditionCoarseXlsx() {
    const editionId = xiaokeActiveEditionId;
    if (!editionId) throw new Error('请先选择版本');
    const matched = document.getElementById('xk-coarse-include-matched')?.checked;
    const unmatched = document.getElementById('xk-coarse-include-unmatched')?.checked;
    if (!matched && !unmatched) throw new Error('请至少勾选「已匹配」或「未匹配」');
    const pairs = Array.from(
      document.querySelectorAll('#xk-coarse-export-rows input[type="checkbox"]:checked'),
    ).map((el) => ({ old_code: el.dataset.old, new_code: el.dataset.new }))
      .filter((p) => p.old_code && p.new_code);
    if (!pairs.length) throw new Error('请至少勾选一册');

    const btn = document.getElementById('xk-coarse-export-dl');
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(
        `${API}/workbook/xiaoke/editions/${encodeURIComponent(editionId)}/export-coarse-xlsx`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            pairs,
            include_matched: Boolean(matched),
            include_unmatched: Boolean(unmatched),
          }),
        },
      );
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try {
          const d = await r.json();
          msg = d.error || msg;
        } catch (_) { /* ignore */ }
        throw new Error(msg);
      }
      const blob = await r.blob();
      const cd = r.headers.get('Content-Disposition') || '';
      const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(cd);
      const name = decodeURIComponent((m && (m[1] || m[2])) || '') || '本版粗分.xlsx';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      toast(`已开始下载（${pairs.length} 册）`);
    } finally {
      syncXiaokeCoarseExportDlBtn();
    }
  }

  let xiaokeCoarseExportUiBound = false;

  function bindXiaokeCoarseExportUi() {
    if (xiaokeCoarseExportUiBound) return;
    xiaokeCoarseExportUiBound = true;
    document.getElementById('xk-coarse-export-close')?.addEventListener('click', () => {
      const panel = document.getElementById('xk-coarse-export-panel');
      if (panel) panel.hidden = true;
    });
    document.getElementById('xk-coarse-export-dl')?.addEventListener('click', async () => {
      try {
        await downloadXiaokeEditionCoarseXlsx();
      } catch (e) {
        toast(e.message || String(e));
      }
    });
    document.getElementById('xk-coarse-include-matched')?.addEventListener('change', syncXiaokeCoarseExportDlBtn);
    document.getElementById('xk-coarse-include-unmatched')?.addEventListener('change', syncXiaokeCoarseExportDlBtn);
    document.getElementById('xk-coarse-select-all')?.addEventListener('click', () => {
      document.querySelectorAll('#xk-coarse-export-rows input[type="checkbox"]:not(:disabled)').forEach((el) => {
        el.checked = true;
      });
      syncXiaokeCoarseExportDlBtn();
    });
    document.getElementById('xk-coarse-select-none')?.addEventListener('click', () => {
      document.querySelectorAll('#xk-coarse-export-rows input[type="checkbox"]').forEach((el) => {
        el.checked = false;
      });
      syncXiaokeCoarseExportDlBtn();
    });
  }

  function renderXiaokeGate() {
    const root = document.getElementById('editions') || document.getElementById('xiaoke-editions');
    if (!root) return;
    // 重绘前把面板挪出，避免被 innerHTML 销毁（与旧库建设一致）
    parkXiaokeBatchPanel();
    parkXiaokeCoarseExportPanel();
    parkXiaokePreprocessPanel();

    const SCHOOL_SYSTEMS = [
      { id: '63', label: '小学 · 六三学制' },
      { id: '54', label: '小学 · 五·四学制' },
    ];
    const pool = xiaokeEditions.filter((e) => e.school_system === xiaokeActiveSystem);
    if (!pool.length) {
      root.innerHTML = '<p class="wb-empty-msg">该学制暂无已开放版本</p>';
      return;
    }
    if (!xiaokeActiveEditionId || !pool.find((e) => e.edition_id === xiaokeActiveEditionId)) {
      xiaokeActiveEditionId = pool[0].edition_id;
    }
    const active = pool.find((e) => e.edition_id === xiaokeActiveEditionId) || pool[0];
    const systemTabs = SCHOOL_SYSTEMS.map((s) =>
      `<button type="button" class="tab system-tab${s.id === xiaokeActiveSystem ? ' active' : ''}" data-system="${s.id}">${s.label}</button>`
    ).join('');
    const editionTabs = pool.map((e) =>
      `<button type="button" class="tab edition-tab${e.edition_id === active.edition_id ? ' active' : ''}" data-id="${esc(e.edition_id)}">${esc(xiaokeEditionTabLabel(e))}</button>`
    ).join('');
    const missing = countXiaokeMissingCatalog(active);
    const missingPdf = (active.volumes || []).filter((v) => !v.has_new_pdf).length;
    const ghostPdf = (active.volumes || []).filter((v) => v.pdf_blob_missing).length;
    const toolbarHint = missing
      ? `本页只读拉取旧库目录：还有 ${missing} 册在旧库中尚无目录。可先进入有目录的册次；无目录需到旧库建设载入。`
      : (ghostPdf
        ? `有 ${ghostPdf} 册 PDF 登记丢失（文件不在磁盘），请重新一键上传后再预处理。`
        : (missingPdf
          ? `本版旧库目录齐全。还可一键上传新教材 PDF（缺 ${missingPdf} 册），再一键预处理。`
          : '本版旧库目录齐全，新侧 PDF 已齐。可一键预处理，或勾选下载粗分结果。'));
    const cards = (active.volumes || []).map((v) => {
      const libReady = xiaokeHasLibraryCatalog(v);
      const libN = Number(v.library_lesson_count || 0);
      const complete = !!(libReady && v.new_ready);
      const cardCls = !libReady ? 'vol-card--setup' : complete ? 'vol-card--complete' : '';
      const sub = libReady ? `已有目录 · ${libN} 课` : '旧库暂无目录';
      const status = xiaokeParseStatus(v);
      const enterLabel = libReady ? '进入本册建设 →' : '进入查看 →';
      return `<article class="vol-card ${cardCls}" data-xk-enter data-ed="${esc(active.edition_id)}" data-g="${v.grade}" data-t="${esc(v.term)}" data-old="${esc(v.old_code)}" data-new="${esc(v.new_code)}" data-in-db="${v.in_db ? '1' : ''}" role="button" tabindex="0" title="${esc(enterLabel)}">
        <div class="vol-card-head">
          <span class="vol-grade-badge">${v.grade}</span>
          <div>
            <h3 class="vol-card-title">${xiaokeVolTitle(v)}</h3>
            <p class="vol-card-sub">${sub}</p>
          </div>
        </div>
        ${xiaokeProgressBar(v)}
        <p class="vol-status">${xiaokeFormatVolStatus(status)}</p>
        <span class="vol-build-cta">${enterLabel}</span>
      </article>`;
    }).join('');
    root.innerHTML = `
      <p class="wb-filter-label">学制</p>
      <div class="school-system-tabs">${systemTabs}</div>
      <p class="wb-filter-label">教材版本</p>
      <div class="edition-tabs">${editionTabs}</div>
      <div class="edition-toolbar">
        <p class="edition-toolbar-hint">${toolbarHint}</p>
        <div class="edition-toolbar-actions">
          <label class="btn vol-setup-cta tb-batch-trigger" for="xk-batch-input" title="一次选中本版多册新教材 PDF，选完后在下方面板匹配并上传">
            一键上传本版所有新教材
          </label>
          <button type="button" class="btn vol-setup-cta" data-action="xk-preprocess-all" data-ed="${esc(active.edition_id)}" title="对本版已上传 PDF、尚未完成预处理的册依次：识别目录 → 粗分 → 划分页码">
            一键开启所有新教材预处理
          </button>
          <button type="button" class="btn vol-setup-cta" data-action="xk-export-coarse" data-ed="${esc(active.edition_id)}" title="勾选册次，按版本名下载 Excel（每册一个工作表）">
            下载本版粗分结果
          </button>
        </div>
      </div>
      <div id="xk-batch-mount"></div>
      <div id="xk-coarse-export-mount"></div>
      <div class="vol-grid" id="vol-grid">${cards || '<p class="wb-empty-msg">该版本暂无册次</p>'}</div>
    `;
    placeXiaokeBatchPanel();
    placeXiaokeCoarseExportPanel();
    placeXiaokePreprocessPanel();
    if (xkBatchFiles.length) renderXiaokeBatchPanel();
    root.querySelectorAll('.system-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        xiaokeActiveSystem = btn.dataset.system;
        renderXiaokeGate();
      });
    });
    root.querySelectorAll('.edition-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        xiaokeActiveEditionId = btn.dataset.id;
        renderXiaokeGate();
      });
    });
    root.querySelector('[data-action="xk-export-coarse"]')?.addEventListener('click', () => {
      openXiaokeCoarseExportPanel(active.edition_id);
    });
    root.querySelector('[data-action="xk-preprocess-all"]')?.addEventListener('click', async (btnEv) => {
      const btn = btnEv.currentTarget;
      try {
        btn.disabled = true;
        await preprocessXiaokeEditionAll(active.edition_id);
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        btn.disabled = false;
      }
    });
    const enterXiaokeVolume = async (el) => {
      if (!el || el.classList.contains('is-busy')) return;
      const title = el.querySelector('.vol-card-title')?.textContent || '本册';
      try {
        el.classList.add('is-busy');
        toast(`正在打开${title}…`);
        const r = await fetch(`${API}/workbook/xiaoke/ensure`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            edition_id: el.dataset.ed,
            grade: Number(el.dataset.g),
            term: el.dataset.t,
          }),
        });
        const d = await readJson(r);
        const oldCode = d.old_code || el.dataset.old;
        const newCode = d.new_code || el.dataset.new;
        if (!oldCode || !newCode) throw new Error('未生成册次编码');
        window.location.href = workbookPairUrl(oldCode, newCode, el.dataset.ed);
      } catch (err) {
        toast(err.message || String(err));
        el.classList.remove('is-busy');
      }
    };
    root.querySelectorAll('[data-xk-enter]').forEach((card) => {
      card.addEventListener('click', (e) => {
        e.preventDefault();
        enterXiaokeVolume(card);
      });
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          enterXiaokeVolume(card);
        }
      });
    });
    syncXiaokeEditionUrl();
    if (active.edition_id) {
      refreshXiaokePreprocessJob(active.edition_id).then((job) => {
        if (job?.status === 'running' || job?.status === 'cancelling') {
          startXiaokePreprocessPolling(active.edition_id);
        }
      }).catch(() => {});
    }
  }

  async function loadXiaokeGate() {
    applyXiaokeWorkbenchShell();
    bindXiaokeCoarseExportUi();
    bindXiaokeBatchUi();
    const root = document.getElementById('editions') || document.getElementById('xiaoke-editions');
    if (!root) return;
    const seed = seedXiaokeEditionsFromSubject();
    xiaokeEditions = seed;
    const fromUrl = new URLSearchParams(window.location.search).get('edition');
    if (fromUrl) {
      const ed = xiaokeEditions.find((e) => e.edition_id === fromUrl || e.label === fromUrl);
      if (ed) {
        xiaokeActiveSystem = ed.school_system || '63';
        xiaokeActiveEditionId = ed.edition_id;
      }
    }
    renderXiaokeGate();
    try {
      await refreshXiaokeEditionsStatus();
    } catch (e) {
      toast(`册次状态暂不可用：${e.message || String(e)}`);
    }
  }

  // ===== 数学学科：版本/册次卡片网格（仿小科，精简版） =====
  let mathEditions = [];
  let mathActiveEditionId = null;

  function seedMathEditionsFromSubject() {
    return (subject.editions || []).map((e) => {
      const prefix = String(e.code_prefix || '').trim().toUpperCase();
      return {
        edition_id: e.id,
        label: e.label,
        diff_prefix: prefix,
        volumes: (e.grades || []).map((g) => ({
          grade: g.grade,
          term: g.term,
          old_code: prefix
            ? `${prefix}-${g.grade}${xiaokeTermToken(g.term)}-DOLD`
            : '',
          new_code: prefix
            ? `${prefix}-${g.grade}${xiaokeTermToken(g.term)}-DNEW`
            : '',
          in_db: false,
          old_lesson_count: 0,
          new_lesson_count: 0,
          has_new_pdf: false,
          has_old_pdf: false,
          old_ready: false,
          new_ready: false,
        })),
      };
    });
  }

  function mathVolTitle(v) {
    return `${v.grade}年级${v.term === '上' ? '上册' : '下册'}`;
  }

  function mathParseStatus(v) {
    const oldPdf = !!v.has_old_pdf;
    const newPdf = !!v.has_new_pdf;
    const newN = Number(v.new_lesson_count || 0);
    if (!oldPdf && !newPdf) return '待上传 PDF';
    if (v.new_ready) return `新侧 ${newN} 课 · 可粗分`;
    if (newN) return `新侧 ${newN} 课`;
    if (newPdf && !oldPdf) return '新侧已上传 · 待识别目录';
    if (oldPdf && !newPdf) return '旧侧已上传 · 待传新侧';
    return '已上传 PDF';
  }

  function mathProgressBar(v) {
    const steps = [
      { label: '旧PDF', done: !!v.has_old_pdf },
      { label: '新PDF', done: !!v.has_new_pdf },
      { label: '可粗分', done: !!v.new_ready },
    ];
    if (!steps.some((s) => s.done)) return '';
    const segs = steps
      .map((s) => `<span class="vol-progress-seg${s.done ? ' done' : ''}"></span>`)
      .join('');
    const labels = steps
      .map((s) => `<span class="${s.done ? 'is-done' : ''}">${s.label}</span>`)
      .join('');
    return `<div class="vol-progress-bar" aria-hidden="true">${segs}</div><div class="vol-step-labels">${labels}</div>`;
  }

  function applyMathWorkbenchShell() {
    document.body.classList.remove('diff-page', 'workbook-page');
    document.body.classList.add('workbench-page', 'wb-math');
    document.title = '数学建设 · 新旧教材对比';
    const hero = document.querySelector('header.diff-hero, header.wb-hero');
    if (hero) {
      hero.className = 'wb-hero';
      const backQ = window.diffSubjectQuery
        ? window.diffSubjectQuery(subjectId)
        : `subject=${encodeURIComponent(subjectId)}`;
      hero.innerHTML = `
        <div class="wb-hero-inner">
          <a class="wb-back" href="/textbook-diff/?${backQ}">← 选择学科</a>
          <h1>数学建设</h1>
          <p class="wb-lead">初中数学对比 · 点册次进入本册工作页（上传 PDF + 预处理 + 粗分）</p>
        </div>`;
    }
    const main = document.querySelector('main.diff-main, main.wb-main');
    if (main) main.className = 'wb-main';
    const listPanel = document.getElementById('list-panel');
    if (listPanel) {
      listPanel.className = 'wb-panel';
      listPanel.hidden = false;
    }
    const hideEl = (id) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.hidden = true;
      el.style.display = 'none';
    };
    hideEl('list-panel-title');
    hideEl('list-panel-lead');
    hideEl('create-form');
    hideEl('pair-list');
    hideEl('pair-panel');
    hideEl('coarse-panel');
    const gate =
      document.getElementById('xiaoke-editions') ||
      document.getElementById('editions');
    if (gate) {
      gate.hidden = false;
      gate.style.display = '';
      gate.className = '';
      gate.id = 'editions';
    }
  }

  function renderMathGate() {
    const root =
      document.getElementById('editions') ||
      document.getElementById('xiaoke-editions');
    if (!root) return;
    const pool = mathEditions;
    if (!pool.length) {
      root.innerHTML = '<p class="wb-empty-msg">暂无已开放版本</p>';
      return;
    }
    if (
      !mathActiveEditionId ||
      !pool.find((e) => e.edition_id === mathActiveEditionId)
    ) {
      mathActiveEditionId = pool[0].edition_id;
    }
    const active =
      pool.find((e) => e.edition_id === mathActiveEditionId) || pool[0];
    const cards = (active.volumes || [])
      .map((v) => {
        const complete = !!(v.has_old_pdf && v.new_ready);
        const cardCls = complete
          ? 'vol-card--complete'
          : !v.has_old_pdf && !v.has_new_pdf
            ? 'vol-card--setup'
            : '';
        const sub = v.in_db
          ? `旧 ${v.old_lesson_count || 0} 课 · 新 ${v.new_lesson_count || 0} 课`
          : '尚未创建本册';
        const status = mathParseStatus(v);
        return `<article class="vol-card ${cardCls}" data-math-enter data-ed="${esc(active.edition_id)}" data-g="${v.grade}" data-t="${esc(v.term)}" data-old="${esc(v.old_code)}" data-new="${esc(v.new_code)}" role="button" tabindex="0" title="进入本册建设">
        <div class="vol-card-head">
          <span class="vol-grade-badge">${v.grade}</span>
          <div>
            <h3 class="vol-card-title">${mathVolTitle(v)}</h3>
            <p class="vol-card-sub">${esc(sub)}</p>
          </div>
        </div>
        ${mathProgressBar(v)}
        <p class="vol-status">${esc(status)}</p>
        <span class="vol-build-cta">进入本册建设 →</span>
      </article>`;
      })
      .join('');
    root.innerHTML = `
      <div class="edition-toolbar">
        <p class="edition-toolbar-hint">冀教版 7-9 年级上下册。点册次卡片进入本册工作页（旧/新 PDF 上传 + 预处理 + 粗分）。</p>
      </div>
      <div class="vol-grid" id="vol-grid">${cards || '<p class="wb-empty-msg">该版本暂无册次</p>'}</div>`;
    const enterMathVolume = async (el) => {
      if (!el || el.classList.contains('is-busy')) return;
      const title =
        el.querySelector('.vol-card-title')?.textContent || '本册';
      try {
        el.classList.add('is-busy');
        toast(`正在打开${title}…`);
        const r = await fetch(`${API}/workbook/ensure`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            subject: subject.label,
            edition_id: el.dataset.ed,
            grade: Number(el.dataset.g),
            term: el.dataset.t,
          }),
        });
        const d = await readJson(r);
        const oldCode = d.old_code || el.dataset.old;
        const newCode = d.new_code || el.dataset.new;
        if (!oldCode || !newCode) throw new Error('未生成册次编码');
        window.location.href = workbookPairUrl(oldCode, newCode, el.dataset.ed);
      } catch (err) {
        toast(err.message || String(err));
        el.classList.remove('is-busy');
      }
    };
    root.querySelectorAll('[data-math-enter]').forEach((card) => {
      card.addEventListener('click', (e) => {
        e.preventDefault();
        enterMathVolume(card);
      });
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          enterMathVolume(card);
        }
      });
    });
  }

  async function refreshMathEditionsStatus() {
    const seed = seedMathEditionsFromSubject();
    try {
      const r = await fetch(
        `${API}/workbook/editions?subject=${encodeURIComponent(subject.label)}`
      );
      const d = await readJson(r);
      const apiById = new Map(
        (d.editions || []).map((e) => [e.edition_id, e])
      );
      mathEditions = seed.map((base) => {
        const api = apiById.get(base.edition_id);
        if (!api) return base;
        const volMap = new Map(
          (api.volumes || []).map((v) => [
            `${Number(v.grade)}-${v.term === '下' ? '下' : '上'}`,
            v,
          ])
        );
        return {
          ...base,
          ...api,
          volumes: (base.volumes || []).map((v) => {
            const hit = volMap.get(
              `${Number(v.grade)}-${v.term === '下' ? '下' : '上'}`
            );
            return hit
              ? {
                  ...v,
                  ...hit,
                  grade: Number(hit.grade),
                  term: hit.term === '下' ? '下' : '上',
                }
              : v;
          }),
        };
      });
    } catch (e) {
      mathEditions = seed;
      throw e;
    }
    renderMathGate();
  }

  async function loadMathGate() {
    applyMathWorkbenchShell();
    const root =
      document.getElementById('editions') ||
      document.getElementById('xiaoke-editions');
    if (!root) return;
    mathEditions = seedMathEditionsFromSubject();
    renderMathGate();
    try {
      await refreshMathEditionsStatus();
    } catch (e) {
      toast(`册次状态暂不可用：${e.message || String(e)}`);
    }
  }

  async function loadPairList() {
    if (isXiaokeSubject()) {
      await loadXiaokeGate();
      return;
    }
    if (subjectId === 'shuxue') {
      await loadMathGate();
      return;
    }
    const list = document.getElementById('pair-list');
    if (!list) return;
    try {
      const q = window.diffSubjectQuery
        ? `?${window.diffSubjectQuery(subjectId)}`
        : `?subject=${encodeURIComponent(subject.label)}`;
      const r = await fetch(`${API}/workbook/pairs${q}`);
      const d = await readJson(r);
      const pairs = d.pairs || [];
      if (!pairs.length) {
        list.innerHTML = '<p class="hint">尚无本册。请在上方创建要比对的年级学期。</p>';
        return;
      }
      list.innerHTML = pairs.map((p) => {
        const href = workbookPairUrl(p.old_code, p.new_code);
        return `
        <div class="workbook-pair-row" data-old-code="${esc(p.old_code)}" data-new-code="${esc(p.new_code)}">
          <div>
            <strong>${esc(p.display_title || '')}</strong>
            <div class="hint">${esc(p.old_code)} ↔ ${esc(p.new_code)}
              · 旧课 ${p.old?.lesson_count || 0} · 新课 ${p.new?.lesson_count || 0}</div>
          </div>
          <a class="workbook-open-link" href="${esc(href)}">打开</a>
        </div>`;
      }).join('');
    } catch (e) {
      list.innerHTML = `<p class="hint">加载失败：${esc(e.message || String(e))}</p>`;
    }
  }

  document.getElementById('create-form')?.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const body = {
        subject: subject.label,
        edition: editionSel.value,
        grade: Number(document.getElementById('f-grade').value),
        term: document.getElementById('f-term').value,
      };
      const r = await fetch(`${API}/workbook/pairs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await readJson(r);
      toast(d.old?.created || d.new?.created ? '已创建本册，正在打开…' : '本册已存在，正在打开…');
      window.location.href = workbookPairUrl(d.old_code, d.new_code);
    } catch (e) {
      toast(e.message || String(e));
    }
  });

  document.getElementById('btn-coarse').addEventListener('click', async () => {
    if (!current.old_code || !current.new_code) {
      toast('请先打开一本');
      return;
    }
    const blocked = coarseReadyHint(current.detail);
    if (blocked) {
      toast(blocked);
      return;
    }
    const draft = isDraftCoarse(current.detail);
    const wrap = document.getElementById('coarse-table');
    updateCoarseChrome(current.detail, { busy: true });
    if (wrap) {
      wrap.innerHTML = draft
        ? '<p class="hint">正在页级粗分（扫描修订页脚并对齐旧书页），请稍候…</p>'
        : '<p class="hint">正在课对课粗分，请稍候…</p>';
    }
    toast(draft ? '页级粗分进行中…' : '粗分进行中…');
    try {
      const d = await runCoarseMatchRequest();
      current.detail.coarse = d.coarse;
      renderCoarse(current.detail);
      if (draft) {
        const changed = (d.coarse && d.coarse.chapter_overview
          && d.coarse.chapter_overview.changed_chapter_count) || 0;
        toast(`页级粗分完成：${d.created || 0} 页 · ${changed} 个章节有变动`);
      } else {
        toast(`粗分完成：${d.created || 0} 对`);
      }
    } catch (e) {
      renderCoarse(current.detail);
      toast(e.message || String(e));
    }
  });

  document.getElementById('btn-volume-pipeline')?.addEventListener('click', () => {
    if (isXiaokeSubject()) {
      startXiaokeVolumeFull();
      return;
    }
    if (isTestSubject() && !isDraftCoarse(current.detail)) {
      startTestVolumeCombo();
      return;
    }
    startPipeline({ scope: 'volume', label: '整册' });
  });

  document.getElementById('btn-volume-cancel')?.addEventListener('click', async () => {
    if (!current.old_code || !current.new_code) return;
    const btn = document.getElementById('btn-volume-cancel');
    try {
      if (btn) btn.disabled = true;
      const r = await fetch(`${API}/workbook/xiaoke/volume-full`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          old_code: current.old_code,
          new_code: current.new_code,
          cancel: true,
        }),
      });
      const d = await readJson(r);
      toast(d.message || '已停止整册一键');
      setPipelineStatus(d.message || '已停止整册一键', { busy: false });
      if (btn) btn.hidden = true;
      document.querySelectorAll(
        '.btn-lesson-pipeline, #btn-volume-pipeline, #btn-volume-retry-failed'
      ).forEach((b) => { b.disabled = false; });
      startXiaokeVolumeFullPoll();
    } catch (e) {
      toast(e.message || String(e));
      if (btn) btn.disabled = false;
    }
  });

  document.getElementById('btn-volume-retry-failed')?.addEventListener('click', () => {
    startFailedVolumeRetry().catch((e) => toast(e.message || String(e)));
  });

  document.getElementById('btn-export-coarse-xlsx')?.addEventListener('click', () => {
    if (!current.old_code || !current.new_code) {
      toast('请先打开一本');
      return;
    }
    if (isDraftCoarse(current.detail)) {
      toast('修订版页级模式暂不支持粗分导出');
      return;
    }
    const items = (current.detail && current.detail.coarse && current.detail.coarse.items) || [];
    if (!items.length) {
      toast('尚无粗分结果，请先运行粗分');
      return;
    }
    const q = new URLSearchParams({
      old_code: current.old_code,
      new_code: current.new_code,
    });
    const a = document.createElement('a');
    a.href = `${API}/workbook/export-coarse-xlsx?${q.toString()}`;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast('粗分结果 Excel 已开始下载');
  });

  document.getElementById('btn-export-xlsx')?.addEventListener('click', () => {
    if (!current.old_code || !current.new_code) {
      toast('请先打开一本');
      return;
    }
    if (isDraftCoarse(current.detail)) {
      toast('修订版页级模式暂不支持整册导出');
      return;
    }
    // 用同源 <a> 直链下载，保留用户点击手势；async fetch+blob 易被浏览器拦截
    const q = new URLSearchParams({
      old_code: current.old_code,
      new_code: current.new_code,
    });
    const a = document.createElement('a');
    a.href = `${API}/workbook/export-xlsx?${q.toString()}`;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast('对比结果 Excel 已开始下载');
  });

  document.getElementById('btn-one-click-recognize')?.addEventListener('click', () => {
    startOneClickRecognize().catch((e) => toast(e.message || String(e)));
  });

  async function callTestReset(path, { confirmDb } = {}) {
    if (!isTestSubject()) return;
    if (!current.old_code || !current.new_code) {
      toast(`请先打开一本 ${subject.label} 册`);
      return;
    }
    const body = {
      old_code: current.old_code,
      new_code: current.new_code,
    };
    if (confirmDb) body.confirm = true;
    const r = await fetch(`${API}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return readJson(r);
  }

  document.getElementById('btn-test-flush-db')?.addEventListener('click', async () => {
    if (!current.old_code || !current.new_code) {
      toast(`请先打开一本 ${subject.label} 册`);
      return;
    }
    try {
      const sumR = await fetch(
        `${API}/test/local-cache-summary?old_code=${encodeURIComponent(current.old_code)}`
        + `&new_code=${encodeURIComponent(current.new_code)}`
      );
      const sum = await readJson(sumR);
      if (!confirm(
        `将本地 JSON 对比结果写入 MySQL？\n`
        + `缓存文件 ${sum.cache_files || 0} · 有原子 ${sum.pages_with_atoms || 0}`
        + ` · 文字比对 ${sum.pages_with_text_compare || 0}`
        + ` · 图片比对 ${sum.pages_with_image_compare || 0}`
      )) return;
      const d = await callTestReset('/test/flush-local-to-db', { confirmDb: true });
      toast(
        `已提交：原子页 ${d.atoms_pages || 0} · 文字 ${d.text_pages || 0}`
        + ` · 图片 ${d.image_pages || 0}`
        + (d.error_count ? ` · 失败 ${d.error_count}` : '')
      );
    } catch (e) {
      toast(e.message || String(e));
    }
  });

  document.getElementById('btn-test-clear-cache')?.addEventListener('click', async () => {
    if (!confirm(`清空本对 ${subject.label} 的 OCR/对比/目录缓存？\n课时与粗分课对会保留。`)) return;
    try {
      const d = await callTestReset('/test/clear-cache');
      const rm = (d && d.removed) || {};
      toast(
        `测试缓存已清：page_text ${rm.page_text || 0} · atoms ${rm.diff_page_atoms || 0}`
        + ` · 快照 ${rm.db_atom_snapshots || 0}`
      );
    } catch (e) {
      toast(e.message || String(e));
    }
  });

  document.getElementById('btn-test-clear-db')?.addEventListener('click', async () => {
    if (!confirm(
      `清空本对 ${subject.label} 数据库内容？\n`
      + '将删除课时、粗分课对，并解绑 PDF；保留册次码便于重新上传测试。\n'
      + '不会改动其它学科册次。'
    )) return;
    try {
      const d = await callTestReset('/test/clear-db', { confirmDb: true });
      toast(
        `测试库已清：课时 ${d.lessons || 0} · 课对 ${d.diff_lesson_pairs || 0}`
        + ' · 可重新上传 PDF'
      );
      if (current.old_code && current.new_code) {
        await openPair(current.old_code, current.new_code);
      }
    } catch (e) {
      toast(e.message || String(e));
    }
  });

  function esc(s) {
    const el = document.createElement('span');
    el.textContent = s == null ? '' : String(s);
    return el.innerHTML;
  }

  wireChemShelf();

  if (isPairPage) {
    if (isSandboxSubject() && window.WorkbookTestUpload) {
      window.WorkbookTestUpload.bindOnce();
    }
    if (isXiaokeSubject() && window.WorkbookXiaokeIntake) {
      window.WorkbookXiaokeIntake.init({
        getPair: () => current,
        toast,
        refreshPair: refreshPairAfterRecognize,
        openUpload: () => {
          if (!current.new_code) {
            toast('本册未就绪');
            return;
          }
          openTestUploadModal('new', current.new_code);
        },
      });
    }
    openPair(deepOld, deepNew)
      .then(() => refreshPipelineStatus())
      .then((snap) => {
        if (snap && (snap.running_count || 0) > 0 && !isXiaokeSubject()) startPipelinePoll();
      })
      .then(async () => {
        if (!isXiaokeSubject() || !current.old_code || !current.new_code) return;
        try {
          const r = await fetch(
            `${API}/workbook/xiaoke/volume-full/status?old_code=${encodeURIComponent(current.old_code)}`
            + `&new_code=${encodeURIComponent(current.new_code)}`
          );
          const d = await readJson(r);
          if (d.job && String(d.job.status || '') === 'running') startXiaokeVolumeFullPoll();
        } catch (_) { /* ignore */ }
      })
      .catch((e) => toast(e.message || String(e)));
  } else {
    loadPairList().catch((e) => toast(e.message || String(e)));
  }
})();
