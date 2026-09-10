function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 8000);
}

function termToken(term) {
  return term === '下' ? 'xia' : 'shang';
}

const SCHOOL_SYSTEMS = [
  { id: '63', label: '小学 · 六三学制' },
  { id: '54', label: '小学 · 五·四学制' },
];

let allEditions = [];
let activeSystem = '63';
let activeEditionId = null;

function applyEditionFromUrl() {
  const raw = new URLSearchParams(window.location.search).get('edition');
  if (!raw) return;
  const ed = allEditions.find((e) => e.edition_id === raw || e.label === raw);
  if (!ed) return;
  activeSystem = ed.school_system;
  activeEditionId = ed.edition_id;
}

function syncEditionUrl() {
  const u = new URL(window.location.href);
  if (activeEditionId) u.searchParams.set('edition', activeEditionId);
  else u.searchParams.delete('edition');
  const next = u.pathname + u.search;
  const cur = window.location.pathname + window.location.search;
  if (next !== cur) history.replaceState(null, '', next);
}

async function readJson(r) {
  const text = await r.text();
  let d;
  try {
    d = text ? JSON.parse(text) : {};
  } catch (e) {
    throw new Error(r.ok ? '服务器返回了非 JSON 响应' : `请求失败 HTTP ${r.status}`);
  }
  if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

async function bootstrap(editionId, grade, term, replace) {
  const t = termToken(term);
  const r = await fetch(`/api/old-library/volumes/${editionId}/${grade}/${t}/bootstrap`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ replace }),
  });
  return readJson(r);
}

