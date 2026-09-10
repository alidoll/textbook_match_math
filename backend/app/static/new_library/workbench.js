function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 5000);
}

function termToken(term) {
  return term === '下' ? 'xia' : 'shang';
}

const SCHOOL_SYSTEMS = [
  { id: '63', label: '小学 · 六三学制' },
  { id: '54', label: '小学 · 五·四学制' },
];

const PIPELINE_SHORT = {
  pdf_upload: 'PDF',
  catalog_extract: '识别目录',
  lesson_split: '页码划分',
  coarse_match: '旧库粗分',
  block_build: '新区块建立',
};

let allEditions = [];
let activeSystem = '63';
let activeEditionId = null;

let tbBatchFiles = [];
let tbBatchPlan = [];
const tbBatchProgress = new Map();
let tbBatchUploading = false;

let preprocessJobPollTimer = null;
let preprocessJobEditionId = null;
let preprocessJobCardSig = '';

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

async function readJsonResponse(r) {
  const text = await r.text();
  let d;
  try {
    d = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`服务器返回异常（HTTP ${r.status}）`);
  }
  if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}

async function ensureVolume(editionId, grade, term) {
  const t = termToken(term);
  const r = await fetch(`/api/new-library/volumes/${editionId}/${grade}/${t}/ensure`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  return readJsonResponse(r);
}

function editionsForSystem(systemId) {
  return allEditions.filter((e) => e.school_system === systemId);
}

function statusFromPipeline(pipeline, lessonCount, inDb) {
  if (!pipeline?.steps?.length) {
    return inDb ? '尚未上传 PDF' : '尚未开始建设';
  }
  if (pipeline.all_done) return `${lessonCount} 课 · 本册建设已完成`;
  const active = pipeline.steps[pipeline.active_index] ?? pipeline.steps[0];
  if (active.busy) return `${lessonCount} 课 · ${active.label}进行中…`;
  if (active.progress) return `${lessonCount} 课 · ${active.label} ${active.progress}`;
  if (lessonCount > 0) return `${lessonCount} 课 · ${active.label}`;
  return active.label;
}

function formatVolStatus(status) {
  const m = status.match(/^(\d+ 课) · (.+)$/);
  if (m) return `<strong>${m[1]}</strong> · ${m[2]}`;
  return status;
}

function renderProgressBar(pipeline) {
  if (!pipeline?.steps?.length) return '';
  const segs = pipeline.steps.map((step) => {
    const done = step.done;
    const partial = !done && step.ratio > 0;
    const cls = done ? 'done' : partial ? 'partial' : '';
    return `<span class="vol-progress-seg ${cls}"></span>`;
  }).join('');
  const labels = pipeline.steps.map((step) => {
    const done = step.done;
    const partial = !done && step.ratio > 0;
    const cls = done ? 'is-done' : partial ? 'is-partial' : '';
    const short = PIPELINE_SHORT[step.key] || step.label;
    return `<span class="${cls}" title="${step.label}">${short}</span>`;
  }).join('');
  return `<div class="vol-progress-bar vol-progress-bar--new" aria-hidden="true">${segs}</div>
    <div class="vol-step-labels vol-step-labels--new">${labels}</div>`;
}

function editionTabLabel(ed) {
  if (ed.school_system === '54' && ed.label === '青岛版') return '青岛版（五四）';
  if (ed.school_system === '54' && (ed.label === '沪科技版' || ed.edition_id === 'hukexue_54')) {
    return '沪科技版（五四）';
  }
  return ed.label;
}

function volTitle(v) {
  return `${v.grade}年级${v.term === '上' ? '上册' : '下册'}`;
}

function intakeLink(volumeCode) {
  return `/new-library/volumes/${encodeURIComponent(volumeCode)}/intake`;
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

function countMissingPdf(ed) {
  return (ed.volumes || []).filter((v) => !v.has_pdf).length;
}

function editionToolbar(ed) {
  const missing = countMissingPdf(ed);
  const hint = missing
    ? `还有 ${missing} 册未上传 PDF。可多选本版新教材 PDF 一次导入，再一键整册预处理。`
    : `本版 PDF 已齐。可一键对本版待处理册依次跑整册预处理（目录→划分→页图→粗分）。`;
  return `<div class="edition-toolbar">
    <p class="edition-toolbar-hint">${hint}</p>
    <div class="edition-toolbar-actions">
      <label class="btn vol-setup-cta tb-batch-trigger" for="tb-batch-input" title="一次选中本版多册新教材 PDF，选完后在下方面板匹配并上传">
        一键导入本版所有教材
      </label>
      <button type="button" class="btn vol-setup-cta" data-action="preprocess-all" data-ed="${ed.edition_id}" title="对本版已上传 PDF、尚未完成预处理的册依次开始">
        一键开启本版整册预处理
      </button>
      <button type="button" class="btn vol-setup-cta" data-action="export-coarse" data-ed="${ed.edition_id}" title="勾选册次，下载已匹配/未匹配课时粗分 Excel">
        下载本版粗分结果
      </button>
    </div>
  </div>`;
}

function volHasCoarse(pipeline) {
  const step = (pipeline?.steps || []).find((s) => s.key === 'coarse_match');
  return Boolean(step?.done);
}

function volCards(ed) {
  return ed.volumes.map((v) => {
    const count = v.lesson_count || 0;
    const pipeline = v.pipeline;
    const status = statusFromPipeline(pipeline, count, v.in_db);
    const progress = renderProgressBar(pipeline);
    const complete = pipeline?.all_done;
    const setup = !v.in_db && !count;
    const cardCls = setup ? 'vol-card--setup' : complete ? 'vol-card--complete' : '';
    const sub = setup ? '尚未开始' : `${count} 课`;
    const coarseBtn = volHasCoarse(pipeline)
      ? `<button type="button" class="btn btn-sm vol-coarse-dl" data-action="export-volume-coarse" data-code="${escapeHtml(v.volume_code)}" title="下载本册粗分结果 Excel">下载粗分</button>`
      : '';

    return `<article class="vol-card ${cardCls}">
      <div class="vol-card-head">
        <span class="vol-grade-badge">${v.grade}</span>
        <div>
          <h3 class="vol-card-title">${volTitle(v)}</h3>
          <p class="vol-card-sub">${sub}</p>
        </div>
      </div>
      ${progress}
      <p class="vol-status">${formatVolStatus(status)}</p>
      <div class="vol-card-actions">
        ${coarseBtn}
        <a class="vol-build-cta" href="${intakeLink(v.volume_code)}" data-action="enter" data-ed="${ed.edition_id}" data-g="${v.grade}" data-t="${v.term}" data-in-db="${v.in_db ? '1' : ''}">进入本册建设 →</a>
      </div>
    </article>`;
  }).join('');
}

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

function parkPreprocessPanel() {
  const panel = document.getElementById('ed-prep-panel');
  const park = document.getElementById('tb-batch-park');
  if (panel && park && panel.parentElement !== park) park.appendChild(panel);
}

function placePreprocessPanel() {
  const panel = document.getElementById('ed-prep-panel');
  const mount = document.getElementById('tb-batch-mount');
  if (panel && mount) mount.appendChild(panel);
}

function parkCoarseExportPanel() {
  const panel = document.getElementById('ed-coarse-export-panel');
  const park = document.getElementById('tb-batch-park');
  if (panel && park && panel.parentElement !== park) park.appendChild(panel);
}

function placeCoarseExportPanel() {
  const panel = document.getElementById('ed-coarse-export-panel');
  const mount = document.getElementById('tb-batch-mount');
  if (panel && mount) mount.appendChild(panel);
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

  parkTbBatchPanel();
  parkPreprocessPanel();
  parkCoarseExportPanel();

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
  placePreprocessPanel();
  placeCoarseExportPanel();
  if (tbBatchFiles.length) renderTbBatchPanel();

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

  bindVolLinks();
  bindToolbarActions();
  syncEditionUrl();

  if (active.edition_id) {
    refreshPreprocessJob(active.edition_id).then((job) => {
      if (job?.status === 'running' || job?.status === 'cancelling') {
        if (!preprocessJobPollTimer || preprocessJobEditionId !== active.edition_id) {
          startPreprocessJobPolling(active.edition_id);
        } else {
          renderPreprocessJobPanel(job);
        }
      }
    }).catch(() => {});
  }
}

function bindVolLinks() {
  document.querySelectorAll('[data-action="enter"]').forEach((link) => {
    link.onclick = async (e) => {
      if (link.dataset.inDb === '1') return;
      e.preventDefault();
      try {
        link.classList.add('is-busy');
        await ensureVolume(link.dataset.ed, Number(link.dataset.g), link.dataset.t);
        window.location.href = link.getAttribute('href');
      } catch (err) {
        toast(err.message);
        link.classList.remove('is-busy');
      }
    };
  });
}

function bindToolbarActions() {
  document.querySelectorAll('[data-action="preprocess-all"]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        btn.disabled = true;
        await preprocessEditionAll(btn.dataset.ed);
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        btn.disabled = false;
      }
    };
  });
  document.querySelectorAll('[data-action="export-coarse"]').forEach((btn) => {
    btn.onclick = () => openCoarseExportPanel(btn.dataset.ed);
  });
  document.querySelectorAll('[data-action="export-volume-coarse"]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        btn.disabled = true;
        await downloadVolumeCoarseXlsx(btn.dataset.code);
      } catch (e) {
        toast(e.message || String(e));
      } finally {
        btn.disabled = false;
      }
    };
  });
}

