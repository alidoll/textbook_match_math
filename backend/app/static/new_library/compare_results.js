const API = '/api/new-library';

function toast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = String(msg || '');
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 4500);
}

function escapeHtml(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function readParams() {
  const u = new URL(window.location.href);
  return {
    status: u.searchParams.get('status') || 'pending',
    volume: u.searchParams.get('volume') || '',
  };
}

function syncUrl(status, volume) {
  const u = new URL(window.location.href);
  if (status && status !== 'all') u.searchParams.set('status', status);
  else u.searchParams.delete('status');
  if (volume) u.searchParams.set('volume', volume);
  else u.searchParams.delete('volume');
  history.replaceState(null, '', u.pathname + u.search);
}

async function readJsonResponse(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`服务器返回异常（HTTP ${r.status}）`);
  }
}

const STATUS_LABEL = {
  pending: '待对比',
  confirmed: '已确认',
  no_blocks: '未建块',
};

function compareUrl(lessonUid, volumeCode) {
  const q = volumeCode ? `?volume=${encodeURIComponent(volumeCode)}` : '';
  return `/new-library/lessons/${encodeURIComponent(lessonUid)}/compare${q}`;
}

function parseOldLessonHint(hint) {
  const text = String(hint || '').trim();
  if (!text || text === '—') return { base: '—', ai: '' };
  const aiMatch = text.match(/（AI：([\s\S]+)）$/);
  if (aiMatch) {
    return { base: text.slice(0, aiMatch.index).trim(), ai: aiMatch[1].trim() };
  }
  if (text.startsWith('AI：')) return { base: '—', ai: text.slice(3).trim() };
  return { base: text, ai: '' };
}

function renderOldMatchCell(hint) {
  const { base, ai } = parseOldLessonHint(hint);
  if (!ai) return escapeHtml(base);
  const aiShort = ai.length > 52 ? `${ai.slice(0, 50)}…` : ai;
  return `<div class="cr-old-base" title="${escapeHtml(base)}">${escapeHtml(base)}</div>`
    + `<div class="cr-old-ai" title="AI：${escapeHtml(ai)}">AI：${escapeHtml(aiShort)}</div>`;
}

function renderLessonActions(les, volCode) {
  const compare = compareUrl(les.lesson_uid, volCode);
  const annotate = escapeHtml(les.annotate_url);
  if ((les.block_count || 0) <= 0 || les.compare_status === 'no_blocks') {
    return `<a class="cr-btn primary" href="${annotate}">去建块</a>`;
  }
  const editBlock = les.compare_status === 'confirmed'
    ? `<a class="cr-btn muted" href="${annotate}">查看建块</a>`
    : `<a class="cr-btn muted" href="${annotate}">编辑建块</a>`;
  return `<a class="cr-btn primary" href="${compare}">进入对比</a>${editBlock}`;
}

function renderSummary(summary) {
  const el = document.getElementById('cr-summary');
  if (!el || !summary) return;
  el.innerHTML = [
    `<strong>${summary.total || 0}</strong> 课可对照`,
    `待对比 ${summary.pending || 0}`,
    `已确认 ${summary.confirmed || 0}`,
    summary.no_blocks ? `未建块 ${summary.no_blocks}` : '',
  ].filter(Boolean).join(' · ');
}

function renderVolumeOptions(options, selected) {
  const sel = document.getElementById('filter-volume');
  if (!sel) return;
  const opts = ['<option value="">全部册次</option>'];
  (options || []).forEach((v) => {
    const code = v.volume_code;
    if (!code) return;
    const label = v.volume_title || code;
    const picked = code === selected ? ' selected' : '';
    opts.push(`<option value="${escapeHtml(code)}"${picked}>${escapeHtml(label)}</option>`);
  });
  sel.innerHTML = opts.join('');
}

function renderLessonCard(les, volCode) {
  const stLabel = STATUS_LABEL[les.compare_status] || les.compare_status;
  const stCls = les.compare_status || 'pending';
  const { base, ai } = parseOldLessonHint(les.old_lesson_hint);
  const compare = compareUrl(les.lesson_uid, volCode);
  const annotate = escapeHtml(les.annotate_url);
  const aiShort = ai && ai.length > 80 ? `${ai.slice(0, 78)}…` : ai;
  return `<article class="cr-lesson-card">
    <div class="cr-card-top">
      <div class="cr-card-title">${escapeHtml(les.lesson_label)}</div>
      <span class="cr-status ${stCls}">${escapeHtml(stLabel)}</span>
    </div>
    <div class="cr-card-old"><span class="cr-card-old-base">${escapeHtml(base)}</span></div>
    ${ai ? `<div class="cr-card-ai" title="${escapeHtml(ai)}">AI：${escapeHtml(aiShort)}</div>` : ''}
    <div class="cr-card-meta">${les.block_count || 0} 个新区块</div>
    <div class="cr-card-actions">
      ${les.block_count > 0
        ? `<a class="cr-btn primary" href="${compare}">进入对比 →</a><a class="cr-btn muted" href="${annotate}">编辑建块</a>`
        : `<a class="cr-btn primary" href="${annotate}">去建块 →</a>`}
    </div>
  </article>`;
}