async function bootstrapAll(editionId, { replace = false } = {}) {
  if (window.OldLibraryBootstrapAll?.bootstrapEditionAll) {
    return window.OldLibraryBootstrapAll.bootstrapEditionAll(editionId, { replace });
  }
  const r = await fetch(`/api/old-library/editions/${encodeURIComponent(editionId)}/bootstrap-all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ replace, only_missing: !replace }),
  });
  return readJson(r);
}

async function syncBenchmark(editionId, grade, term) {
  const t = termToken(term);
  const r = await fetch(`/api/old-library/volumes/${editionId}/${grade}/${t}/sync-benchmark`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  return readJson(r);
}

function editionsForSystem(systemId) {
  return allEditions.filter((e) => e.school_system === systemId);
}

function parseStatusLabel(status, lessonCount, buildProgress) {
  if (!lessonCount) return '请先载入基准目录';
  if (!buildProgress) {
    const pdf = {
      pending: 'PDF 待上传',
      processing: '正在解析…',
      done: '课次已拆分',
      failed: '解析失败',
    };
    return `${lessonCount} 课 · ${pdf[status] || 'PDF 待上传'}`;
  }
  const parts = [];
  if (!buildProgress.has_pdf) parts.push('待上传 PDF');
  else if (!buildProgress.parse_done) {
    parts.push(status === 'processing' ? '正在拆分课次' : '待拆分课次');
  } else if (buildProgress.lessons_with_slides < buildProgress.lesson_count) {
    parts.push(`课件 ${buildProgress.lessons_with_slides}/${buildProgress.lesson_count}`);
  } else if (buildProgress.lessons_with_blocks < buildProgress.lesson_count) {
    parts.push(`建块 ${buildProgress.lessons_with_blocks}/${buildProgress.lesson_count}`);
  } else {
    parts.push('本册建设已完成');
  }
  return `${lessonCount} 课 · ${parts[0]}`;
}

function formatVolStatus(status) {
  const m = status.match(/^(\d+ 课) · (.+)$/);
  if (m) return `<strong>${m[1]}</strong> · ${m[2]}`;
  return status;
}

function buildStepStates(buildProgress) {
  if (!buildProgress) return null;
  const n = buildProgress.lesson_count || 0;
  return [
    { label: 'PDF', done: buildProgress.has_pdf },
    { label: '拆分', done: buildProgress.parse_done },
    {
      label: '课件',
      done: n > 0 && buildProgress.lessons_with_slides >= n,
      partial: buildProgress.lessons_with_slides > 0 && buildProgress.lessons_with_slides < n,
    },
    {
      label: '建块',
      done: (
        n > 0
        && buildProgress.lessons_with_slides >= n
        && buildProgress.lessons_with_blocks >= buildProgress.lessons_with_slides
      ),
      partial: (
        buildProgress.lessons_with_blocks > 0
        && !(
          n > 0
          && buildProgress.lessons_with_slides >= n
          && buildProgress.lessons_with_blocks >= buildProgress.lessons_with_slides
        )
      ),
    },
  ];
}

function isVolumeComplete(buildProgress) {
  const steps = buildStepStates(buildProgress);
  return steps && steps.every((s) => s.done);
}

function renderProgressBar(buildProgress) {
  const steps = buildStepStates(buildProgress);
  if (!steps) return '';
  const segs = steps.map((s) => {
    const cls = s.done ? 'done' : s.partial ? 'partial' : '';
    return `<span class="vol-progress-seg ${cls}"></span>`;
  }).join('');
  const labels = steps.map((s) => {
    const cls = s.done ? 'is-done' : s.partial ? 'is-partial' : '';
    return `<span class="${cls}">${s.label}</span>`;
  }).join('');
  return `<div class="vol-progress-bar" aria-hidden="true">${segs}</div>
    <div class="vol-step-labels">${labels}</div>`;
}

function intakeLink(volumeCode) {
  return `/old-library/volumes/${encodeURIComponent(volumeCode)}/intake`;
}

function editionTabLabel(ed) {
  if (ed.school_system === '54' && ed.label === '青岛版') {
    return '青岛版（五四）';
  }
  if (ed.school_system === '54' && (ed.label === '沪科技版' || ed.edition_id === 'hukexue_54')) {
    return '沪科技版（五四）';
  }
  return ed.label;
}

function volTitle(v) {
  return `${v.grade}年级${v.term === '上' ? '上册' : '下册'}`;
}

function countMissingVolumes(ed) {
  return (ed.volumes || []).filter((v) => !(v.lesson_count || 0)).length;
}

function editionToolbar(ed) {
  if (!ed.has_old_benchmark) return '';
  const missing = countMissingVolumes(ed);
  const hint = missing
    ? `还有 ${missing} 册未载入目录。流程：载入本版目录 → 导入本版教材 PDF → 一键开启本版解析。`
    : `本版目录已齐。导入本版教材 PDF 后，可一键开启本版旧教材解析（对各册依次「开始解析」）。`;
  // 与 intake「整册上传 ZIP」同构：label[for=file] 打开系统选框，不用 JS input.click()
  return `<div class="edition-toolbar">
    <p class="edition-toolbar-hint">${hint}</p>
    <div class="edition-toolbar-actions">
      <button type="button" class="btn vol-setup-cta" data-action="bootstrap-all" data-ed="${ed.edition_id}">
        一键载入本版全部基准目录
      </button>
      <label class="btn vol-setup-cta tb-batch-trigger" for="tb-batch-input" title="一次选中本版多册教材 PDF，选完后在下方面板匹配并上传">
        一键导入本版所有教材
      </label>
      <button type="button" class="btn vol-setup-cta" data-action="parse-all" data-ed="${ed.edition_id}" title="对本版已上传 PDF、尚未拆分完成的册依次开始解析">
        一键开启本版旧教材解析
      </button>
    </div>
  </div>`;
}

function volCards(ed) {
  return ed.volumes.map((v) => {
    const count = v.lesson_count || 0;
    const status = parseStatusLabel(v.parse_status, count, v.build_progress);
    const progress = renderProgressBar(v.build_progress);
    const complete = isVolumeComplete(v.build_progress);
    const cardCls = !count ? 'vol-card--setup' : complete ? 'vol-card--complete' : '';

    if (!count) {
      return `<article class="vol-card ${cardCls}">
        <div class="vol-card-head">
          <span class="vol-grade-badge">${v.grade}</span>
          <div>
            <h3 class="vol-card-title">${volTitle(v)}</h3>
            <p class="vol-card-sub">尚未载入目录</p>
          </div>
        </div>
        <p class="vol-status">请用上方「一键载入本版全部基准目录」；单册载入仅作补救</p>
        <button type="button" class="btn secondary" data-action="bootstrap" data-ed="${ed.edition_id}" data-g="${v.grade}" data-t="${v.term}">单册载入</button>
      </article>`;
    }

    return `<article class="vol-card ${cardCls}">
      <div class="vol-card-head">
        <span class="vol-grade-badge">${v.grade}</span>
        <div>
          <h3 class="vol-card-title">${volTitle(v)}</h3>
          <p class="vol-card-sub">${count} 课</p>
        </div>
      </div>
      ${progress}
      <p class="vol-status">${formatVolStatus(status)}</p>
      <a class="vol-build-cta" href="${intakeLink(v.volume_code)}" target="_blank" rel="noopener noreferrer" title="新标签打开，不影响本版解析进度">进入本册建设 →</a>
      <div class="vol-admin">
        <button type="button" class="btn secondary" data-action="sync" data-ed="${ed.edition_id}" data-g="${v.grade}" data-t="${v.term}">同步单元名</button>
        <button type="button" class="btn secondary" data-action="bootstrap" data-ed="${ed.edition_id}" data-g="${v.grade}" data-t="${v.term}">重新载入目录</button>
        <button type="button" class="btn secondary" data-action="replace" data-ed="${ed.edition_id}" data-g="${v.grade}" data-t="${v.term}">覆盖重建</button>
      </div>
    </article>`;
  }).join('');
}

function renderEditions() {
  const root = document.getElementById('editions');
  const pool = editionsForSystem(activeSystem);
  if (!pool.length) {
    root.innerHTML = '<p class="wb-empty-msg">该学制暂无已开放版本</p>';
    return;
  }
  if (!activeEditionId || !pool.find((e) => e.edition_id === activeEditionId)) {
    activeEditionId = pool[0].edition_id;
  }
  const active = pool.find((e) => e.edition_id === activeEditionId) || pool[0];

  const systemTabs = SCHOOL_SYSTEMS.map((s) =>
    `<button type="button" class="tab system-tab${s.id === activeSystem ? ' active' : ''}" data-system="${s.id}">${s.label}</button>`
  ).join('');

  const editionTabs = pool.map((e) =>
    `<button type="button" class="tab edition-tab${e.edition_id === active.edition_id ? ' active' : ''}" data-id="${e.edition_id}">${editionTabLabel(e)}</button>`
  ).join('');

  // 重绘前把面板挪出，避免被 innerHTML 销毁
  parkTbBatchPanel();
  parkParseJobPanel();

  root.innerHTML = `
    <p class="wb-filter-label">学制</p>
    <div class="school-system-tabs">${systemTabs}</div>
    <p class="wb-filter-label">教材版本</p>
    <div class="edition-tabs">${editionTabs}</div>
    ${editionToolbar(active)}
    <div id="tb-batch-mount"></div>
    <div class="vol-grid" id="vol-grid">${volCards(active)}</div>
  `;

  placeTbBatchPanel();
  placeParseJobPanel();
  if (tbBatchFiles.length) renderTbBatchPanel();
  // 回来本页时仅接上「进行中」的后台解析；已完成的不再常驻显示
  if (active.edition_id) {
    refreshParseJob(active.edition_id).then((job) => {
      if (job?.status === 'running' || job?.status === 'cancelling') {
        if (!parseJobPollTimer || parseJobEditionId !== active.edition_id) {
          startParseJobPolling(active.edition_id);
        } else {
          renderParseJobPanel(job);
        }
      } else {
        hideParseJobPanel();
      }
    }).catch(() => {});
  }

  root.querySelectorAll('.system-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      activeSystem = btn.dataset.system;
      renderEditions();
    });
  });

  root.querySelectorAll('.edition-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      activeEditionId = btn.dataset.id;
      renderEditions();
    });
  });

  bindVolButtons();
  syncEditionUrl();
}

function volumesPendingParse(editionId) {
  const ed = allEditions.find((e) => e.edition_id === editionId);
  return (ed?.volumes || []).filter((v) => {
    if (!(v.lesson_count || 0)) return false;
    const hasPdf = !!(v.has_pdf || v.build_progress?.has_pdf);
    if (!hasPdf) return false;
    return v.parse_status !== 'done' && !v.build_progress?.parse_done;
  });
}

let parseJobPollTimer = null;
let parseJobEditionId = null;
/** 进度面板 ↔ 下方册卡片同步签名（跨 load 重绘保持，避免轮询死循环） */
let parseJobCardSig = '';

function parkParseJobPanel() {
  const panel = document.getElementById('ed-parse-panel');
  const park = document.getElementById('tb-batch-park');
  if (panel && park && panel.parentElement !== park) park.appendChild(panel);
}

function placeParseJobPanel() {
  const panel = document.getElementById('ed-parse-panel');
  const mount = document.getElementById('tb-batch-mount');
  if (panel && mount) mount.appendChild(panel);
}

function hideParseJobPanel() {
  const panel = document.getElementById('ed-parse-panel');
  if (panel) panel.hidden = true;
}

function stopParseJobPolling() {
  if (parseJobPollTimer) {
    clearInterval(parseJobPollTimer);
    parseJobPollTimer = null;
  }
}

function renderParseJobPanel(job) {
  const mount = document.getElementById('tb-batch-mount');
  let panel = document.getElementById('ed-parse-panel');
  // 仅进行中展示；完成/取消/空闲直接隐藏
  if (!job || job.status !== 'running') {
    hideParseJobPanel();
    return;
  }
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'ed-parse-panel';
    panel.className = 'ed-parse-panel';
    panel.innerHTML = `
      <div class="ed-parse-panel-head">
        <div class="ed-parse-panel-title">
          <strong>本版旧教材解析</strong>
          <span class="hint" id="ed-parse-summary"></span>
        </div>
        <div class="ed-parse-panel-actions">
          <button type="button" class="btn btn-sm secondary" id="ed-parse-cancel-btn">取消排队</button>
        </div>
      </div>
      <ul class="ed-parse-list" id="ed-parse-list"></ul>
      <p class="ed-parse-note hint">任务在服务器后台执行：可新标签打开「进入本册建设」，离开本页也会继续。</p>
    `;
    const park = document.getElementById('tb-batch-park');
    (mount || park || document.body).appendChild(panel);
    document.getElementById('ed-parse-cancel-btn')?.addEventListener('click', async () => {
      if (!parseJobEditionId) return;
      try {
        await fetch(`/api/old-library/editions/${encodeURIComponent(parseJobEditionId)}/parse-all`, {
          method: 'DELETE',
        });
        toast('已请求取消（当前正在解析的册会跑完）');
      } catch (e) {
        toast(e.message || String(e));
      }
    });
  }
  placeParseJobPanel();
  panel.hidden = false;
  const summary = document.getElementById('ed-parse-summary');
  const list = document.getElementById('ed-parse-list');
  const cancelBtn = document.getElementById('ed-parse-cancel-btn');
  const vols = Object.values(job.volumes || {});
  if (summary) {
    const phase = job.status === 'cancelling' ? '取消中' : '进行中';
    const total = job.volume_count || vols.length || 0;
    const cur = job.current_index || 0;
    summary.textContent = `${phase} 第 ${cur}/${total} 册`
      + (job.current_volume ? ` · ${job.current_volume}` : '')
      + (job.message && job.status === 'cancelling' ? ` · ${job.message}` : '');
  }
  if (cancelBtn) cancelBtn.disabled = false;
  if (list) {
    list.innerHTML = vols.map((v) => {
      const termLabel = v.term === '下' || v.term === 'xia' ? '下' : '上';
      const lessons = v.lesson_count != null ? ` · ${v.lesson_count} 课` : '';
      const title = `${v.grade || ''}年级${termLabel}${lessons} · ${v.volume_code}`;
      const cls = `ed-parse-item ed-parse-item--${v.state || 'queued'}`;
      return `<li class="${cls}"><span>${escapeHtml(title)}</span><span>${escapeHtml(v.label || v.state || '')}</span></li>`;
    }).join('');
  }
}

async function refreshParseJob(editionId) {
  const id = editionId || parseJobEditionId || activeEditionId;
  if (!id) return null;
  const r = await fetch(`/api/old-library/editions/${encodeURIComponent(id)}/parse-all`);
  const d = await readJson(r);
  const job = d.job;
  if (job?.status === 'running' || job?.status === 'cancelling') {
    parseJobEditionId = id;
    renderParseJobPanel(job);
  } else {
    hideParseJobPanel();
  }
  return job;
}

function _parseJobCardSig(job) {
  if (!job) return '';
  const vols = Object.values(job.volumes || {})
    .map((v) => `${v.volume_code}:${v.state || ''}`)
    .sort()
    .join('|');
  return `${job.status || ''}:${job.current_index || 0}:${vols}`;
}

function startParseJobPolling(editionId) {
  parseJobEditionId = editionId;
  stopParseJobPolling();
  const tick = async () => {
    try {
      const job = await refreshParseJob(editionId);
      if (!job || (job.status !== 'running' && job.status !== 'cancelling')) {
        stopParseJobPolling();
        hideParseJobPanel();
        parseJobCardSig = '';
        if (job && (job.status === 'done' || job.status === 'cancelled')) {
          toast(job.message || '本版解析已结束');
          load();
        }
        return;
      }
      // 单册完成/开始解析时同步刷新下方册卡片（否则面板已「已完成」而卡片仍「待拆分」）
      const sig = _parseJobCardSig(job);
      if (sig !== parseJobCardSig) {
        parseJobCardSig = sig;
        load();
      }
    } catch (e) {
      /* 轮询失败不打断后台任务 */
    }
  };
  tick();
  parseJobPollTimer = setInterval(tick, 2500);
}

async function parseEditionAll(editionId) {
  const pending = volumesPendingParse(editionId);
  // 若已有后台任务，直接接上轮询
  try {
    const existing = await refreshParseJob(editionId);
    if (existing?.status === 'running' || existing?.status === 'cancelling') {
      toast(existing.status === 'cancelling'
        ? '本版解析正在取消，请稍候…'
        : '本版解析已在后台进行中，继续显示进度');
      startParseJobPolling(editionId);
      return { ok: true, already_running: true, job: existing };
    }
  } catch (e) { /* ignore */ }

  if (!pending.length) {
    toast('本版没有待解析的册（需已上传 PDF，且尚未拆分完成）');
    return { ok: true, parsed_count: 0 };
  }
  if (!confirm(
    `将对约 ${pending.length} 册在后台依次解析（等同各册「开始解析」）。\n`
    + '可新标签打开「进入本册建设」，离开本页也不会中断。继续？',
  )) {
    return null;
  }

  const r = await fetch(`/api/old-library/editions/${encodeURIComponent(editionId)}/parse-all`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ only_pending: true }),
  });
  const d = await readJson(r);
  const job = d.job;
  if (!job || job.status === 'idle') {
    toast(job?.message || d.message || '没有待解析的册');
    return d;
  }
  toast(d.already_running
    ? '本版解析已在后台进行中'
    : `本版解析已在后台启动（${job.volume_count || pending.length} 册），可新开标签进册建设`);
  renderParseJobPanel(job);
  startParseJobPolling(editionId);
  return d;
}

function bindVolButtons() {
  document.querySelectorAll('[data-action]').forEach((btn) => {
    btn.onclick = async () => {
      const action = btn.dataset.action;
      const replace = action === 'replace';
      if (replace && !confirm('将清空该册已有课时并重新从基准库导入，继续？')) return;
      try {
        btn.disabled = true;
        if (action === 'bootstrap-all') {
          const d = await bootstrapAll(btn.dataset.ed);
          const msg = window.OldLibraryBootstrapAll?.formatResultToast
            ? window.OldLibraryBootstrapAll.formatResultToast(d)
            : `新建 ${d.created_count || 0} 册 · 共 ${d.lessons_created_total || 0} 课`;
          toast(msg);
          load();
        } else if (action === 'parse-all') {
          const d = await parseEditionAll(btn.dataset.ed);
          if (d === null) return;
          // 后台任务：不立刻整页重载打断面板；结束时轮询会 load()
        } else if (action === 'sync') {
          const d = await syncBenchmark(btn.dataset.ed, Number(btn.dataset.g), btn.dataset.t);
          toast(`已同步 ${d.lessons_updated}/${d.lesson_count} 课单元名 · ${d.volume_code}`);
          load();
        } else {
          const d = await bootstrap(btn.dataset.ed, Number(btn.dataset.g), btn.dataset.t, replace);
          toast(`已载入 ${d.lessons_created} 课 · ${d.volume_code}（可进入本册建设）`);
          load();
        }
      } catch (e) {
        toast(e.message);
      } finally {
        btn.disabled = false;
      }
    };
  });
}

/* ── 本版教材 PDF 批量导入（同构于 intake 整册课件 ZIP）── */
let tbBatchFiles = [];
let tbBatchPlan = [];
const tbBatchProgress = new Map();
let tbBatchUploading = false;

function parkTbBatchPanel() {
  const panel = document.getElementById('tb-batch-panel');
  const park = document.getElementById('tb-batch-park');
  if (panel && park && panel.parentElement !== park) park.appendChild(panel);
}

function placeTbBatchPanel() {
  const panel = document.getElementById('tb-batch-panel');
  const mount = document.getElementById('tb-batch-mount');
  if (panel && mount) mount.appendChild(panel);
}

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function shortFileName(name, max = 40) {
  const s = String(name || '');
  if (s.length <= max) return s;
  return `${s.slice(0, max - 1)}…`;
}

function activeEditionVolumes() {
  const ed = allEditions.find((e) => e.edition_id === activeEditionId);
  return ed?.volumes || [];
}

function volumeSelectHtml(selectedCode) {
  const vols = activeEditionVolumes();
  const opts = [`<option value="">— 未匹配 —</option>`];
  for (const v of vols) {
    const title = `${v.grade}年级${v.term === '上' ? '上' : '下'} · ${v.volume_code}`;
    const note = !(v.lesson_count || 0) ? '（未载目录）' : (v.build_progress?.has_pdf ? '（已有 PDF）' : '');
    const sel = v.volume_code === selectedCode ? ' selected' : '';
    opts.push(`<option value="${escapeHtml(v.volume_code)}"${sel}>${escapeHtml(title + note)}</option>`);
  }
  return opts.join('');
}

function tbBatchProgressLabel(state, percent, customLabel) {
  if (customLabel) return customLabel;
  if (state === 'uploading') return `上传 ${percent || 0}%`;
  if (state === 'done') return '完成';
  if (state === 'error') return '失败';
  if (state === 'skip') return '跳过';
  return '待上传';
}

function tbBatchProgressHtml(filename) {
  const saved = tbBatchProgress.get(filename);
  const state = saved?.state || 'pending';
  const percent = saved?.percent || 0;
  const label = tbBatchProgressLabel(state, percent, saved?.label);
  return (
    `<td class="tb-batch-progress-cell">` +
    `<div class="tb-batch-progress tb-batch-progress--${state}" role="progressbar" ` +
    `data-progress-file="${escapeHtml(filename)}" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100">` +
    `<div class="tb-batch-progress-bar" style="width:${percent}%"></div>` +
    `</div>` +
    `<span class="tb-batch-progress-label tb-batch-progress--${state}">${escapeHtml(label)}</span>` +
    `</td>`
  );
}