function activeEditionVolumes() {
  const ed = allEditions.find((e) => e.edition_id === activeEditionId);
  return ed?.volumes || [];
}

function volumeSelectHtml(selectedCode) {
  const vols = activeEditionVolumes();
  const opts = ['<option value="">— 未匹配 —</option>'];
  for (const v of vols) {
    const title = `${v.grade}年级${v.term === '上' ? '上' : '下'} · ${v.volume_code}`;
    const note = v.has_pdf ? '（已有 PDF）' : '';
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
    `<td class="tb-batch-progress-cell">`
    + `<div class="tb-batch-progress tb-batch-progress--${state}" role="progressbar" `
    + `data-progress-file="${escapeHtml(filename)}" aria-valuenow="${percent}" aria-valuemin="0" aria-valuemax="100">`
    + `<div class="tb-batch-progress-bar" style="width:${percent}%"></div>`
    + `</div>`
    + `<span class="tb-batch-progress-label tb-batch-progress--${state}">${escapeHtml(label)}</span>`
    + `</td>`
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
  track.setAttribute('aria-valuenow', String(next.percent || 0));
  const bar = track.querySelector('.tb-batch-progress-bar');
  if (bar) bar.style.width = `${next.percent || 0}%`;
  if (labelEl) {
    labelEl.className = `tb-batch-progress-label tb-batch-progress--${next.state}`;
    labelEl.textContent = next.label;
  }
}

function syncTbBatchBar() {
  const previewBtn = document.getElementById('tb-batch-preview-btn');
  const uploadBtn = document.getElementById('tb-batch-upload-btn');
  const hasFiles = tbBatchFiles.length > 0;
  const mapped = tbBatchPlan.filter((p) => p.volume_code).length;
  if (previewBtn) previewBtn.disabled = !hasFiles || tbBatchUploading;
  if (uploadBtn) uploadBtn.disabled = !hasFiles || !mapped || tbBatchUploading;
  document.querySelectorAll('.tb-batch-trigger').forEach((el) => {
    el.classList.toggle('is-busy', tbBatchUploading);
  });
}

function renderTbBatchPanel() {
  const panel = document.getElementById('tb-batch-panel');
  const tbody = document.getElementById('tb-batch-rows');
  const summary = document.getElementById('tb-batch-summary');
  if (!panel || !tbody) return;
  if (!tbBatchFiles.length) {
    panel.hidden = true;
    tbody.innerHTML = '';
    if (summary) summary.textContent = '';
    syncTbBatchBar();
    return;
  }
  placeTbBatchPanel();
  panel.hidden = false;
  const planByName = new Map(tbBatchPlan.map((p) => [p.filename, p]));
  const matched = tbBatchPlan.filter((p) => p.volume_code).length;
  if (summary) {
    summary.textContent = `已选 ${tbBatchFiles.length} 个 · 已匹配 ${matched} 个`;
  }
  tbody.innerHTML = tbBatchFiles.map((file) => {
    const plan = planByName.get(file.name) || {};
    const score = plan.score != null ? Number(plan.score).toFixed(2) : '—';
    const statusClass = plan.volume_code ? 'tb-batch-ok' : 'tb-batch-warn';
    const statusText = plan.has_pdf
      ? '册已有 PDF（将跳过，除非改匹配）'
      : (plan.volume_code ? (plan.reason || '已匹配') : (plan.reason || '未匹配'));
    const selectDisabled = tbBatchUploading ? ' disabled' : '';
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
        has_pdf: !!(vol && vol.has_pdf),
        manual: true,
      };
      if (existing) Object.assign(existing, row);
      else tbBatchPlan.push(row);
      renderTbBatchPanel();
    });
  });
  syncTbBatchBar();
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
      `/api/new-library/editions/${encodeURIComponent(activeEditionId)}/import-textbooks?preview=1`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preview: true, filenames: tbBatchFiles.map((f) => f.name) }),
      },
    );
    const d = await readJsonResponse(r);
    tbBatchPlan = (d.matches || []).map((m) => ({
      filename: m.filename,
      volume_code: m.volume_code,
      score: m.score,
      reason: m.reason,
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
    xhr.open('POST', `/api/new-library/volumes/${encodeURIComponent(volumeCode)}/pdf`);
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.max(0, Math.min(99, Math.round((ev.loaded / ev.total) * 100)));
      onProgress?.(pct);
    };
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText || '{}'); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300 && d.ok !== false) resolve(d);
      else reject(new Error(d.error || `HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    const fd = new FormData();
    fd.append('pdf', file, file.name);
    xhr.send(fd);
  });
}

async function uploadTbBatch() {
  const mapped = tbBatchPlan.filter((p) => p.volume_code);
  if (!mapped.length) {
    toast('请先完成匹配');
    return;
  }
  if (!confirm(`将上传 ${mapped.length} 个 PDF 到对应新库册次（已有 PDF 的册会跳过）。继续？`)) return;

  tbBatchUploading = true;
  syncTbBatchBar();
  const btn = document.getElementById('tb-batch-upload-btn');
  if (btn) btn.textContent = '上传中…';

  const byName = new Map(tbBatchFiles.map((f) => [f.name, f]));
  let okCount = 0;
  let skipCount = 0;
  let errCount = 0;

  // 优先走服务端批量（含建册 + only_missing）
  try {
    const fd = new FormData();
    const mapping = [];
    for (const plan of mapped) {
      const file = byName.get(plan.filename);
      if (!file) continue;
      fd.append('pdf', file, file.name);
      mapping.push({ filename: plan.filename, volume_code: plan.volume_code });
    }
    fd.append('mapping', JSON.stringify(mapping));
    fd.append('only_missing', '1');
    const r = await fetch(
      `/api/new-library/editions/${encodeURIComponent(activeEditionId)}/import-textbooks`,
      { method: 'POST', body: fd },
    );
    const d = await readJsonResponse(r);
    for (const row of d.imported || []) {
      updateTbBatchProgressRow(row.filename, { state: 'done', percent: 100, label: '完成' });
      okCount += 1;
    }
    for (const row of d.skipped || []) {
      updateTbBatchProgressRow(row.filename, { state: 'skip', percent: 100, label: '已有 PDF' });
      skipCount += 1;
    }
    for (const row of d.errors || []) {
      updateTbBatchProgressRow(row.filename, {
        state: 'error',
        percent: 100,
        label: row.error || '失败',
      });
      errCount += 1;
    }
  } catch (e) {
    // 回退：逐册上传
    for (const plan of mapped) {
      const file = byName.get(plan.filename);
      if (!file) continue;
      const vol = activeEditionVolumes().find((v) => v.volume_code === plan.volume_code);
      if (vol?.has_pdf) {
        updateTbBatchProgressRow(plan.filename, { state: 'skip', percent: 100, label: '已有 PDF' });
        skipCount += 1;
        continue;
      }
      try {
        updateTbBatchProgressRow(plan.filename, { state: 'uploading', percent: 0 });
        // ensure then upload
        const m = String(plan.volume_code).match(/-(\d)([SX])-NEW$/i);
        if (m) {
          await ensureVolume(
            activeEditionId,
            Number(m[1]),
            m[2].toUpperCase() === 'X' ? '下' : '上',
          );
        }
        await uploadPdfWithProgress(plan.volume_code, file, (pct) => {
          updateTbBatchProgressRow(plan.filename, {
            state: 'uploading',
            percent: pct,
            label: pct >= 100 ? '写入中…' : `上传 ${pct}%`,
          });
        });
        updateTbBatchProgressRow(plan.filename, { state: 'done', percent: 100, label: '完成' });
        okCount += 1;
      } catch (err) {
        updateTbBatchProgressRow(plan.filename, {
          state: 'error',
          percent: 100,
          label: err.message || '失败',
        });
        errCount += 1;
      }
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

/* ── 本版整册预处理 ── */

function hidePreprocessJobPanel() {
  const panel = document.getElementById('ed-prep-panel');
  if (panel) panel.hidden = true;
}

function stopPreprocessJobPolling() {
  if (preprocessJobPollTimer) {
    clearInterval(preprocessJobPollTimer);
    preprocessJobPollTimer = null;
  }
}

function renderPreprocessJobPanel(job) {
  const mount = document.getElementById('tb-batch-mount');
  let panel = document.getElementById('ed-prep-panel');
  if (!job || (job.status !== 'running' && job.status !== 'cancelling')) {
    hidePreprocessJobPanel();
    return;
  }
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'ed-prep-panel';
    panel.className = 'ed-parse-panel';
    panel.innerHTML = `
      <div class="ed-parse-panel-head">
        <div class="ed-parse-panel-title">
          <strong>本版整册预处理</strong>
          <span class="hint" id="ed-prep-summary"></span>
        </div>
        <div class="ed-parse-panel-actions">
          <button type="button" class="btn btn-sm secondary" id="ed-prep-cancel-btn">取消排队</button>
        </div>
      </div>
      <ul class="ed-parse-list" id="ed-prep-list"></ul>
      <p class="ed-parse-note hint">后台串行：目录 → 划分 → 页图 → 粗分。可新标签进入本册建设，离开本页也会继续。</p>
    `;
    const park = document.getElementById('tb-batch-park');
    (mount || park || document.body).appendChild(panel);
    document.getElementById('ed-prep-cancel-btn')?.addEventListener('click', async () => {
      if (!preprocessJobEditionId) return;
      try {
        await fetch(
          `/api/new-library/editions/${encodeURIComponent(preprocessJobEditionId)}/preprocess-all/cancel`,
          { method: 'POST' },
        );
        toast('已请求取消（当前正在处理的册会跑完）');
      } catch (e) {
        toast(e.message || String(e));
      }
    });
  }
  placePreprocessPanel();
  panel.hidden = false;
  const summary = document.getElementById('ed-prep-summary');
  const list = document.getElementById('ed-prep-list');
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
      const lessons = v.lesson_count != null ? ` · ${v.lesson_count} 课` : '';
      const title = `${v.grade || ''}年级${termLabel}${lessons} · ${v.volume_code}`;
      const cls = `ed-parse-item ed-parse-item--${v.state || 'queued'}`;
      return `<li class="${cls}"><span>${escapeHtml(title)}</span><span>${escapeHtml(v.label || v.state || '')}</span></li>`;
    }).join('');
  }
}

async function refreshPreprocessJob(editionId) {
  const r = await fetch(
    `/api/new-library/editions/${encodeURIComponent(editionId)}/preprocess-all`,
  );
  const d = await readJsonResponse(r);
  const job = d.job;
  if (job) renderPreprocessJobPanel(job);
  else hidePreprocessJobPanel();
  return job;
}

function _preprocessJobCardSig(job) {
  const vols = Object.values(job.volumes || {})
    .map((v) => `${v.volume_code}:${v.state}`)
    .join('|');
  return `${job.status || ''}:${job.current_index || 0}:${vols}`;
}

function startPreprocessJobPolling(editionId) {
  preprocessJobEditionId = editionId;
  stopPreprocessJobPolling();
  const tick = async () => {
    try {
      const job = await refreshPreprocessJob(editionId);
      if (!job || (job.status !== 'running' && job.status !== 'cancelling')) {
        stopPreprocessJobPolling();
        hidePreprocessJobPanel();
        preprocessJobCardSig = '';
        if (job && (job.status === 'done' || job.status === 'cancelled')) {
          toast(job.message || '本版预处理已结束');
          load();
        }
        return;
      }
      const sig = _preprocessJobCardSig(job);
      if (sig !== preprocessJobCardSig) {
        preprocessJobCardSig = sig;
        load();
      }
    } catch (e) { /* ignore poll errors */ }
  };
  tick();
  preprocessJobPollTimer = setInterval(tick, 3000);
}

async function preprocessEditionAll(editionId) {
  try {
    const existing = await refreshPreprocessJob(editionId);
    if (existing?.status === 'running' || existing?.status === 'cancelling') {
      toast(existing.status === 'cancelling'
        ? '本版预处理正在取消，请稍候…'
        : '本版预处理已在后台进行中');
      startPreprocessJobPolling(editionId);
      return { ok: true, already_running: true, job: existing };
    }
  } catch (e) { /* ignore */ }

  if (!confirm(
    '将对已上传 PDF、尚未完成预处理的册在后台依次执行：\n'
    + '识别目录 → 页码划分 → 生成页图 → 旧库粗分。\n'
    + '可新标签打开「进入本册建设」，离开本页也不中断。继续？',
  )) {
    return null;
  }

  const r = await fetch(
    `/api/new-library/editions/${encodeURIComponent(editionId)}/preprocess-all`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ only_pending: true }),
    },
  );
  const d = await readJsonResponse(r);
  const job = d.job;
  if (!job || job.status === 'idle') {
    toast(job?.message || d.message || '没有待预处理的册');
    return d;
  }
  toast(d.already_running
    ? '本版预处理已在后台进行中'
    : `本版预处理已启动（${job.volume_count || '?'} 册）`);
  renderPreprocessJobPanel(job);
  startPreprocessJobPolling(editionId);
  return d;
}

/* ── 本版 / 单册粗分下载 ── */

function filenameFromContentDisposition(header, fallback) {
  if (!header) return fallback;
  const m = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(header);
  const raw = decodeURIComponent((m && (m[1] || m[2])) || '').trim();
  return raw || fallback;
}

async function downloadBlobResponse(r, fallbackName) {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try {
      const d = await r.json();
      msg = d.error || msg;
    } catch (_) { /* ignore */ }
    throw new Error(msg);
  }
  const blob = await r.blob();
  const name = filenameFromContentDisposition(
    r.headers.get('Content-Disposition'),
    fallbackName,
  );
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function downloadVolumeCoarseXlsx(volumeCode) {
  const q = new URLSearchParams({
    include_matched: '1',
    include_unmatched: '1',
  });
  const r = await fetch(
    `/api/new-library/volumes/${encodeURIComponent(volumeCode)}/course-match/export.xlsx?${q}`,
  );
  await downloadBlobResponse(r, `粗分_${volumeCode}.xlsx`);
  toast('本册粗分结果已开始下载');
}

function syncCoarseExportDownloadBtn() {
  const btn = document.getElementById('ed-coarse-export-dl');
  if (!btn) return;
  const matched = document.getElementById('ed-coarse-include-matched')?.checked;
  const unmatched = document.getElementById('ed-coarse-include-unmatched')?.checked;
  const checked = document.querySelectorAll('#ed-coarse-export-rows input[type="checkbox"]:checked');
  btn.disabled = !(matched || unmatched) || checked.length === 0;
}

async function openCoarseExportPanel(editionId) {
  const panel = document.getElementById('ed-coarse-export-panel');
  const tbody = document.getElementById('ed-coarse-export-rows');
  const summary = document.getElementById('ed-coarse-export-summary');
  if (!panel || !tbody) return;
  placeCoarseExportPanel();
  panel.hidden = false;
  tbody.innerHTML = '<tr><td colspan="5" class="hint">加载册次状态…</td></tr>';
  syncCoarseExportDownloadBtn();
  try {
    const r = await fetch(
      `/api/new-library/editions/${encodeURIComponent(editionId)}/course-match/export-targets`,
    );
    const d = await readJsonResponse(r);
    const vols = d.volumes || [];
    const ready = vols.filter((v) => v.has_course_match);
    if (summary) {
      summary.textContent = ready.length
        ? `${ready.length}/${vols.length} 册已有粗分`
        : '本版尚无粗分结果';
    }
    if (!vols.length) {
      tbody.innerHTML = '<tr><td colspan="5" class="hint">本版无册次</td></tr>';
      syncCoarseExportDownloadBtn();
      return;
    }
    tbody.innerHTML = vols.map((v) => {
      const disabled = v.has_course_match ? '' : ' disabled';
      const checked = v.has_course_match ? ' checked' : '';
      const st = v.has_course_match
        ? (v.status === 'done' ? '已粗分' : (v.status || '有结果'))
        : '尚无粗分';
      return `<tr class="${v.has_course_match ? '' : 'is-muted'}">
        <td><input type="checkbox" data-code="${escapeHtml(v.volume_code)}"${checked}${disabled}></td>
        <td>${escapeHtml(v.label || v.volume_code)}</td>
        <td>${v.matched_count ?? 0}</td>
        <td>${v.unmatched_count ?? 0}</td>
        <td>${escapeHtml(st)}</td>
      </tr>`;
    }).join('');
    tbody.querySelectorAll('input[type="checkbox"]').forEach((el) => {
      el.addEventListener('change', syncCoarseExportDownloadBtn);
    });
    syncCoarseExportDownloadBtn();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5">${escapeHtml(e.message || String(e))}</td></tr>`;
    toast(e.message || String(e));
    syncCoarseExportDownloadBtn();
  }
}