function renderLessonTableRow(les, volCode) {
  const stLabel = STATUS_LABEL[les.compare_status] || les.compare_status;
  const stCls = les.compare_status || 'pending';
  return `<tr>
    <td>${escapeHtml(les.unit_title || '—')}</td>
    <td>${escapeHtml(les.lesson_label)}</td>
    <td>${renderOldMatchCell(les.old_lesson_hint)}</td>
    <td>${les.block_count || 0}</td>
    <td><span class="cr-status ${stCls}">${escapeHtml(stLabel)}</span></td>
    <td class="cr-actions">${renderLessonActions(les, volCode)}</td>
  </tr>`;
}

function renderVolumeLessons(vol) {
  const lessons = vol.lessons || [];
  const pending = lessons.filter((l) => l.compare_status === 'pending');
  const confirmed = lessons.filter((l) => l.compare_status === 'confirmed');
  const noBlocks = lessons.filter((l) => l.compare_status === 'no_blocks');
  const other = lessons.filter((l) => !['pending', 'confirmed', 'no_blocks'].includes(l.compare_status));

  let html = '';
  if (pending.length) {
    html += `<section class="cr-section">
      <h3 class="cr-section-head">待对比（${pending.length}）</h3>
      <div class="cr-card-grid">${pending.map((l) => renderLessonCard(l, vol.volume_code)).join('')}</div>
    </section>`;
  }
  const renderCompactTable = (title, rows) => {
    if (!rows.length) return '';
    return `<details class="cr-collapsible"${title.startsWith('已确认') ? '' : ''}>
      <summary>${title}（${rows.length}）</summary>
      <div class="table-wrap"><table class="cr-lesson-table">
        <thead><tr><th>单元</th><th>新课</th><th>旧课</th><th>区块</th><th>状态</th><th></th></tr></thead>
        <tbody>${rows.map((l) => renderLessonTableRow(l, vol.volume_code)).join('')}</tbody>
      </table></div>
    </details>`;
  };
  html += renderCompactTable('已确认', confirmed);
  html += renderCompactTable('未建块', noBlocks);
  html += renderCompactTable('其他', other);
  if (!html) html = '<p class="cr-empty">当前筛选下本册无课时。</p>';
  return html;
}

function renderVolumes(volumes) {
  const root = document.getElementById('cr-volumes');
  if (!root) return;
  if (!volumes?.length) {
    root.innerHTML = `<div class="card cr-empty">暂无可对比课时。请先在 <a href="/new-library/">新库建设</a> 完成粗分、建块后再来。</div>`;
    return;
  }

  root.innerHTML = volumes.map((vol) => {
    const st = vol.stats || {};
    return `<article class="card cr-vol-card" data-volume="${escapeHtml(vol.volume_code)}">
      <div class="cr-vol-head">
        <h2>${escapeHtml(vol.volume_title || vol.volume_code)}</h2>
        <div class="cr-vol-stats">
          共 ${st.total || 0} 课
          <span class="pill pending">待对比 ${st.pending || 0}</span>
          <span class="pill confirmed">已确认 ${st.confirmed || 0}</span>
          ${st.no_blocks ? `<span class="pill muted">未建块 ${st.no_blocks}</span>` : ''}
        </div>
      </div>
      ${renderVolumeLessons(vol)}
    </article>`;
  }).join('');
}

async function loadWorkbench() {
  const params = readParams();
  const statusSel = document.getElementById('filter-status');
  const volumeSel = document.getElementById('filter-volume');
  if (statusSel) statusSel.value = params.status;
  if (volumeSel) volumeSel.value = params.volume;

  const qs = new URLSearchParams();
  if (params.status) qs.set('status', params.status);
  if (params.volume) qs.set('volume', params.volume);

  const r = await fetch(`${API}/compare-results?${qs}`);
  const data = await readJsonResponse(r);
  if (!r.ok || !data.ok) throw new Error(data.error || '加载失败');

  renderSummary(data.summary);
  renderVolumeOptions(data.volume_options || data.volumes || [], params.volume);
  renderVolumes(data.volumes || []);
}

function bindFilters() {
  const statusSel = document.getElementById('filter-status');
  const volumeSel = document.getElementById('filter-volume');
  const reload = () => {
    const status = statusSel?.value || 'all';
    const volume = volumeSel?.value || '';
    syncUrl(status, volume);
    loadWorkbench().catch((e) => toast(e.message));
  };
  statusSel?.addEventListener('change', reload);
  volumeSel?.addEventListener('change', reload);
}

bindFilters();
loadWorkbench().catch((e) => {
  const root = document.getElementById('cr-volumes');
  if (root) root.innerHTML = `<p class="cr-empty">${escapeHtml(e.message)}</p>`;
});