function updateTbBatchProgressRow(filename, patch) {
  const prev = tbBatchProgress.get(filename) || { percent: 0, state: 'pending', label: '' };
  const next = {
    percent: patch.percent ?? prev.percent,
    state: patch.state ?? prev.state,
    label: patch.label ?? tbBatchProgressLabel(
      patch.state ?? prev.state,
      patch.percent ?? prev.percent,
      patch.label,
    ),
  };
  tbBatchProgress.set(filename, next);
  const track = document.querySelector(
    `.tb-batch-progress[data-progress-file="${CSS.escape(filename)}"]`,
  );
  const labelEl = track?.closest('.tb-batch-progress-cell')?.querySelector('.tb-batch-progress-label');
  if (!track) return;
  track.className = `tb-batch-progress tb-batch-progress--${next.state}`;
  track.setAttribute('aria-valuenow', String(next.percent));
  const bar = track.querySelector('.tb-batch-progress-bar');
  if (bar) bar.style.width = `${next.percent}%`;
  if (labelEl) {
    labelEl.className = `tb-batch-progress-label tb-batch-progress--${next.state}`;
    labelEl.textContent = next.label;
  }
}

function syncTbBatchBar() {
  const previewBtn = document.getElementById('tb-batch-preview-btn');
  const uploadBtn = document.getElementById('tb-batch-upload-btn');
  const hasFiles = tbBatchFiles.length > 0;
  const matched = tbBatchPlan.some((p) => p.volume_code);
  if (previewBtn) previewBtn.disabled = !hasFiles || tbBatchUploading;
  if (uploadBtn) uploadBtn.disabled = !hasFiles || !matched || tbBatchUploading;
  document.querySelectorAll('.tb-batch-trigger').forEach((el) => {
    el.classList.toggle('is-disabled', tbBatchUploading);
  });
}