async function downloadEditionCoarseXlsx() {
  if (!activeEditionId) throw new Error('请先选择版本');
  const matched = document.getElementById('ed-coarse-include-matched')?.checked;
  const unmatched = document.getElementById('ed-coarse-include-unmatched')?.checked;
  if (!matched && !unmatched) throw new Error('请至少勾选「已匹配」或「未匹配」');
  const codes = Array.from(
    document.querySelectorAll('#ed-coarse-export-rows input[type="checkbox"]:checked'),
  ).map((el) => el.dataset.code).filter(Boolean);
  if (!codes.length) throw new Error('请至少勾选一册');

  const btn = document.getElementById('ed-coarse-export-dl');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(
      `/api/new-library/editions/${encodeURIComponent(activeEditionId)}/course-match/export.xlsx`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          volume_codes: codes,
          include_matched: Boolean(matched),
          include_unmatched: Boolean(unmatched),
        }),
      },
    );
    await downloadBlobResponse(r, '本版粗分.xlsx');
    toast(`已开始下载（${codes.length} 册）`);
  } finally {
    syncCoarseExportDownloadBtn();
  }
}

function bindCoarseExportUi() {
  document.getElementById('ed-coarse-export-close')?.addEventListener('click', () => {
    const panel = document.getElementById('ed-coarse-export-panel');
    if (panel) panel.hidden = true;
  });
  document.getElementById('ed-coarse-export-dl')?.addEventListener('click', async () => {
    try {
      await downloadEditionCoarseXlsx();
    } catch (e) {
      toast(e.message || String(e));
    }
  });
  document.getElementById('ed-coarse-include-matched')?.addEventListener('change', syncCoarseExportDownloadBtn);
  document.getElementById('ed-coarse-include-unmatched')?.addEventListener('change', syncCoarseExportDownloadBtn);
  document.getElementById('ed-coarse-select-all')?.addEventListener('click', () => {
    document.querySelectorAll('#ed-coarse-export-rows input[type="checkbox"]:not(:disabled)').forEach((el) => {
      el.checked = true;
    });
    syncCoarseExportDownloadBtn();
  });
  document.getElementById('ed-coarse-select-none')?.addEventListener('click', () => {
    document.querySelectorAll('#ed-coarse-export-rows input[type="checkbox"]').forEach((el) => {
      el.checked = false;
    });
    syncCoarseExportDownloadBtn();
  });
}

async function load() {
  try {
    const r = await fetch('/api/new-library/editions');
    const d = await readJsonResponse(r);
    allEditions = d.editions || [];
    applyEditionFromUrl();
    renderEditions();
  } catch (e) {
    document.getElementById('editions').innerHTML =
      `<p class="wb-empty-msg">加载失败：${e.message || '请刷新重试'}</p>`;
  }
}

bindTbBatchUi();
bindCoarseExportUi();
load();