function renderTbBatchPanel() {
  const panel = document.getElementById('tb-batch-panel');
  const tbody = document.getElementById('tb-batch-rows');
  const summary = document.getElementById('tb-batch-summary');
  if (!panel || !tbody) return;

  placeTbBatchPanel();

  if (!tbBatchFiles.length) {
    panel.hidden = true;
    tbBatchPlan = [];
    tbBatchProgress.clear();
    syncTbBatchBar();
    return;
  }

  panel.hidden = false;
  const matched = tbBatchPlan.filter((p) => p.volume_code).length;
  if (summary) {
    summary.textContent = `已选 ${tbBatchFiles.length} 个 PDF · 已匹配 ${matched} 册`;
  }
  const planByName = new Map(tbBatchPlan.map((p) => [p.filename, p]));
  tbody.innerHTML = tbBatchFiles.map((file) => {
    const plan = planByName.get(file.name) || {};
    const statusText = plan.volume_code
      ? (plan.has_catalog === false ? '未载目录' : (plan.has_pdf ? '将覆盖/跳过' : '可导入'))
      : (plan.reason || '未匹配');
    const statusClass = plan.volume_code && plan.has_catalog !== false ? 'tb-batch-ok' : 'tb-batch-warn';
    const selectDisabled = tbBatchUploading ? ' disabled' : '';
    const score = plan.score != null ? Number(plan.score).toFixed(2) : '—';
    return `<tr>
      <td class="tb-batch-filename" title="${escapeHtml(file.name)}">${escapeHtml(shortFileName(file.name, 36))}</td>
      <td>
        <select class="tb-batch-vol-select" data-filename="${escapeHtml(file.name)}"${selectDisabled}>
          ${volumeSelectHtml(plan.volume_code || '')}
        </select>
      </td>
      <td>${escapeHtml(score)}</td>
      <td class="${statusClass}">${escapeHtml(statusText)}</td>
      ${tbBatchProgressHtml(file.name)}
    </tr>`;
  }).join('');

  tbody.querySelectorAll('.tb-batch-vol-select').forEach((sel) => {
    sel.addEventListener('change', () => {
      if (tbBatchUploading) return;
      const filename = sel.dataset.filename;
      const volumeCode = sel.value;
      const existing = tbBatchPlan.find((p) => p.filename === filename);
      const vol = activeEditionVolumes().find((v) => v.volume_code === volumeCode);
      const row = {
        filename,
        volume_code: volumeCode,
        score: existing?.score,
        reason: volumeCode ? '手动指定' : '未匹配',
        has_catalog: volumeCode ? !!(vol && (vol.lesson_count || 0) > 0) : false,
        has_pdf: !!(vol?.build_progress?.has_pdf),
        manual: true,
      };
      if (existing) Object.assign(existing, row);
      else tbBatchPlan.push(row);
      renderTbBatchPanel();
    });
  });
  syncTbBatchBar();
  // 面板插在工具栏下；滚入视口，避免被册次卡片「顶没」
  requestAnimationFrame(() => {
    panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  });
}

async function previewTbBatchMatch() {
  if (!tbBatchFiles.length) {
    toast('请先点「一键导入本版所有教材」选择 PDF');
    return;
  }
  if (!activeEditionId) {
    toast('请先选择教材版本');
    return;
  }
  const btn = document.getElementById('tb-batch-preview-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '匹配中…';
  }
  try {
    const r = await fetch(
      `/api/old-library/editions/${encodeURIComponent(activeEditionId)}/import-textbooks?preview=1`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview: true, filenames: tbBatchFiles.map((f) => f.name) }),
      },
    );
    const d = await readJson(r);
    tbBatchPlan = (d.matches || []).map((m) => ({
      filename: m.filename,
      volume_code: m.volume_code,
      score: m.score,
      reason: m.reason,
      has_catalog: m.has_catalog,
      has_pdf: m.has_pdf,
      manual: false,
    }));
    tbBatchProgress.clear();
    const matchedNames = new Set(tbBatchPlan.map((p) => p.filename));
    for (const row of d.unmatched || []) {
      if (matchedNames.has(row.filename)) continue;
      const sug = row.suggestions?.[0];
      tbBatchPlan.push({
        filename: row.filename,
        volume_code: sug?.volume_code || '',
        score: sug?.score,
        reason: sug ? `建议：${sug.reason || sug.volume_code}` : '未匹配',
        has_catalog: undefined,
        has_pdf: false,
        manual: false,
      });
    }
    renderTbBatchPanel();
    const n = tbBatchPlan.filter((p) => p.volume_code).length;
    toast(`匹配完成：${n}/${tbBatchFiles.length} 个 PDF 已对应册次，请核对后确认上传`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.textContent = '预览匹配';
      syncTbBatchBar();
    }
  }
}

function uploadPdfWithProgress(volumeCode, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/old-library/volumes/${encodeURIComponent(volumeCode)}/pdf`);
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.max(0, Math.min(99, Math.round((ev.loaded / ev.total) * 100)));
      onProgress?.(pct);
    };
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText || '{}'); } catch (e) { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300 && d.ok !== false) {
        onProgress?.(100);
        resolve(d);
      } else {
        reject(new Error(d.error || `上传失败 HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error('网络错误，上传中断'));
    const fd = new FormData();
    fd.append('pdf', file, file.name);
    xhr.send(fd);
  });
}

async function uploadTbBatch() {
  const tasks = tbBatchFiles.map((file) => {
    const row = tbBatchPlan.find((p) => p.filename === file.name);
    return { file, row };
  }).filter((t) => t.row?.volume_code);

  if (!tasks.length) {
    toast('没有可上传的匹配项，请先预览匹配或手动选择册次');
    return;
  }

  const btn = document.getElementById('tb-batch-upload-btn');
  tbBatchUploading = true;
  syncTbBatchBar();
  if (btn) btn.textContent = '上传中…';

  let okCount = 0;
  let errCount = 0;
  let skipCount = 0;

  for (const file of tbBatchFiles) {
    const row = tbBatchPlan.find((p) => p.filename === file.name);
    if (!row?.volume_code) {
      updateTbBatchProgressRow(file.name, { state: 'skip', percent: 0, label: '跳过' });
      skipCount += 1;
      continue;
    }
    const vol = activeEditionVolumes().find((v) => v.volume_code === row.volume_code);
    if (vol && !(vol.lesson_count || 0)) {
      updateTbBatchProgressRow(file.name, { state: 'skip', percent: 0, label: '未载目录' });
      skipCount += 1;
      continue;
    }
    if (row.has_pdf || vol?.build_progress?.has_pdf) {
      // 默认跳过已有 PDF（与 only_missing 一致）；需要覆盖时可再扩「强制覆盖」
      updateTbBatchProgressRow(file.name, { state: 'skip', percent: 0, label: '已有 PDF' });
      skipCount += 1;
      continue;
    }
    updateTbBatchProgressRow(file.name, { state: 'uploading', percent: 0, label: '上传 0%' });
    try {
      await uploadPdfWithProgress(row.volume_code, file, (pct) => {
        updateTbBatchProgressRow(file.name, {
          state: 'uploading',
          percent: pct,
          label: pct >= 100 ? '写入中…' : `上传 ${pct}%`,
        });
      });
      updateTbBatchProgressRow(file.name, { state: 'done', percent: 100, label: '完成' });
      okCount += 1;
    } catch (e) {
      updateTbBatchProgressRow(file.name, {
        state: 'error',
        percent: 100,
        label: e.message || '失败',
      });
      errCount += 1;
    }
  }

  tbBatchUploading = false;
  if (btn) btn.textContent = '确认上传';
  syncTbBatchBar();
  toast(`上传结束：成功 ${okCount} · 跳过 ${skipCount} · 失败 ${errCount}`);
  if (okCount > 0) load();
}

function bindTbBatchUi() {
  const input = document.getElementById('tb-batch-input');
  const previewBtn = document.getElementById('tb-batch-preview-btn');
  const uploadBtn = document.getElementById('tb-batch-upload-btn');
  const closeBtn = document.getElementById('tb-batch-close');
  if (!input) return;

  input.addEventListener('change', () => {
    if (tbBatchUploading) return;
    tbBatchFiles = Array.from(input.files || []).filter((f) => f && f.name);
    tbBatchPlan = [];
    tbBatchProgress.clear();
    input.value = '';
    if (!tbBatchFiles.length) {
      toast('未选择 PDF 文件');
      renderTbBatchPanel();
      return;
    }
    toast(`已选中 ${tbBatchFiles.length} 个 PDF，正在自动匹配…`);
    renderTbBatchPanel();
    previewTbBatchMatch();
  });

  previewBtn?.addEventListener('click', () => {
    if (tbBatchUploading) return;
    previewTbBatchMatch();
  });
  uploadBtn?.addEventListener('click', () => {
    if (tbBatchUploading) return;
    uploadTbBatch();
  });
  closeBtn?.addEventListener('click', () => {
    if (tbBatchUploading) return;
    tbBatchFiles = [];
    tbBatchPlan = [];
    tbBatchProgress.clear();
    renderTbBatchPanel();
  });
}

async function load() {
  try {
    const r = await fetch('/api/old-library/editions');
    const d = await readJson(r);
    allEditions = d.editions || [];
    applyEditionFromUrl();
    renderEditions();
  } catch (e) {
    document.getElementById('editions').innerHTML =
      `<p class="wb-empty-msg">加载失败：${e.message || '请刷新重试'}</p>`;
  }
}

bindTbBatchUi();
load();
