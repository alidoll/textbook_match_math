const VOLUME_CODE = window.INTAKE_VOLUME_CODE;
const API_PREFIX = window.INTAKE_API_PREFIX || '/api/new-library';

const INTAKE_FOCUS_KEY = 'new-library-intake-focus';
const INTAKE_RELOAD_CHANNEL = 'new-library-intake-reload';

/** @type {BroadcastChannel | null} */
let intakeReloadChannel = null;
try {
  intakeReloadChannel = new BroadcastChannel(INTAKE_RELOAD_CHANNEL);
  intakeReloadChannel.onmessage = (ev) => {
    if (ev?.data?.type === 'reload-volume') loadVolume().catch(() => {});
  };
} catch {
  intakeReloadChannel = null;
}

let lastVolumeData = null;
let previewOpenLessonUid = null;
let previewRefreshTimer = null;
let previewBackdropScrollY = 0;
let courseByUid = {};

const PDF_UPLOAD_FLASH_MS = 15000;

/** @type {{ name: string } | null} */
let pdfPending = null;
/** @type {ReturnType<typeof setTimeout> | null} */
let pdfUploadFlashTimer = null;
/** @type {ReturnType<typeof setInterval> | null} */
let parseHintTimer = null;
/** 避免 loadVolume 与按钮重复轮询 */
let parsePollActive = false;

const PARSE_BUSY_HINTS = [
  '划分页码已在后台运行，约 2～5 分钟…',
  '正在推算目录偏移 x 并划分各课起止页…',
  '正在修剪单元扉页/小结…',
  '不生成页图，预览走 PDF 直链；页图请用下方「一键生成页图」',
  '请勿重启后端或保存代码触发热重载…',
];

/** @type {'catalog'|'parse'|'build-pages'|'course'|'preprocess'|null} */
let intakeLocalBusy = null;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/"/g, '&quot;');
}

function shortFileName(name, max = 28) {
  return name.length > max ? `${name.slice(0, max - 1)}…` : name;
}

function hintWithDot(className, text) {
  return (
    `<span class="${className}">` +
    `<span class="cw-upload-dot" aria-hidden="true"></span>` +
    `${text}</span>`
  );
}

function setPdfUploadHint(html) {
  const el = document.getElementById('pdf-upload-hint');
  if (!el) return;
  if (!html) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  el.innerHTML = html;
}

function refreshPdfUploadUi() {
  const btn = document.getElementById('upload-btn');
  if (!btn) return;
  const hasPdf = !!lastVolumeData?.has_pdf;

  if (pdfPending) {
    setPdfUploadHint(
      hintWithDot(
        'cw-zip-pending',
        `已加载「${escapeHtml(shortFileName(pdfPending.name))}」，请点击确认上传`,
      ),
    );
    btn.classList.remove('pdf-upload-btn-idle', 'pdf-upload-btn-done');
    btn.classList.add('pdf-upload-btn-ready');
    btn.disabled = false;
    btn.textContent = '确认上传';
    return;
  }

  if (pdfUploadFlashTimer) return;

  setPdfUploadHint('');
  btn.classList.remove('pdf-upload-btn-ready', 'pdf-upload-btn-idle', 'pdf-upload-btn-done');
  if (hasPdf) {
    btn.classList.add('pdf-upload-btn-done');
    btn.disabled = true;
    btn.textContent = '已上传';
  } else {
    btn.classList.add('pdf-upload-btn-idle');
    btn.disabled = true;
    btn.textContent = '确认上传';
  }
}

const INTAKE_STEP_LABELS = {
  catalog: { idle: '识别目录', done: '已识别', rerun: '重新识别目录', busy: '识别中…' },
  parse: { idle: '划分页码', done: '已划分', rerun: '重新划分页码', busy: '划分中…' },
  buildPages: { idle: '一键生成页图', done: '已生成', rerun: '重新生成页图', busy: '生成中…' },
  course: { idle: '运行粗分', done: '已粗分', rerun: '重新粗分', busy: '粗分中…' },
  preprocess: { idle: '开始预处理', done: '预处理完成', rerun: '重新预处理', busy: '预处理中…' },
};

const PREPROCESS_SEGS = [
  { key: 'catalog', name: '目录' },
  { key: 'split', name: '划分' },
  { key: 'pages', name: '页图' },
  { key: 'coarse', name: '粗分' },
];

const ANALYSIS_SEGS = [
  { key: 'text_ocr', name: '文字OCR' },
  { key: 'image_ocr', name: '图片OCR' },
  { key: 'curate', name: '整理原子' },
  { key: 'block_match', name: '栏目·块名' },
  { key: 'prescan', name: '对照预判' },
  { key: 'pair_review', name: '确认对照' },
];

const BLOCK_SEGS = [
  { key: 'column_detect', name: '认栏目头' },
  { key: 'topic_cluster', name: '原子分组' },
  { key: 'balance', name: '合并碎块' },
  { key: 'attributes', name: '对照建块' },
  { key: 'anchor', name: '挂旧块锚' },
  { key: 'validate', name: '检查锚定' },
  { key: 'edit_ready', name: '待核对' },
];

/** @type {'catalog'|'split'|'pages'|'coarse'|null} */
let preprocessActiveStep = null;
let analysisBatchPollTimer = null;
let analysisBatchRunning = false;
let blockBatchPollTimer = null;
let blockBatchRunning = false;
/** @type {Set<string>} */
const lessonAnalysisBusy = new Set();

function segStatusClass(status) {
  if (status === 'running') return 'is-running';
  if (status === 'done') return 'is-done';
  if (status === 'skip') return 'is-skip';
  if (status === 'error') return 'is-error';
  return 'is-pending';
}

function segColumn(key, name, status) {
  const cls = segStatusClass(status);
  return (
    `<div class="block-batch-segcol">` +
    `<span class="block-batch-seg seg-${key} ${cls}"></span>` +
    `<span class="block-batch-seg-name ${cls}">${escapeHtml(name)}</span>` +
    `</div>`
  );
}

function segTrackHtml(segments, steps, label, stateClass, labelHtml) {
  const cols = steps.map((s) => segColumn(s.key, s.name, segments[s.key] || 'pending')).join('');
  const labelPart = labelHtml != null
    ? labelHtml
    : (label ? `<span class="cw-batch-progress-label ${stateClass || ''}">${escapeHtml(label)}</span>` : '');
  return (
    `<div class="block-batch-progress-row">` +
    `<div class="block-batch-segwrap" role="progressbar">` +
    `<div class="block-batch-segtrack">${cols}</div>` +
    `</div>` +
    labelPart +
    `</div>`
  );
}

/** @type {Set<string>} */
const expandedLessonUids = new Set();

function segTrackHtmlExpanded(segments, steps, label, stateClass, labelHtml) {
  const inner = segTrackHtml(segments, steps, label, stateClass, labelHtml);
  return inner.replace('block-batch-progress-row', 'block-batch-progress-row lesson-expanded-progress');
}

function extractAiSummaryFromHint(hint) {
  if (!hint) return '';
  const text = String(hint);
  const aiMatch = text.match(/（AI：([\s\S]+)）$/);
  if (aiMatch) return aiMatch[1].trim();
  if (text.startsWith('AI：')) return text.slice(3).trim();
  return '';
}

function analysisBatchStatusVariant(les) {
  const batch = lessonBatchProgress(les.lesson_uid);
  const a = les.analysis || {};
  if (lessonAnalysisBusy.has(les.lesson_uid)) return 'running';
  if (batch?.state === 'running') return 'running';
  if (a.stage === 'running' && analysisBatchRunning) return 'running';
  if (batch?.state === 'queued') return 'queued';
  return null;
}

function mergeVolumeAnalysisFromPoll(d) {
  if (!lastVolumeData) {
    lastVolumeData = d;
    return;
  }
  lastVolumeData.analysis_batch = d.analysis_batch;
  lastVolumeData.analysis_summary = d.analysis_summary;
  const byUid = Object.fromEntries((d.lessons || []).map((l) => [l.lesson_uid, l]));
  lastVolumeData.lessons = (lastVolumeData.lessons || []).map((les) => {
    const fresh = byUid[les.lesson_uid];
    if (!fresh) return les;
    return { ...les, analysis: fresh.analysis };
  });
}

function mergeVolumeBlockBatchFromPoll(d) {
  if (!lastVolumeData) {
    lastVolumeData = d;
    return;
  }
  lastVolumeData.block_batch = d.block_batch;
  const byUid = Object.fromEntries((d.lessons || []).map((l) => [l.lesson_uid, l]));
  lastVolumeData.lessons = (lastVolumeData.lessons || []).map((les) => {
    const fresh = byUid[les.lesson_uid];
    if (!fresh) return les;
    return {
      ...les,
      block_count: fresh.block_count,
      anchored_block_count: fresh.anchored_block_count,
    };
  });
}

function lessonBlockBatchProgress(lessonUid) {
  return lastVolumeData?.block_batch?.lessons?.[lessonUid] || null;
}

function blockBatchStatusVariant(les) {
  const batch = lessonBlockBatchProgress(les.lesson_uid);
  if (!batch) return null;
  if (batch.state === 'running') return 'running';
  if (batch.state === 'queued') return 'queued';
  if (batch.state === 'error') return 'error';
  return null;
}

function patchLessonBlockCells() {
  const tbody = document.getElementById('lesson-rows');
  if (!tbody || !lastVolumeData?.lessons) return;
  for (const les of lastVolumeData.lessons) {
    const tr = tbody.querySelector(`tr.lesson-row-compact[data-lesson-row="${les.lesson_uid}"]`);
    if (!tr) continue;
    const batchVariant = blockBatchStatusVariant(les);
    const cell = tr.querySelector('td.annotate-cell--compact');
    if (!cell) continue;
    cell.className = `annotate-cell annotate-cell--compact${batchVariant ? ` annotate-cell--batch-${batchVariant}` : ''}`;
    cell.innerHTML = renderCompactBlockCell(les);
    const detailRow = tbody.querySelector(`tr.lesson-row-detail[data-lesson-detail="${les.lesson_uid}"]`);
    if (detailRow && expandedLessonUids.has(les.lesson_uid)) {
      detailRow.querySelector('td').innerHTML = renderExpandedPanel(les);
      bindLessonTableEvents(tbody);
    }
  }
}

function patchLessonAnalysisCells() {
  const tbody = document.getElementById('lesson-rows');
  if (!tbody || !lastVolumeData?.lessons) return;
  for (const les of lastVolumeData.lessons) {
    const tr = tbody.querySelector(`tr.lesson-row-compact[data-lesson-row="${les.lesson_uid}"]`);
    if (!tr) continue;
    const batchVariant = analysisBatchStatusVariant(les);
    const cell = tr.querySelector('td.analysis-cell--compact');
    if (!cell) continue;
    cell.className = `analysis-cell analysis-cell--compact${batchVariant ? ` analysis-cell--batch-${batchVariant}` : ''}`;
    cell.innerHTML = renderCompactAnalysisCell(les);
  }
}

function lessonBatchRowClass(_les) {
  return '';
}

function renderCompactAnalysisCell(les) {
  const a = les.analysis || {};
  const batch = lessonBatchProgress(les.lesson_uid);
  const batchVariant = analysisBatchStatusVariant(les);
  let label = a.label || '待分析';
  if (a.stage === 'no_pages') label = '无页图';
  if (courseByUid[les.lesson_uid]?.build_hint === 'fully_new' && a.stage === 'ready') {
    label = '完全新制';
  }
  if (batchVariant === 'running') {
    label = (batch?.label || a.label || '分析中').replace(/…+$/, '');
    if (a.progress && !label.includes(a.progress)) {
      label = `${label} · ${a.progress}`;
    }
  } else if (batchVariant === 'queued') {
    label = (batch?.label || '排队中').replace(/…+$/, '');
  }
  const review = analysisReviewLabelHtml(les);
  let cls = 'lesson-compact-badge';
  if (batchVariant === 'running') cls += ' lesson-compact-status--batch-running';
  else if (batchVariant === 'queued') cls += ' lesson-compact-status--batch-queued';
  else if (a.stage === 'ready') cls += ' lesson-compact-status--ok';
  else if (a.stage === 'await_confirm' || a.stage === 'needs_review') cls += ' lesson-compact-status--warn';
  const liveDot = batchVariant === 'running'
    ? '<span class="lesson-batch-live-dot" aria-hidden="true"></span>'
    : '';
  const queueMark = batchVariant === 'queued'
    ? '<span class="lesson-batch-queue-mark" aria-hidden="true">⏳</span>'
    : '';
  const ellipsis = batchVariant === 'running'
    ? '<span class="lesson-status-ellipsis" aria-hidden="true"></span>'
    : '';
  return (
    `<span class="lesson-compact-status ${cls}">` +
    `${liveDot}${queueMark}` +
    `<span class="lesson-compact-status-text">${escapeHtml(label)}${ellipsis}</span>` +
    `</span>` +
    (review ? `<div class="lesson-compact-review">${review}</div>` : '')
  );
}

function renderCompactBlockCell(les) {
  const batch = lessonBlockBatchProgress(les.lesson_uid);
  const batchVariant = blockBatchStatusVariant(les);
  let label = deriveBlockAnchorLabel(les);
  if (batchVariant === 'running') {
    label = (batch?.label || label || '建块中').replace(/…+$/, '');
  } else if (batchVariant === 'queued') {
    label = (batch?.label || '排队中').replace(/…+$/, '');
  } else if (batchVariant === 'error') {
    label = batch?.label || '建块失败';
  }
  const bc = les.block_count || 0;
  const ac = les.anchored_block_count || 0;
  let cls = 'lesson-compact-status';
  if (batchVariant === 'running') cls += ' lesson-compact-status--batch-running';
  else if (batchVariant === 'queued') cls += ' lesson-compact-status--batch-queued';
  else if (batchVariant === 'error') cls += ' lesson-compact-status--error';
  else if (bc > 0 && ac >= bc) cls += ' lesson-compact-status--ok';
  const liveDot = batchVariant === 'running'
    ? '<span class="lesson-batch-live-dot" aria-hidden="true"></span>'
    : '';
  const queueMark = batchVariant === 'queued'
    ? '<span class="lesson-batch-queue-mark" aria-hidden="true">⏳</span>'
    : '';
  const ellipsis = batchVariant === 'running'
    ? '<span class="lesson-status-ellipsis" aria-hidden="true"></span>'
    : '';
  return (
    `<span class="${cls}">` +
    `${liveDot}${queueMark}` +
    `<span class="lesson-compact-status-text">${escapeHtml(label)}${ellipsis}</span>` +
    `</span>`
  );
}

function shortOldLessonHint(hint) {
  const text = String(hint || '').trim();
  const aiIdx = text.search(/（AI：/);
  const base = (aiIdx >= 0 ? text.slice(0, aiIdx) : text).trim();
  if (base.length <= 26) return base;
  return `${base.slice(0, 24)}…`;
}

function oldCandidateSubline(cand) {
  const vol = (cand.old_volume_code || '').trim();
  const pages = cand.old_page_count ? `${cand.old_page_count} 页` : '';
  return [vol, pages].filter(Boolean).join(' · ') || '—';
}

function renderOldCandidateCard(cand, rank) {
  const medal = rank === 1 ? '🥇' : rank === 2 ? '🥈' : rank === 3 ? '🥉' : `${rank}.`;
  const pct = cand.similarity_score != null
    ? `${(cand.similarity_score * 100).toFixed(0)}%`
    : '—';
  const title = shortOldLessonHint(cand.old_lesson_hint);
  const sub = oldCandidateSubline(cand);
  const preview = cand.old_lesson_uid
    ? `<a class="btn btn-sm secondary old-candidate-preview" href="/old-library/lessons/${encodeURIComponent(cand.old_lesson_uid)}/annotate" target="_blank" rel="noopener">预览</a>`
    : '';
  return (
    `<div class="old-candidate-card${rank === 1 ? ' is-primary' : ''}">` +
    `<div class="old-candidate-row1">` +
    `<span class="old-candidate-medal">${medal}</span>` +
    `<span class="old-candidate-title" title="${escapeHtml(cand.old_lesson_hint || '')}">${escapeHtml(title)}</span>` +
    `<span class="old-candidate-pct">${escapeHtml(pct)}</span>` +
    `</div>` +
    `<div class="old-candidate-row2">` +
    `<span class="old-candidate-sub">${escapeHtml(sub)}</span>${preview}` +
    `</div>` +
    `</div>`
  );
}

function renderExpandedLeft(les) {
  const m = courseByUid[les.lesson_uid];
  const title = `${les.lesson_no} ${les.lesson_name}`.trim();
  let coarseHtml = courseTierBadge('pending', '待粗分');
  let scoreLine = '';
  if (m) {
    if (m.build_hint === 'fully_new') {
      coarseHtml = '<span class="match-fully-new-badge">完全新制</span>';
    } else {
      coarseHtml = courseTierBadge(m.match_tier, m.match_label);
      if (m.similarity_score != null) {
        scoreLine = `<div class="lesson-expanded-score-line">参考 ${(m.similarity_score * 100).toFixed(1)}% 相似</div>`;
      }
    }
  }
  const candidates = (m?.candidates || [])
    .filter((c) => c.old_lesson_hint && c.match_tier !== 'traceability')
    .slice(0, 3);
  const listHtml = candidates.length
    ? candidates.map((c, i) => renderOldCandidateCard(c, i + 1)).join('')
    : '<p class="hint lesson-expanded-empty">尚无旧库对照候选</p>';
  const aiFromCoarse = extractAiSummaryFromHint(m?.old_lesson_hint);
  const aiFromPrescan = (les.analysis?.prescan_summary || '').trim();
  const aiText = aiFromPrescan || aiFromCoarse;
  const aiHtml = aiText
    ? `<p class="lesson-expanded-ai lesson-expanded-ai--compact">${escapeHtml(aiText)}</p>`
    : '';
  return (
    `<div class="lesson-expanded-col-inner">` +
    `<div class="lesson-expanded-headline">` +
    `<h3 class="lesson-expanded-lesson-title">${escapeHtml(title)}</h3>${coarseHtml}` +
    `</div>` +
    scoreLine +
    `<section class="lesson-expanded-section"><h4 class="lesson-expanded-subtitle">旧库对照</h4>${listHtml}</section>` +
    aiHtml +
    `</div>`
  );
}

function renderAnalysisSummary(les) {
  const m = courseByUid[les.lesson_uid];
  const a = les.analysis || {};
  const matchPct = m?.similarity_score != null
    ? `${(m.similarity_score * 100).toFixed(1)}%`
    : null;
  const bodyPct = a.lesson_body_similarity != null && !Number.isNaN(Number(a.lesson_body_similarity))
    ? `${(Number(a.lesson_body_similarity) * 100).toFixed(1)}%`
    : null;
  const verdict = (a.prescan_pair_label || a.prescan_agreement_label || '').trim();
  const summary = (a.prescan_summary || '').trim();
  const parts = [];
  if (matchPct) {
    parts.push(
      `<div class="analysis-stat">` +
      `<span class="analysis-stat-val">${escapeHtml(matchPct)}</span>` +
      `<span class="analysis-stat-lbl">整体匹配度</span></div>`,
    );
  }
  if (bodyPct) {
    parts.push(
      `<div class="analysis-stat">` +
      `<span class="analysis-stat-val">${escapeHtml(bodyPct)}</span>` +
      `<span class="analysis-stat-lbl">正文相似</span></div>`,
    );
  } else if (verdict) {
    parts.push(
      `<div class="analysis-stat analysis-stat--verdict">` +
      `<span class="analysis-stat-val analysis-stat-val--sm">${escapeHtml(verdict)}</span>` +
      `<span class="analysis-stat-lbl">预判结论</span></div>`,
    );
  }
  const statsHtml = parts.length
    ? `<div class="analysis-summary-stats">${parts.join('')}</div>`
    : '';
  let bodyHtml = '';
  if (summary) {
    bodyHtml = `<p class="analysis-summary-text">${escapeHtml(summary)}</p>`;
  } else if (!statsHtml && a.stage !== 'ready' && a.stage !== 'await_confirm' && a.stage !== 'needs_review') {
    bodyHtml = '<p class="hint lesson-expanded-empty">完成对照分析后显示摘要</p>';
  }
  if (!statsHtml && !bodyHtml) return '';
  return (
    `<section class="analysis-summary-card">` +
    `<h5 class="analysis-summary-title">分析结果</h5>` +
    statsHtml +
    bodyHtml +
    `</section>`
  );
}

function renderAnalysisMetrics(les) {
  return renderAnalysisSummary(les);
}

function renderExpandedMiddle(les) {
  const a = les.analysis || {};
  const segs = normalizeAnalysisSegments(a);
  let label = a.label || '待分析';
  if (a.stage === 'no_pages') label = '无页图';
  const stateClass = a.stage === 'failed' ? 'cw-batch-progress--error'
    : (a.stage === 'running' ? 'cw-batch-progress--running' : '');
  if (courseByUid[les.lesson_uid]?.build_hint === 'fully_new' && a.stage === 'ready') {
    label = '完全新制';
  }
  const reviewLabelHtml = analysisReviewLabelHtml(les);
  const hasPages = (les.lesson_page_count || 0) > 0;
  const busy = lessonAnalysisBusy.has(les.lesson_uid);
  const inBatch = isLessonInBatchQueue(les.lesson_uid);
  const btnLabel = busy ? '分析中…' : (a.stage === 'ready' ? '重新分析' : '对照分析');
  const canAnalyze = hasPages && !inBatch && !busy;
  const annotateHref = `/new-library/lessons/${encodeURIComponent(les.lesson_uid)}/annotate`;
  const progressExtra = a.progress ? ` · ${escapeHtml(a.progress)}` : '';
  return (
    `<div class="lesson-expanded-col-inner">` +
    `<h4 class="lesson-expanded-col-title">对照分析</h4>` +
    segTrackHtmlExpanded(segs, ANALYSIS_SEGS, `${label}${progressExtra}`, stateClass, reviewLabelHtml) +
    renderAnalysisSummary(les) +
    `<div class="lesson-expanded-actions">` +
    `<button type="button" class="btn btn-sm secondary btn-analysis-lesson" data-lesson-analysis="${les.lesson_uid}"` +
    ` ${canAnalyze ? '' : 'disabled'}>${escapeHtml(btnLabel)}</button>` +
    `<a class="btn btn-sm btn-annotate" href="${annotateHref}" target="_blank" rel="noopener">进入详情 →</a>` +
    `</div>` +
    `</div>`
  );
}

function renderExpandedRight(les) {
  const m = courseByUid[les.lesson_uid];
  const segs = deriveBlockAnchorSegments(les);
  const label = deriveBlockAnchorLabel(les);
  const fullyNewBadge = m?.build_hint === 'fully_new'
    ? '<span class="match-fully-new-badge">完全新制</span>'
    : '';
  const bc = les.block_count || 0;
  const ac = les.anchored_block_count || 0;
  const href = `/new-library/lessons/${encodeURIComponent(les.lesson_uid)}/annotate`;
  const resultHtml = bc > 0
    ? `<p class="lesson-block-result">共 <strong>${bc}</strong> 个区块 · 已锚定 <strong>${ac}/${bc}</strong></p>`
    : '<p class="hint lesson-expanded-empty">尚未建块，进入详情页对照旧块创建</p>';
  return (
    `<div class="lesson-expanded-col-inner">` +
    `<h4 class="lesson-expanded-col-title">建块与锚定</h4>` +
    `${fullyNewBadge}` +
    segTrackHtmlExpanded(segs, BLOCK_SEGS, label, '') +
    resultHtml +
    `<div class="lesson-expanded-actions lesson-expanded-actions--stack">` +
    `<a class="btn btn-sm secondary" href="${href}" target="_blank" rel="noopener">重新建块</a>` +
    `<a class="btn btn-sm btn-annotate" href="${href}" target="_blank" rel="noopener">进入编辑 →</a>` +
    `</div>` +
    `</div>`
  );
}

function renderExpandedPanel(les) {
  return (
    `<div class="lesson-expanded-panel">` +
    `<div class="lesson-expanded-col lesson-expanded-col--left">${renderExpandedLeft(les)}</div>` +
    `<div class="lesson-expanded-col lesson-expanded-col--mid">${renderExpandedMiddle(les)}</div>` +
    `<div class="lesson-expanded-col lesson-expanded-col--right">${renderExpandedRight(les)}</div>` +
    `</div>`
  );
}

function toggleLessonExpand(lessonUid) {
  if (expandedLessonUids.has(lessonUid)) expandedLessonUids.delete(lessonUid);
  else expandedLessonUids.add(lessonUid);
  if (lastVolumeData) renderVolume(lastVolumeData);
}

function bindLessonTableEvents(tbody) {
  tbody.querySelectorAll('.lesson-expand-btn').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      toggleLessonExpand(btn.dataset.lessonExpand);
    };
  });
  tbody.querySelectorAll('.page-preview-btn').forEach((btn) => {
    btn.onclick = () => openLessonPreview(btn.dataset.lesson);
  });
  tbody.querySelectorAll('.lesson-move-btn').forEach((btn) => {
    btn.onclick = () => moveLesson(btn.dataset.lesson, btn.dataset.dir);
  });
  tbody.querySelectorAll('.page-save-btn').forEach((btn) => {
    btn.onclick = () => saveLessonPageRange(btn.dataset.lesson);
  });
  tbody.querySelectorAll('.page-start-input, .page-end-input').forEach((input) => {
    const lessonUid = input.dataset.lesson;
    input.addEventListener('input', () => {
      markPageRangeDirty(lessonUid, true);
      if (previewOpenLessonUid === lessonUid) {
        clearTimeout(previewRefreshTimer);
        previewRefreshTimer = setTimeout(() => openLessonPreview(lessonUid), 350);
      }
    });
  });
  tbody.querySelectorAll('[data-lesson-analysis]').forEach((btn) => {
    btn.onclick = () => startLessonAnalysisForLesson(btn.dataset.lessonAnalysis);
  });
}

function lessonReviewFlagHtml(les) {
  const a = les.analysis || {};
  if (a.stage === 'await_confirm') {
    return (
      '<span class="lesson-row-flag" title="对照分析已完成，需进入 Annotate 确认旧课对应">待确认</span>'
    );
  }
  if (a.stage === 'needs_review') {
    const tip = escapeHtml(a.label || '对照预判存疑，请人工复核');
    return (
      `<span class="lesson-row-flag lesson-row-flag--review" title="${tip}">需复核</span>`
    );
  }
  return '';
}

function analysisReviewLabelHtml(les) {
  const a = les.analysis || {};
  const annotateHref = `/new-library/lessons/${encodeURIComponent(les.lesson_uid)}/annotate`;
  if (a.stage === 'await_confirm') {
    return (
      `<div class="analysis-review-label-row">` +
      `<span class="analysis-review-badge analysis-review-badge--confirm">待确认对照</span>` +
      `<a class="analysis-review-link" href="${annotateHref}" target="_blank" rel="noopener">去确认 →</a>` +
      `</div>`
    );
  }
  if (a.stage === 'needs_review') {
    const label = escapeHtml(a.label || '建议复核');
    return (
      `<div class="analysis-review-label-row">` +
      `<span class="analysis-review-badge analysis-review-badge--review">${label}</span>` +
      `<a class="analysis-review-link" href="${annotateHref}" target="_blank" rel="noopener">去确认 →</a>` +
      `</div>`
    );
  }
  return null;
}

function derivePreprocessSegments(data) {
  const segs = {
    catalog: isCatalogDone(data) ? 'done' : 'pending',
    split: isParseDone(data) ? 'done' : 'pending',
    pages: isBuildPagesDone(data) ? 'done' : 'pending',
    coarse: isCourseDone(data) ? 'done' : 'pending',
  };
  if (data?.parse_status === 'processing') segs.split = 'running';
  if (preprocessActiveStep) segs[preprocessActiveStep] = 'running';
  return segs;
}

function derivePreprocessLabel(data) {
  if (preprocessActiveStep === 'catalog') return '识别目录中…';
  if (preprocessActiveStep === 'split') return '划分页码中…';
  if (preprocessActiveStep === 'pages') return '生成页图中…';
  if (preprocessActiveStep === 'coarse') return '粗分对照中…';
  if (isCourseDone(data)) return '预处理已完成';
  if (isBuildPagesDone(data) && !isCourseDone(data)) return '页图已就绪，待粗分';
  if (isParseDone(data) && !isBuildPagesDone(data)) return '页码已划分，待生成页图';
  if (isCatalogDone(data) && !isParseDone(data)) return '目录已识别，待划分页码';
  if (data?.has_pdf) return '待开始预处理';
  return '';
}

function renderPreprocessCard(data) {
  const meta = document.getElementById('preprocess-meta');
  const btn = document.getElementById('preprocess-btn');
  if (!meta || !btn) return;
  const segs = derivePreprocessSegments(data);
  const label = derivePreprocessLabel(data);
  const anyDone = Object.values(segs).some((s) => s === 'done' || s === 'running');
  meta.hidden = !anyDone && !preprocessActiveStep;
  if (!meta.hidden) {
    meta.innerHTML = segTrackHtml(segs, PREPROCESS_SEGS, label, '');
  }
  const allDone = isCourseDone(data);
  const busy = !!preprocessActiveStep || intakeLocalBusy === 'preprocess'
    || data?.parse_status === 'processing';
  applyIntakeStepButton('preprocess-btn', {
    key: 'preprocess',
    done: allDone,
    enabled: !!data?.has_pdf,
    busy,
    busyText: '预处理中…',
  });
}

function deriveBlockAnchorSegments(les) {
  const batch = lessonBlockBatchProgress(les.lesson_uid);
  const fullyNew = courseByUid[les.lesson_uid]?.build_hint === 'fully_new';
  const segs = {
    column_detect: 'pending',
    topic_cluster: 'pending',
    balance: 'pending',
    attributes: 'pending',
    anchor: 'pending',
    validate: 'pending',
    edit_ready: 'pending',
  };
  if (fullyNew) {
    segs.column_detect = 'skip';
    segs.topic_cluster = 'skip';
    segs.balance = 'skip';
    segs.attributes = 'skip';
    segs.anchor = 'skip';
    return segs;
  }
  if (batch?.segments) {
    for (const k of Object.keys(segs)) {
      if (batch.segments[k]) segs[k] = batch.segments[k];
    }
    return segs;
  }
  const apiSegs = les.block_pipeline?.segments;
  if (apiSegs) {
    for (const k of Object.keys(segs)) {
      if (apiSegs[k]) segs[k] = apiSegs[k];
    }
  }
  return segs;
}

function deriveBlockAnchorLabel(les) {
  const batch = lessonBlockBatchProgress(les.lesson_uid);
  const bc = les.block_count || 0;
  const ac = les.anchored_block_count || 0;
  const fullyNew = courseByUid[les.lesson_uid]?.build_hint === 'fully_new';
  if (fullyNew) return '完全新制';
  if (batch?.state === 'running') return batch.label || '建块中…';
  if (batch?.state === 'queued') return batch.label || '排队中…';
  if (batch?.state === 'error') return batch.label || '建块失败';
  if (bc > 0 && ac >= bc) return `锚定 ${ac}/${bc}`;
  if (bc > 0) return `${bc} 区块 · 锚定 ${ac}/${bc}`;
  return '待建块';
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

function lessonBatchProgress(lessonUid) {
  return lastVolumeData?.analysis_batch?.lessons?.[lessonUid] || null;
}

function isLessonInBatchQueue(lessonUid) {
  const prog = lessonBatchProgress(lessonUid);
  return prog && (prog.state === 'queued' || prog.state === 'running');
}

function renderAnalysisCell(les) {
  const a = les.analysis || {};
  const segs = normalizeAnalysisSegments(a);
  let label = a.label || '待分析';
  if (a.stage === 'no_pages') label = '无页图';
  const stateClass = a.stage === 'failed' ? 'cw-batch-progress--error'
    : (a.stage === 'running' ? 'cw-batch-progress--running' : '');
  if (courseByUid[les.lesson_uid]?.build_hint === 'fully_new' && a.stage === 'ready') {
    label = '完全新制';
  }
  const reviewLabelHtml = analysisReviewLabelHtml(les);
  const hasPages = (les.lesson_page_count || 0) > 0;
  const busy = lessonAnalysisBusy.has(les.lesson_uid);
  const inBatch = isLessonInBatchQueue(les.lesson_uid);
  const btnLabel = busy ? '分析中…' : (a.stage === 'ready' ? '重新分析' : '对照分析');
  const canAnalyze = hasPages && !inBatch && !busy;
  const btnTitle = !hasPages
    ? '请先生成页图'
    : inBatch
      ? '该课在批量队列中，请先取消批量或等待'
      : '对本课运行对照分析（OCR → 整理原子 → 栏目·块名 → 对照预判）';
  return (
    segTrackHtml(segs, ANALYSIS_SEGS, label, stateClass, reviewLabelHtml) +
    `<div class="annotate-actions">` +
    `<button type="button" class="btn btn-sm btn-annotate btn-analysis-lesson" data-lesson-analysis="${les.lesson_uid}"` +
    ` ${canAnalyze ? '' : 'disabled'} title="${escapeHtml(btnTitle)}">${escapeHtml(btnLabel)}</button>` +
    `</div>`
  );
}

function renderBlockAnchorCell(les) {
  const m = courseByUid[les.lesson_uid];
  const segs = deriveBlockAnchorSegments(les);
  const label = deriveBlockAnchorLabel(les);
  const fullyNewBadge = m?.build_hint === 'fully_new'
    ? '<span class="match-fully-new-badge">完全新制</span><br>'
    : '';
  const href = `/new-library/lessons/${encodeURIComponent(les.lesson_uid)}/annotate`;
  return (
    `${fullyNewBadge}` +
    segTrackHtml(segs, BLOCK_SEGS, label, '') +
    `<div class="annotate-actions">` +
    `<a class="btn btn-sm btn-annotate" href="${href}" target="_blank" rel="noopener" title="建块与锚定">建块</a>` +
    `</div>`
  );
}

function syncAnalysisBatchRunning(data) {
  const batch = data?.analysis_batch;
  const status = batch?.status || 'idle';
  analysisBatchRunning = status === 'running' || status === 'cancelling';
  const hint = document.getElementById('analysis-batch-hint');
  if (hint) hint.hidden = !analysisBatchRunning;
  const cancelBtn = document.getElementById('analysis-batch-cancel-btn');
  if (cancelBtn) {
    cancelBtn.disabled = status === 'cancelling';
    cancelBtn.textContent = status === 'cancelling' ? '取消中…' : '取消任务';
  }
}

function refreshAnalysisBatchButton(data) {
  const btn = document.getElementById('analysis-batch-btn');
  if (!btn) return;
  const as = data?.analysis_summary || {};
  const pagesOk = (data?.lessons || []).some((l) => (l.lesson_page_count || 0) > 0);
  const coarseOk = isCourseDone(data);
  const pending = (as.await_ocr || 0) + (as.await_curate || 0) + (as.await_prescan || 0)
    + (as.failed || 0);
  const analyzed = (as.ready || 0) + (as.needs_review || 0) + (as.await_confirm || 0);
  const total = as.lesson_count || data?.lesson_count || 0;
  btn.disabled = !coarseOk || !pagesOk || analysisBatchRunning || total <= 0;
  btn.textContent = analysisBatchRunning ? '分析中…' : (
    analyzed >= total && total > 0 && pending === 0 ? '重新对照分析' : '批量对照分析'
  );
}

function refreshManualReviewHint(data) {
  const hintEl = document.querySelector('.intake-lessons-hint');
  if (!hintEl) return;
  const as = data?.analysis_summary || {};
  const manual = (as.await_confirm || 0) + (as.needs_review || 0);
  hintEl.innerHTML = manual > 0
    ? `页码校对 · 对照分析 · 建块与锚定 · <strong class="intake-manual-review-count">${manual} 课待人工确认对照</strong>`
    : '页码校对 · 对照分析 · 建块与锚定';
}

function countPendingBlockAnchorLessons(data) {
  return (data?.lessons || []).filter((les) => {
    const bc = les.block_count || 0;
    const ac = les.anchored_block_count || 0;
    const fullyNew = courseByUid[les.lesson_uid]?.build_hint === 'fully_new';
    if (bc === 0) return true;
    if (fullyNew) return false;
    return ac < bc;
  }).length;
}

function syncBlockBatchRunning(data) {
  const batch = data?.block_batch;
  const status = batch?.status || 'idle';
  blockBatchRunning = status === 'running' || status === 'cancelling';
  const hint = document.getElementById('block-anchor-batch-hint');
  if (hint) hint.hidden = !blockBatchRunning;
  const cancelBtn = document.getElementById('block-anchor-batch-cancel-btn');
  if (cancelBtn) {
    cancelBtn.disabled = status === 'cancelling';
    cancelBtn.textContent = status === 'cancelling' ? '取消中…' : '取消任务';
  }
}

function refreshBlockAnchorBatchButton(data) {
  const btn = document.getElementById('block-anchor-batch-btn');
  if (!btn) return;
  const as = data?.analysis_summary || {};
  const analysisStarted = (as.ready || 0) + (as.needs_review || 0) + (as.running || 0) > 0
    || (as.await_ocr || 0) > 0;
  const pending = countPendingBlockAnchorLessons(data);
  const total = data?.lesson_count || 0;
  const enabled = isCourseDone(data) && analysisStarted && pending > 0
    && !analysisBatchRunning && !blockBatchRunning;
  btn.disabled = !enabled;
  if (blockBatchRunning) {
    btn.textContent = '建块中…';
    btn.title = '整册建块后台运行中，行内可查看各课进度';
    return;
  }
  if (pending <= 0 && total > 0 && isCourseDone(data)) {
    btn.textContent = '建块已完成';
    btn.title = '本册课时均已建块（完全新制课无需锚定）';
    return;
  }
  btn.textContent = pending > 0 ? `整册建块（${pending} 课）` : '整册建块';
  btn.title = pending > 0
    ? `后台逐课运行建块流水线（含豆包锚定），尚有 ${pending} 课待处理`
    : '逐课建块与锚定；行内查看进度';
}

function scheduleBlockBatchPoll(data) {
  if (blockBatchPollTimer) {
    clearTimeout(blockBatchPollTimer);
    blockBatchPollTimer = null;
  }
  const st = data?.block_batch?.status;
  if (st !== 'running' && st !== 'cancelling') return;
  blockBatchPollTimer = setTimeout(async () => {
    try {
      const r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}`);
      const d = await readJsonResponse(r);
      if (!r.ok || !d.ok) return;
      mergeVolumeBlockBatchFromPoll(d);
      syncBlockBatchRunning(lastVolumeData);
      refreshBlockAnchorBatchButton(lastVolumeData);
      const st = d?.block_batch?.status;
      if (st !== 'running' && st !== 'cancelling') {
        lastVolumeData = d;
        renderVolume(d);
        return;
      }
      patchLessonBlockCells();
      scheduleBlockBatchPoll(lastVolumeData);
    } catch {
      /* ignore */
    }
  }, 3000);
}

async function cancelBlockBatch() {
  const btn = document.getElementById('block-anchor-batch-cancel-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(
      `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/block-batch/cancel`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || '取消失败');
    toast(d.message || '已请求取消批量建块');
    await loadVolume(true);
  } catch (e) {
    toast(e.message || String(e));
    if (btn) btn.disabled = false;
  }
}

async function startBlockBatch() {
  const btn = document.getElementById('block-anchor-batch-btn');
  if (btn) btn.disabled = true;
  blockBatchRunning = true;
  const hint = document.getElementById('block-anchor-batch-hint');
  if (hint) hint.hidden = false;
  try {
    const r = await fetch(
      `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/block-batch`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || '启动失败');
    if (d.status === 'idle' && d.lesson_count === 0) {
      toast(d.message || '没有需要建块/锚定的课时');
    } else {
      toast(d.message || `整册建块已启动（${d.lesson_count || ''} 课）`);
    }
    await loadVolume(true);
  } catch (e) {
    toast(e.message || String(e));
    blockBatchRunning = false;
    if (hint) hint.hidden = true;
    refreshBlockAnchorBatchButton(lastVolumeData);
  }
}

function scheduleAnalysisBatchPoll(data) {
  if (analysisBatchPollTimer) {
    clearTimeout(analysisBatchPollTimer);
    analysisBatchPollTimer = null;
  }
  const st = data?.analysis_batch?.status;
  if (st !== 'running' && st !== 'cancelling') return;
  analysisBatchPollTimer = setTimeout(async () => {
    try {
      let r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}`);
      const d = await readJsonResponse(r);
      if (!r.ok || !d.ok) return;
      mergeVolumeAnalysisFromPoll(d);
      syncAnalysisBatchRunning(lastVolumeData);
      refreshAnalysisBatchButton(lastVolumeData);
      const st = d?.analysis_batch?.status;
      if (st !== 'running' && st !== 'cancelling') {
        lastVolumeData = d;
        renderVolume(d);
        return;
      }
      patchLessonAnalysisCells();
      scheduleAnalysisBatchPoll(lastVolumeData);
    } catch {
      /* ignore */
    }
  }, 3000);
}

async function cancelAnalysisBatch() {
  const btn = document.getElementById('analysis-batch-cancel-btn');
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(
      `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/analysis-batch/cancel`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || '取消失败');
    toast(d.message || '已请求取消批量分析');
    await loadVolume(true);
  } catch (e) {
    toast(e.message || String(e));
    if (btn) btn.disabled = false;
  }
}

async function startLessonAnalysisForLesson(lessonUid) {
  if (isLessonInBatchQueue(lessonUid)) {
    toast('该课在批量分析队列中，请先点「取消批量分析」或等待完成');
    return;
  }
  if (!confirm('对本课运行对照分析？\n整课：文字OCR → 图片OCR → 整理原子 → 栏目·块名 → 对照预判；可能耗时数分钟。')) return;
  lessonAnalysisBusy.add(lessonUid);
  if (lastVolumeData) renderVolume(lastVolumeData);
  try {
    const r = await fetch(
      `${API_PREFIX}/lessons/${encodeURIComponent(lessonUid)}/analysis`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || '对照分析失败');
    toast(d.analysis_applied_pair_review
      ? `对照分析完成，已自动确认（${d.analysis_applied_pair_review}）`
      : '对照分析完成');
    await loadVolume(true);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    lessonAnalysisBusy.delete(lessonUid);
    if (lastVolumeData) renderVolume(lastVolumeData);
  }
}

async function startAnalysisBatch() {
  const btn = document.getElementById('analysis-batch-btn');
  if (btn) btn.disabled = true;
  analysisBatchRunning = true;
  const hint = document.getElementById('analysis-batch-hint');
  if (hint) hint.hidden = false;
  try {
    const r = await fetch(
      `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/analysis-batch`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || d.ok === false) throw new Error(d.error || '启动失败');
    toast(d.message || '对照分析已在后台运行');
    await loadVolume(true);
  } catch (e) {
    toast(e.message || String(e));
    analysisBatchRunning = false;
    if (hint) hint.hidden = true;
    refreshAnalysisBatchButton(lastVolumeData);
  }
}

async function runCatalogStep() {
  preprocessActiveStep = 'catalog';
  setIntakeLocalBusy('preprocess');
  renderPreprocessCard(lastVolumeData);
  const meta = document.getElementById('catalog-meta');
  const hint = document.getElementById('preprocess-hint');
  if (hint) {
    hint.hidden = false;
    hint.innerHTML = hintWithDot('cw-zip-pending', '正在识别目录…');
  }
  const r = await fetch(
    `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/catalog-from-pdf`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ replace: true }) },
  );
  const d = await readJsonResponse(r);
  if (!r.ok || !d.ok) throw new Error(d.error || d.message || '目录识别失败');
  if (meta) {
    meta.hidden = false;
    meta.textContent = d.message || `已导入 ${d.lessons_created || d.lesson_count} 节课`;
  }
  if (hint) hint.hidden = true;
}

async function runParseStep(opts = {}) {
  const forceRecalibrate = !!opts.forceRecalibrate;
  preprocessActiveStep = 'split';
  renderPreprocessCard(lastVolumeData);
  showParseBusy();
  const r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/parse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      replace_lessons: false,
      write_lesson_pages: false,
      async: true,
      force_recalibrate: forceRecalibrate,
    }),
  });
  const d = await readJsonResponse(r);
  if (r.status === 202 || d.async) {
    await waitForParseCompletion();
    return;
  }
  if (!d.ok) {
    renderParseResult(d);
    throw new Error(d.error || d.parse_error || '划分失败');
  }
  renderParseResult(d);
}

async function runBuildPagesStep() {
  preprocessActiveStep = 'pages';
  renderPreprocessCard(lastVolumeData);
  const r = await fetch(
    `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/lesson-pages`,
    { method: 'POST' },
  );
  const d = await readJsonResponse(r);
  if (!r.ok || !d.ok) throw new Error(d.error || '生成页图失败');
  toast(`已生成 ${d.lesson_pages_written || 0} 张页图`);
}

async function runCoarseStep() {
  preprocessActiveStep = 'coarse';
  renderPreprocessCard(lastVolumeData);
  const meta = document.getElementById('course-meta');
  if (meta) {
    meta.hidden = false;
    meta.innerHTML = intakeInfoRows([['状态', '正在对照旧库…']]);
  }
  const r = await fetch(
    `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/course-match`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
  );
  const d = await readJsonResponse(r);
  if (!r.ok || !d.ok) throw new Error(d.error || '粗分失败');
  applyCourseIndex(d.matches_by_lesson);
  renderCourseMeta(d.summary, d.status);
}

async function runPreprocessPipeline() {
  setIntakeLocalBusy('preprocess');
  try {
    let data = lastVolumeData || await loadVolume();
    const alreadyComplete = isCatalogDone(data)
      && isParseDone(data)
      && isBuildPagesDone(data)
      && isCourseDone(data);
    // 新库：只补未完成步骤。已有目录时不强制重抽，避免冲掉可用课表。
    // 「重新预处理」必须重划页码（清偏移缓存重校准），再生成页图并粗分。
    if (!isCatalogDone(data)) {
      await runCatalogStep();
      data = await loadVolume();
    }
    if (!isParseDone(data) || alreadyComplete) {
      await runParseStep({ forceRecalibrate: alreadyComplete });
      data = await loadVolume();
    }
    const hasRanges = (data.lessons || []).every((l) => l.page_start && l.page_end);
    if (!hasRanges) {
      toast('页码划分未完成，请检查预处理结果');
      return;
    }
    if (!isBuildPagesDone(data) || alreadyComplete) {
      await runBuildPagesStep();
      data = await loadVolume();
    }
    if (!isCourseDone(data) || alreadyComplete) {
      await runCoarseStep();
      await loadVolume();
      toast(alreadyComplete ? '已重新划分页码并粗分' : '整册预处理完成');
      return;
    }
    toast('整册预处理完成');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    preprocessActiveStep = null;
    clearParseHintTimer();
    clearIntakeLocalBusy();
    await loadVolume();
  }
}

function pipelineStepDone(data, key) {
  return !!data?.pipeline?.steps?.find((s) => s.key === key)?.done;
}

function isCatalogDone(data) {
  return pipelineStepDone(data, 'catalog_extract')
    || ((data?.lesson_count || 0) > 0 && !!data?.has_pdf);
}

function isParseDone(data) {
  if (pipelineStepDone(data, 'lesson_split')) return true;
  if (data?.parse_status !== 'done') return false;
  const lessons = data?.lessons || [];
  return lessons.length > 0
    && lessons.every((les) => les.page_start && les.page_end);
}

function isBuildPagesDone(data) {
  const lessons = data?.lessons || [];
  return lessons.length > 0
    && lessons.every((les) => (les.lesson_page_count || 0) > 0);
}

function isCourseDone(data) {
  return data?.course_match?.status === 'done';
}

function applyIntakeStepButton(btnId, { key, done, enabled, busy, busyText }) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  const labels = INTAKE_STEP_LABELS[key];
  btn.classList.remove(
    'intake-step-btn-idle',
    'intake-step-btn-done',
    'intake-step-btn-ready',
    'intake-step-btn-rerun',
    'pdf-upload-btn-ready',
    'is-btn-glow',
    'is-busy-glow',
  );
  if (busy) {
    btn.disabled = true;
    btn.textContent = busyText || labels.busy;
    return;
  }
  if (done && enabled) {
    btn.classList.add('intake-step-btn-done', 'intake-step-btn-rerun');
    btn.disabled = false;
    btn.textContent = labels.rerun || labels.idle;
    btn.title = `已完成（${labels.done}），点击可${labels.rerun || '重新执行'}`;
    return;
  }
  if (done) {
    btn.classList.add('intake-step-btn-done');
    btn.disabled = true;
    btn.textContent = labels.done;
    btn.title = '';
    return;
  }
  if (enabled) {
    btn.classList.add('intake-step-btn-ready');
    btn.disabled = false;
    btn.textContent = labels.idle;
    btn.title = '';
    return;
  }
  btn.classList.add('intake-step-btn-idle');
  btn.disabled = true;
  btn.textContent = labels.idle;
  btn.title = '';
}

function refreshIntakeStepButtons(data) {
  if (!data) return;
  renderPreprocessCard(data);
  syncAnalysisBatchRunning(data);
  syncBlockBatchRunning(data);
  refreshAnalysisBatchButton(data);
  refreshManualReviewHint(data);
  refreshBlockAnchorBatchButton(data);
  const blockHint = document.getElementById('block-anchor-batch-hint');
  if (blockHint && !blockBatchRunning) {
    blockHint.hidden = true;
  }
}

function finishIntakeStepAction() {
  clearIntakeLocalBusy();
  if (lastVolumeData) {
    refreshIntakeStepButtons(lastVolumeData);
    updateIntakeStepFocus(lastVolumeData);
  }
}

function flashPdfUpload(message) {
  if (pdfUploadFlashTimer) clearTimeout(pdfUploadFlashTimer);
  setPdfUploadHint(hintWithDot('cw-upload-flash', escapeHtml(message)));
  pdfUploadFlashTimer = setTimeout(() => {
    pdfUploadFlashTimer = null;
    refreshPdfUploadUi();
  }, PDF_UPLOAD_FLASH_MS);
}

function setParseHint(html) {
  const el = document.getElementById('preprocess-hint') || document.getElementById('parse-hint');
  if (!el) return;
  if (!html) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  el.innerHTML = html;
}

function clearParseHintTimer() {
  if (parseHintTimer) {
    clearInterval(parseHintTimer);
    parseHintTimer = null;
  }
}

function sleepMs(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForParseCompletion() {
  const maxMs = 20 * 60 * 1000;
  const started = Date.now();
  while (Date.now() - started < maxMs) {
    await sleepMs(3000);
    const r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}`);
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '加载册次失败');
    if (d.parse_status === 'done') {
      await loadCourseMatch();
      renderVolume(d);
      return d;
    }
    if (d.parse_status === 'failed') {
      renderParseResult(d);
      throw new Error(d.parse_error || d.error || '划分失败');
    }
    const waited = Math.round((Date.now() - started) / 1000);
    setParseHint(hintWithDot('cw-zip-pending', `后台划分中… 已等待 ${waited} 秒`));
  }
  throw new Error('划分仍在进行，请稍后刷新页面查看结果');
}

async function resumeParsePollingIfNeeded(data) {
  if (parsePollActive || intakeLocalBusy === 'parse') return data;
  if (data?.parse_status !== 'processing') return data;
  parsePollActive = true;
  setIntakeLocalBusy('preprocess');
  preprocessActiveStep = 'split';
  showParseBusy();
  try {
    const final = await waitForParseCompletion();
    const pageNote = final.lesson_pages_written
      ? ` · 生成 ${final.lesson_pages_written} 张页图`
      : ' · 预览用 PDF 直链（未生成页图）';
    const summary = `页码已划分 · 已匹配 ${final.matched}/${final.lesson_count} 课${pageNote}`;
    toast(summary);
    flashParseSuccess(summary);
    renderParseResult(final);
    return final;
  } catch (e) {
    const msg = e.message || String(e);
    showParseError(msg);
    toast(msg);
    return data;
  } finally {
    parsePollActive = false;
    clearParseHintTimer();
    finishIntakeStepAction();
  }
}

function showParseBusy() {
  setIntakeLocalBusy('preprocess');
  preprocessActiveStep = 'split';
  let idx = 0;
  setParseHint(hintWithDot('cw-zip-pending', PARSE_BUSY_HINTS[0]));
  clearParseHintTimer();
  parseHintTimer = setInterval(() => {
    idx = (idx + 1) % PARSE_BUSY_HINTS.length;
    setParseHint(hintWithDot('cw-zip-pending', PARSE_BUSY_HINTS[idx]));
  }, 4000);
}

function setIntakeLocalBusy(kind) {
  intakeLocalBusy = kind;
  if (lastVolumeData) updateIntakeStepFocus(lastVolumeData);
}

function clearIntakeLocalBusy() {
  intakeLocalBusy = null;
  if (lastVolumeData) updateIntakeStepFocus(lastVolumeData);
}

function resolvePostProofreadFocus(data) {
  const lessons = data.lessons || [];
  const total = data.lesson_count || 0;
  const verified = data.verified_lesson_count || 0;
  const allHavePageImages = lessons.length > 0
    && lessons.every((l) => (l.lesson_page_count || 0) > 0);
  const cmDone = data.course_match?.status === 'done';

  if (verified < total) {
    return { tip: '请在下方表中预览、修正页码并点「保存」', actionCard: null };
  }
  if (!allHavePageImages) {
    return { tip: '页码已保存，请执行下方第 1 步', actionCard: 'build' };
  }
  if (!cmDone) {
    return { tip: '页图已生成，请执行下方第 2 步', actionCard: 'course' };
  }
  return { tip: '粗分已完成，可逐课进入建块', actionCard: null };
}

function clearFocusGlow() {
  document.querySelectorAll('.is-btn-glow').forEach((el) => {
    el.classList.remove('is-btn-glow', 'is-busy-glow');
  });
}

function glowActionButton(container, busy) {
  if (!container) return;
  const btn = container.querySelector(
    '.btn.intake-step-btn-rerun, .btn.intake-step-btn-ready, .btn:not(.pdf-file-btn):not(.pdf-upload-btn-done):not(.pdf-upload-btn-idle):not(.intake-step-btn-idle):not(.intake-step-btn-done)',
  );
  if (!btn) return;
  btn.classList.add('is-btn-glow');
  if (busy) btn.classList.add('is-busy-glow');
}

function focusIntakeCard(cardId, tip, busy) {
  const el = document.getElementById(cardId);
  if (!el) return;
  el.classList.add('is-focus');
  if (busy) el.classList.add('is-busy-focus');
  const ribbon = el.querySelector(':scope > .intake-focus-ribbon');
  if (ribbon) {
    ribbon.hidden = false;
    ribbon.textContent = tip;
  }
  glowActionButton(el, busy);
}

function focusLessonsPost(tip, actionCard, busy) {
  const lessons = document.getElementById('lessons-card');
  if (lessons) {
    if (!actionCard) {
      lessons.classList.add('is-focus');
      if (busy) lessons.classList.add('is-busy-focus');
    }
    const ribbon = lessons.querySelector(':scope > .intake-focus-ribbon');
    if (ribbon) {
      ribbon.hidden = false;
      ribbon.textContent = tip;
    }
  }
  const post = document.getElementById('post-proofread-actions');
  if (!post || !actionCard) return;
  const subRibbon = post.querySelector('.intake-focus-ribbon--sub');
  if (subRibbon) {
    subRibbon.hidden = false;
    subRibbon.classList.add('is-focus-visible');
    subRibbon.textContent = actionCard === 'build'
      ? '↓ 请点击「一键生成页图」'
      : '↓ 请点击「运行粗分」';
  }
  const cards = post.querySelectorAll('.intake-action-card');
  const idx = actionCard === 'build' ? 0 : 1;
  glowActionButton(cards[idx], busy);
}

function updateIntakeStepFocus(data) {
  clearFocusGlow();
  ['upload-card', 'preprocess-card', 'lessons-card'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('is-focus', 'is-busy-focus', 'is-step-done');
    const ribbon = el.querySelector(':scope > .intake-focus-ribbon');
    if (ribbon) {
      ribbon.hidden = true;
      ribbon.textContent = '';
    }
  });

  const postProof = document.getElementById('post-proofread-actions');
  postProof?.classList.remove('is-focus', 'is-busy-focus');

  const pipeline = data?.pipeline;
  const nav = document.getElementById('volume-pipeline');
  const pipelineRibbon = document.getElementById('pipeline-ribbon');
  const fill = document.getElementById('volume-pipeline-fill');

  if (!pipeline?.steps?.length) {
    nav?.classList.remove('is-pipeline-live');
    if (pipelineRibbon) pipelineRibbon.hidden = true;
    fill?.classList.remove('is-animated');
    return;
  }

  const { steps, active_index: activeIndex, all_done: allDone } = pipeline;
  const activeStep = steps[activeIndex] || steps[0];

  const doneCards = ['upload-card', 'preprocess-card'];
  if (isCourseDone(data)) document.getElementById('preprocess-card')?.classList.add('is-step-done');

  const pipelineTips = {
    pdf_upload: '当前步骤：上传整册 PDF',
    catalog_extract: '当前步骤：整册预处理',
    lesson_split: '当前步骤：整册预处理',
    coarse_match: '当前步骤：批量对照分析',
    block_build: '当前步骤：逐课建块与锚定',
  };

  nav?.classList.toggle('is-pipeline-live', !allDone);
  fill?.classList.toggle('is-animated', !allDone);
  if (pipelineRibbon) {
    if (!allDone) {
      pipelineRibbon.hidden = false;
      pipelineRibbon.textContent = pipelineTips[activeStep.key] || activeStep.label;
    } else {
      pipelineRibbon.hidden = true;
      pipelineRibbon.textContent = '';
    }
  }

  if (intakeLocalBusy === 'preprocess' || preprocessActiveStep) {
    focusIntakeCard('preprocess-card', derivePreprocessLabel(data) || '预处理中…', true);
    return;
  }
  if (analysisBatchRunning) {
    const lessons = document.getElementById('lessons-card');
    if (lessons) {
      lessons.classList.add('is-focus', 'is-busy-focus');
      const ribbon = lessons.querySelector(':scope > .intake-focus-ribbon');
      if (ribbon) {
        ribbon.hidden = false;
        ribbon.textContent = '对照分析进行中…';
      }
    }
    return;
  }
  if (blockBatchRunning) {
    const lessons = document.getElementById('lessons-card');
    if (lessons) {
      lessons.classList.add('is-focus', 'is-busy-focus');
      const ribbon = lessons.querySelector(':scope > .intake-focus-ribbon');
      if (ribbon) {
        ribbon.hidden = false;
        ribbon.textContent = '整册建块进行中…';
      }
    }
    return;
  }

  if (allDone) return;

  if (!data.has_pdf) {
    focusIntakeCard('upload-card', '请上传整册 PDF', false);
    return;
  }
  if (!isCourseDone(data)) {
    focusIntakeCard('preprocess-card', '请点击「开始预处理」', false);
    return;
  }
  if ((data.analysis_summary?.ready || 0) < (data.lesson_count || 0)) {
    const lessons = document.getElementById('lessons-card');
    if (lessons) {
      lessons.classList.add('is-focus');
      const ribbon = lessons.querySelector(':scope > .intake-focus-ribbon');
      if (ribbon) {
        ribbon.hidden = false;
        ribbon.textContent = '请点击「批量对照分析」';
      }
      glowActionButton(document.querySelector('.intake-lessons-actions'), false);
    }
    return;
  }
  if (countPendingBlockAnchorLessons(data) > 0) {
    const lessons = document.getElementById('lessons-card');
    if (lessons) {
      lessons.classList.add('is-focus');
      const ribbon = lessons.querySelector(':scope > .intake-focus-ribbon');
      if (ribbon) {
        ribbon.hidden = false;
        ribbon.textContent = '请点击「整册建块」后台建块与锚定';
      }
      glowActionButton(document.getElementById('block-anchor-batch-btn')?.parentElement, false);
    }
    return;
  }
  focusLessonsPost('建块与锚定已完成', null, false);
}

function flashParseSuccess(message) {
  clearParseHintTimer();
  setParseHint(hintWithDot('cw-upload-flash', escapeHtml(message)));
}

function showParseError(message) {
  clearParseHintTimer();
  setParseHint(
    `<span class="pdf-upload-error">${escapeHtml(message || '解析失败')}</span>`,
  );
}

function courseTierBadge(tier, label) {
  const cls = tier || 'pending';
  return `<span class="match-tier ${cls}">${label || '待粗分'}</span>`;
}

function newLibraryWorkbenchHref(editionId) {
  if (!editionId) return '/new-library/';
  return `/new-library/?edition=${encodeURIComponent(editionId)}`;
}

function intakeInfoRows(rows) {
  return rows
    .filter(Boolean)
    .map(([label, val]) =>
      `<div class="intake-info-row"><span class="label">${escapeHtml(String(label))}</span><span>${val}</span></div>`,
    )
    .join('');
}

function renderMatchScore(m) {
  if (m.similarity_score == null) return '';
  const pct = (m.similarity_score * 100).toFixed(1);
  const low = m.match_tier === 'high_similarity' && m.similarity_score < 0.405;
  const cls = low ? 'match-score match-score--low' : 'match-score';
  const title = '规则参考相似度（单元名+课时名+语境混合；AI 高相似档位可与此分不一致）';
  return `<div class="${cls}" title="${escapeHtml(title)}">参考 ${pct}%</div>`;
}

function renderCourseCell(lessonUid) {
  const m = courseByUid[lessonUid];
  if (!m) return courseTierBadge('pending', '待粗分');
  if (m.build_hint === 'fully_new') {
    return '<span class="match-fully-new-badge">完全新制</span>';
  }
  const score = renderMatchScore(m);
  return `${courseTierBadge(m.match_tier, m.match_label)}${score}`;
}

function formatOldLessonHintHtml(hint) {
  if (!hint) return '';
  const text = String(hint);
  const aiMatch = text.match(/（AI：([\s\S]+)）$/);
  if (aiMatch) {
    const base = text.slice(0, aiMatch.index).trim();
    const ai = aiMatch[1].trim();
    if (base) {
      return `${escapeHtml(base)}<br><span class="match-ai-hint">AI：${escapeHtml(ai)}</span>`;
    }
    return `<span class="match-ai-hint">AI：${escapeHtml(ai)}</span>`;
  }
  if (text.startsWith('AI：')) {
    return `<span class="match-ai-hint">${escapeHtml(text)}</span>`;
  }
  return escapeHtml(text);
}

function shortenCoursewareId(id) {
  const text = String(id || '').trim();
  if (!text) return '';
  if (text.length <= 20) return text;
  return `${text.slice(0, 10)}…${text.slice(-6)}`;
}

function renderOldCoursewareMeta(coursewareId, pageCount) {
  const bits = [];
  if (coursewareId) {
    const shortId = shortenCoursewareId(coursewareId);
    bits.push(`<code title="${escapeHtml(coursewareId)}">${escapeHtml(shortId)}</code>`);
  } else {
    bits.push('<span class="match-no-courseware">无旧教材课件</span>');
  }
  if (pageCount) {
    bits.push(`<span class="match-page-count">${pageCount} 页</span>`);
  }
  return `<span class="match-courseware-line">${bits.join(' ')}</span>`;
}

function renderTraceabilityBlock(trace) {
  if (!trace?.old_lesson_hint) return '';
  return (
    `<div class="intake-match-block">` +
    `<span class="match-traceability-hint">溯源参考：${formatOldLessonHintHtml(trace.old_lesson_hint)}</span>` +
    renderOldCoursewareMeta(trace.old_courseware_id, trace.old_page_count) +
    `</div>`
  );
}

function renderBlockedOldBlock(blocked, label) {
  if (!blocked?.old_lesson_hint) return '';
  return (
    `<div class="intake-match-block">` +
    `<span class="match-blocked-old-hint">${escapeHtml(label)}：${formatOldLessonHintHtml(blocked.old_lesson_hint)}</span>` +
    renderOldCoursewareMeta(blocked.old_courseware_id, blocked.old_page_count) +
    `</div>`
  );
}

function renderOldMatchCell(lessonUid) {
  const m = courseByUid[lessonUid];
  if (!m) return '—';
  const trace = (m.candidates || []).find((c) => c.match_tier === 'traceability');
  if (m.build_hint === 'fully_new') {
    const parts = [];
    const blocked = m.build_hint_blocked_old;
    if (blocked?.old_lesson_hint) {
      const label = m.build_hint_reason === 'traceability' ? '溯源旧课' : '占用旧课';
      parts.push(renderBlockedOldBlock(blocked, label));
    } else if (m.match_tier === 'none') {
      const traceBlock = renderTraceabilityBlock(trace);
      if (traceBlock) parts.push(traceBlock);
    } else if (m.old_lesson_hint) {
      parts.push(formatOldLessonHintHtml(m.old_lesson_hint));
      parts.push(renderOldCoursewareMeta(m.old_courseware_id, m.old_page_count));
    }
    const note = m.build_hint_reason === 'traceability'
      ? '溯源旧课已被他课对照占用'
      : '对照旧课已被他课占用';
    parts.push(`<span class="intake-match-note">${note}</span>`);
    return `<div class="intake-match-cell">${parts.join('')}</div>`;
  }
  if (m.match_tier === 'none') {
    const traceBlock = renderTraceabilityBlock(trace);
    if (traceBlock) return `<div class="intake-match-cell">${traceBlock}</div>`;
    return '—';
  }
  const parts = [];
  if (m.old_lesson_hint) parts.push(formatOldLessonHintHtml(m.old_lesson_hint));
  if (m.old_volume_code) parts.push(`<span class="intake-match-meta">${escapeHtml(m.old_volume_code)}</span>`);
  parts.push(renderOldCoursewareMeta(m.old_courseware_id, m.old_page_count));
  return `<div class="intake-match-cell">${parts.join('')}</div>` || '—';
}

function renderAnnotateCell(les) {
  return renderBlockAnchorCell(les);
}

function applyCourseIndex(matchesByLesson) {
  courseByUid = {};
  for (const row of matchesByLesson || []) {
    courseByUid[row.new_lesson_uid] = row;
  }
}

function renderCourseMeta(summary, status) {
  const meta = document.getElementById('course-meta');
  if (!meta) return;
  if (!summary || status !== 'done') {
    meta.hidden = true;
    meta.innerHTML = '';
    return;
  }
  meta.hidden = false;
    meta.innerHTML = intakeInfoRows([
    ['粗分结果', `完全同名 ${summary.exact_match || 0} · 高相似 ${summary.high_similarity || 0} · 溯源 ${summary.traceability || 0} · 无匹配 ${summary.no_match || 0}`],
    summary.match_engine === 'batch'
      ? ['粗分引擎', summary.llm_fallbacks && !summary.llm_decisions
          ? `整册对照 LLM 失败，已回退规则（${summary.llm_fallbacks} 课）`
          : `整册目录对照（LLM ${summary.llm_decisions || 0} 课 · 回退 ${summary.llm_fallbacks || 0} 课）`]
      : summary.match_engine === 'hybrid'
      ? ['粗分引擎', `逐课混合（${summary.llm_decisions || 0} 课 · 回退 ${summary.llm_fallbacks || 0} 课）`]
      : null,
    summary.old_pool_size ? ['旧库池', `${summary.old_pool_size} 节`] : null,
  ]);
}

function toast(msg) {
  const el = document.getElementById('toast');
  const text = String(msg || '');
  el.textContent = text.length > 140 ? `${text.slice(0, 139)}…` : text;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 5000);
}

function formatBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function parseStatusLabel(status) {
  const map = {
    pending: '待解析',
    processing: '解析中',
    done: '已解析',
    failed: '解析失败',
  };
  return map[status] || status || '待解析';
}

function getPreviewOverlay() {
  return document.getElementById('page-preview');
}

function lockBackdropScroll() {
  previewBackdropScrollY = window.scrollY || document.documentElement.scrollTop || 0;
  document.body.classList.add('page-preview-open');
  document.body.style.top = `-${previewBackdropScrollY}px`;
}

function unlockBackdropScroll() {
  document.body.classList.remove('page-preview-open');
  document.body.style.top = '';
  window.scrollTo(0, previewBackdropScrollY);
}

function closeLessonPreview() {
  const overlay = getPreviewOverlay();
  if (!overlay) return;
  overlay.classList.remove('is-open');
  overlay.setAttribute('aria-hidden', 'true');
  previewOpenLessonUid = null;
  unlockBackdropScroll();
}

function openLessonPreviewShell() {
  const overlay = getPreviewOverlay();
  if (!overlay) return;
  if (!overlay.classList.contains('is-open')) {
    lockBackdropScroll();
  }
  overlay.classList.add('is-open');
  overlay.setAttribute('aria-hidden', 'false');
  const body = overlay.querySelector('.page-preview-body');
  if (body) body.scrollTop = 0;
}

function initPreviewModal() {
  const overlay = getPreviewOverlay();
  const closeBtn = document.getElementById('preview-close');
  if (!overlay) return;

  if (closeBtn) {
    closeBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      closeLessonPreview();
    });
  }

  overlay.addEventListener('click', (ev) => {
    if (ev.target === overlay) closeLessonPreview();
  });

  const inner = overlay.querySelector('.page-preview-inner');
  if (inner) {
    inner.addEventListener('click', (ev) => ev.stopPropagation());
  }

  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && overlay.classList.contains('is-open')) {
      closeLessonPreview();
    }
  });
}

function getPageRangeInputs(lessonUid) {
  const startEl = document.querySelector(
    `input.page-start-input[data-lesson="${lessonUid}"]`,
  );
  const endEl = document.querySelector(
    `input.page-end-input[data-lesson="${lessonUid}"]`,
  );
  return { startEl, endEl };
}

function readPageRange(lessonUid) {
  const { startEl, endEl } = getPageRangeInputs(lessonUid);
  if (!startEl || !endEl) return null;
  const pageStart = parseInt(startEl.value, 10);
  const pageEnd = parseInt(endEl.value, 10);
  if (!Number.isFinite(pageStart) || !Number.isFinite(pageEnd)) return null;
  if (pageStart < 1 || pageEnd < pageStart) return null;
  return { pageStart, pageEnd };
}

function markPageRangeDirty(lessonUid, dirty) {
  const { startEl, endEl } = getPageRangeInputs(lessonUid);
  if (startEl) startEl.classList.toggle('dirty', dirty);
  if (endEl) endEl.classList.toggle('dirty', dirty);
  const saveBtn = document.querySelector(
    `button.page-save-btn[data-lesson="${lessonUid}"]`,
  );
  if (saveBtn) saveBtn.hidden = !dirty;
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

function annotateStatsHtml(les) {
  return `<span class="annotate-stats-line">${les.block_count || 0} 区块</span>`;
}

function stepRatioValue(step) {
  if (!step) return 0;
  if (step.done) return 1;
  const r = Number(step.ratio);
  return Number.isFinite(r) ? Math.max(0, Math.min(1, r)) : 0;
}

function markerCenterX(marker, trackRect) {
  const r = marker.getBoundingClientRect();
  return r.left + r.width / 2 - trackRect.left;
}

function pipelineSegmentSpan(markers, trackRect) {
  if (markers.length < 2) return 72;
  const first = markerCenterX(markers[0], trackRect);
  const last = markerCenterX(markers[markers.length - 1], trackRect);
  return (last - first) / (markers.length - 1);
}

function pipelineTailX(markers, trackRect) {
  const lastMarker = markerCenterX(markers[markers.length - 1], trackRect);
  const segmentSpan = pipelineSegmentSpan(markers, trackRect);
  const trackRight = trackRect.width - 6;
  const available = Math.max(0, trackRight - lastMarker);
  return lastMarker + Math.min(segmentSpan, available);
}

function pipelineActiveIndex(steps) {
  for (let i = 0; i < steps.length; i++) {
    if (!steps[i]?.done) return i;
  }
  return steps.length - 1;
}

function pipelineProgressX(markers, steps, trackRect, allDone) {
  const tailX = pipelineTailX(markers, trackRect);
  const lastIdx = markers.length - 1;
  if (allDone) return tailX;

  for (let i = 0; i <= lastIdx; i++) {
    const step = steps[i];
    if (step?.done) continue;
    const r = stepRatioValue(step);
    const from = markerCenterX(markers[i], trackRect);
    const to = i < lastIdx
      ? markerCenterX(markers[i + 1], trackRect)
      : tailX;
    return from + (to - from) * r;
  }
  return tailX;
}

function updatePipelineWalker(track, walker, fill, rail, steps, allDone) {
  if (!track || !walker) return;
  const markers = track.parentElement?.querySelectorAll('.volume-pipeline-marker');
  if (!markers?.length) return;

  const trackRect = track.getBoundingClientRect();
  const activeIdx = pipelineActiveIndex(steps);
  const step = steps[activeIdx];
  const ratio = step?.done ? 1 : stepRatioValue(step);
  const busy = !!step?.busy;
  const progressX = pipelineProgressX(markers, steps, trackRect, allDone);
  const startX = markerCenterX(markers[0], trackRect);
  const tailX = pipelineTailX(markers, trackRect);

  track.style.paddingRight = '';

  walker.classList.toggle('is-idle', false);
  walker.classList.toggle('is-celebrating', !!allDone);
  walker.style.left = `${progressX}px`;

  if (rail) {
    rail.style.left = `${startX}px`;
    rail.style.width = `${Math.max(0, tailX - startX)}px`;
  }

  if (fill) {
    fill.style.left = `${startX}px`;
    fill.style.width = `${Math.max(0, progressX - startX)}px`;
  }
}

let pipelineResizeBound = false;

function renderVolumePipeline(data) {
  const nav = document.getElementById('volume-pipeline');
  const list = document.getElementById('volume-pipeline-steps');
  const track = document.getElementById('volume-pipeline-track');
  const walker = document.getElementById('volume-pipeline-walker');
  const fill = document.getElementById('volume-pipeline-fill');
  const rail = document.getElementById('volume-pipeline-rail');
  const pipeline = data?.pipeline;
  if (!nav || !list || !pipeline?.steps?.length) {
    if (nav) nav.hidden = true;
    return;
  }

  nav.hidden = false;
  const { steps, all_done: allDone } = pipeline;
  const activeIndex = pipelineActiveIndex(steps);

  list.innerHTML = steps.map((step, idx) => {
    let state = 'is-pending';
    if (step.done) state = 'is-done';
    else if (idx === activeIndex) state = step.busy ? 'is-active is-busy' : 'is-active';

    return (
      `<li class="volume-pipeline-step ${state}">` +
      `<span class="volume-pipeline-marker" aria-hidden="true">` +
      `<span class="step-num">${idx + 1}</span>` +
      `<span class="step-check">✓</span>` +
      `</span>` +
      `<span class="volume-pipeline-label">${escapeHtml(step.label)}</span>` +
      `<span class="volume-pipeline-progress">${step.progress ? escapeHtml(step.progress) : (step.done ? '已完成' : '')}</span>` +
      `</li>`
    );
  }).join('');

  requestAnimationFrame(() => {
    updatePipelineWalker(track, walker, fill, rail, steps, allDone);
  });

  if (!pipelineResizeBound) {
    pipelineResizeBound = true;
    window.addEventListener('resize', () => {
      if (lastVolumeData?.pipeline) {
        const p = lastVolumeData.pipeline;
        updatePipelineWalker(
          document.getElementById('volume-pipeline-track'),
          document.getElementById('volume-pipeline-walker'),
          document.getElementById('volume-pipeline-fill'),
          document.getElementById('volume-pipeline-rail'),
          p.steps,
          p.all_done,
        );
      }
    });
  }
}

function renderVolume(data) {
  lastVolumeData = data;
  renderVolumePipeline(data);
  const back = document.getElementById('intake-back-link');
  if (back) back.href = newLibraryWorkbenchHref(data.edition_id);

  document.getElementById('page-title').textContent = data.display_title || data.volume_code;
  const metaParts = [
    `<span class="intake-meta-chip">${escapeHtml(data.volume_code)}</span>`,
    `<span class="intake-meta-chip">${data.lesson_count} 课</span>`,
  ];
  if (data.verified_lesson_count) {
    metaParts.push(
      `<span class="intake-meta-chip">已校对 ${data.verified_lesson_count} 课</span>`,
    );
  }
  metaParts.push(
    `<span class="status-badge ${data.parse_status || 'pending'}">${parseStatusLabel(data.parse_status)}</span>`,
  );
  document.getElementById('page-sub').innerHTML = metaParts.join('');

  const meta = document.getElementById('pdf-meta');
  if (data.has_pdf) {
    meta.hidden = false;
    const storage = data.blob_storage === 'disk' ? '磁盘' : '数据库';
    meta.innerHTML = intakeInfoRows([
      ['存储', `已绑定 PDF（${storage} · blob ${data.blob_id?.slice(0, 8)}…）`],
      data.blob_size_bytes ? ['大小', formatBytes(data.blob_size_bytes)] : null,
      ['镜像', data.mirror_path || '未写入（可检查目录权限）'],
    ]);
  } else {
    meta.hidden = true;
    meta.innerHTML = '';
  }

  const tbody = document.getElementById('lesson-rows');
  const unitPos = lessonUnitPositions(data.lessons || []);
  tbody.innerHTML = data.lessons.map((les) => {
    const pos = unitPos[les.lesson_uid] || { idx: 0, total: 1 };
    const canUp = pos.idx > 0;
    const canDown = pos.idx < pos.total - 1;
    const hasRange = les.page_start && les.page_end;
    const startVal = les.page_start ?? '';
    const endVal = les.page_end ?? '';
    const verifiedBadge = les.page_range_verified
      ? '<span class="page-verified-badge">已校对</span>'
      : '';
    const reviewFlag = lessonReviewFlagHtml(les);
    const batchRowClass = lessonBatchRowClass(les);
    const rowClass = `${reviewFlag ? ' lesson-row--review-flag' : ''}${batchRowClass}`.trim();
    const expanded = expandedLessonUids.has(les.lesson_uid);
    const expandLabel = expanded ? '收起详情' : '展开详情';
    const expandIcon = expanded ? '▼' : '▶';
    const batchVariant = analysisBatchStatusVariant(les);
    const blockVariant = blockBatchStatusVariant(les);
    const compactRow = `<tr data-lesson-row="${les.lesson_uid}" class="lesson-row-compact${expanded ? ' is-expanded' : ''}${rowClass ? ` ${rowClass}` : ''}">
      <td class="lesson-expand-cell">
        <button type="button" class="lesson-expand-btn" data-lesson-expand="${les.lesson_uid}" aria-expanded="${expanded ? 'true' : 'false'}" title="${expandLabel}" aria-label="${expandLabel}">${expandIcon}</button>
      </td>
      <td class="lesson-unit-cell">${reviewFlag}<span class="lesson-unit-text">${escapeHtml(les.unit_title)}</span></td>
      <td class="lesson-title-cell">
        <div class="lesson-reorder">
          <span class="lesson-move-btns" aria-label="调整顺序">
            <button type="button" class="btn-icon lesson-move-btn" data-lesson="${les.lesson_uid}" data-dir="up" title="上移" ${canUp ? '' : 'disabled'}>▲</button>
            <button type="button" class="btn-icon lesson-move-btn" data-lesson="${les.lesson_uid}" data-dir="down" title="下移" ${canDown ? '' : 'disabled'}>▼</button>
          </span>
          <span class="lesson-title-text">${les.lesson_no} ${les.lesson_name}</span>
        </div>
      </td>
      <td class="lesson-coarse-cell">${renderCourseCell(les.lesson_uid)}</td>
      <td class="page-range-cell">
        <div class="page-range-inputs">
          <input type="number" min="1" class="page-start-input" data-lesson="${les.lesson_uid}"
            value="${startVal}" placeholder="起" aria-label="起始页">
          <span class="page-range-dash">–</span>
          <input type="number" min="1" class="page-end-input" data-lesson="${les.lesson_uid}"
            value="${endVal}" placeholder="止" aria-label="结束页">
          ${verifiedBadge}
        </div>
        <div class="page-range-actions">
          <button type="button" class="page-preview-btn" data-lesson="${les.lesson_uid}">预览</button>
          <button type="button" class="page-save-btn" data-lesson="${les.lesson_uid}" hidden>保存</button>
        </div>
      </td>
      <td>${les.lesson_page_count ? `<span class="intake-meta-chip intake-meta-chip--table intake-page-chip">${les.lesson_page_count} 张</span>` : (hasRange ? '<span class="page-pending-badge">未生成</span>' : '—')}</td>
      <td class="analysis-cell analysis-cell--compact${batchVariant ? ` analysis-cell--batch-${batchVariant}` : ''}">${renderCompactAnalysisCell(les)}</td>
      <td class="annotate-cell annotate-cell--compact${blockVariant ? ` annotate-cell--batch-${blockVariant}` : ''}">${renderCompactBlockCell(les)}</td>
    </tr>`;
    const detailRow = expanded
      ? `<tr class="lesson-row-detail" data-lesson-detail="${les.lesson_uid}"><td colspan="8">${renderExpandedPanel(les)}</td></tr>`
      : '';
    return compactRow + detailRow;
  }).join('');

  bindLessonTableEvents(tbody);

  document.getElementById('lessons-card').hidden = !data.lessons.length;

  if (data.course_match?.summary && data.course_match.status === 'done') {
    renderCourseMeta(data.course_match.summary, data.course_match.status);
  }

  const parseMeta = document.getElementById('parse-meta');
  if (parseMeta) {
    if (data.parse_status === 'failed' && data.parse_error) {
      parseMeta.hidden = false;
      parseMeta.textContent = `上次失败：${data.parse_error}`;
    } else if (!isParseDone(data)) {
      parseMeta.hidden = true;
      parseMeta.textContent = '';
    }
  }
  refreshPdfUploadUi();
  refreshIntakeStepButtons(data);
  renderPreprocessCard(data);
  updateIntakeStepFocus(data);
}

function renderParseResult(d) {
  const el = document.getElementById('parse-meta');
  if (!el) return;
  if (!d.ok && d.parse_error) {
    el.hidden = false;
    el.innerHTML = `失败：${escapeHtml(d.parse_error)}`;
    return;
  }
  if (d.ok) {
    el.hidden = false;
    const unmatched = (d.parse_details || d.lessons || [])
      .filter((x) => x.matched === false)
      .map((x) => `${x.lesson_name || x.lesson_uid}`);
    el.innerHTML = [
      `已匹配 ${d.matched}/${d.lesson_count} 课`,
      d.lesson_pages_written ? `已生成 ${d.lesson_pages_written} 张页图` : '页图按需预览（未批量生成）',
      d.text_mode ? `模式：${d.text_mode}` : '',
      d.total_pages ? `共 ${d.total_pages} 页` : '',
      d.lesson_source ? `课时来源：${
        {
          llm_vision: '大模型识别目录',
          llm_vision_cache: '大模型识别目录（缓存）',
          step1_ocr: 'OCR 识别目录',
          pdf_catalog: 'PDF 文本层',
        }[d.lesson_source] || d.lesson_source
      }` : '',
      d.warnings?.length ? `提示：${d.warnings.join('；')}` : '',
      unmatched.length
        ? `未匹配课时（请改页码后保存）：${unmatched.join('、')}`
        : '',
    ].filter(Boolean).join('<br>');
  }
}

async function readJsonResponse(r) {
  const text = await r.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`服务器返回异常（HTTP ${r.status}）`);
  }
}

async function loadCourseMatch() {
  try {
    const r = await fetch(
      `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/course-match`,
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) return null;
    applyCourseIndex(d.matches_by_lesson);
    renderCourseMeta(d.summary, d.status);
    return d;
  } catch {
    return null;
  }
}

async function moveLesson(lessonUid, direction) {
  const btn = document.querySelector(
    `.lesson-move-btn[data-lesson="${lessonUid}"][data-dir="${direction}"]`,
  );
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(
      `${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/lessons/${encodeURIComponent(lessonUid)}/reorder`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ direction }),
      },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '调整顺序失败');
    await loadVolume();
    toast(direction === 'up' ? '已上移' : '已下移');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function ensureVolumeRecord() {
  const r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/ensure`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  const d = await readJsonResponse(r);
  if (!r.ok || !d.ok) throw new Error(d.error || '创建册次失败');
  return d;
}

async function loadVolume(skipBatchSync) {
  let r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}`);
  if (r.status === 404) {
    await ensureVolumeRecord();
    r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}`);
  }
  const d = await readJsonResponse(r);
  if (!r.ok || !d.ok) throw new Error(d.error || `加载失败（HTTP ${r.status}）`);
  await loadCourseMatch();
  renderVolume(d);
  syncAnalysisBatchRunning(d);
  syncBlockBatchRunning(d);
  scheduleAnalysisBatchPoll(d);
  scheduleBlockBatchPoll(d);
  if (skipBatchSync) return d;
  return resumeParsePollingIfNeeded(d);
}

async function saveLessonPageRange(lessonUid) {
  const range = readPageRange(lessonUid);
  if (!range) {
    toast('请填写有效的起止页码');
    return;
  }
  const btn = document.querySelector(`button.page-save-btn[data-lesson="${lessonUid}"]`);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '保存中…';
  }
  try {
    const r = await fetch(
      `${API_PREFIX}/lessons/${encodeURIComponent(lessonUid)}/page-range`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_start: range.pageStart,
          page_end: range.pageEnd,
          rebuild_pages: false,
        }),
      },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '保存失败');
    toast(d.message || '已确认落库');
    markPageRangeDirty(lessonUid, false);
    await loadVolume();
    if (previewOpenLessonUid === lessonUid) {
      await openLessonPreview(lessonUid);
    }
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '保存';
    }
  }
}

initPreviewModal();

const pdfInputEl = document.getElementById('pdf-input');
if (pdfInputEl) {
  pdfInputEl.addEventListener('change', () => {
    const file = pdfInputEl.files?.[0];
    pdfPending = file ? { name: file.name } : null;
    refreshPdfUploadUi();
  });
}

const uploadBtn = document.getElementById('upload-btn');
if (uploadBtn) {
  uploadBtn.addEventListener('click', async () => {
    const input = document.getElementById('pdf-input');
    if (!input?.files?.length) {
      toast('请先选择 PDF 文件');
      return;
    }
    const file = input.files[0];
    const sizeMb = (file.size / 1024 / 1024).toFixed(1);
    const fd = new FormData();
    fd.append('pdf', file);
    uploadBtn.disabled = true;
    uploadBtn.classList.remove('pdf-upload-btn-ready');
    uploadBtn.textContent = `上传中… ${sizeMb} MB`;
    setPdfUploadHint(hintWithDot('cw-zip-pending', `正在上传 PDF（${sizeMb} MB）…`));
    try {
      const r = await fetch(`${API_PREFIX}/volumes/${encodeURIComponent(VOLUME_CODE)}/pdf`, {
        method: 'POST',
        body: fd,
      });
      const d = await readJsonResponse(r);
      if (!r.ok || !d.ok) throw new Error(d.error || `上传失败（HTTP ${r.status}）`);
      pdfPending = null;
      input.value = '';
      flashPdfUpload('PDF 已上传');
      toast('PDF 已上传');
      await loadVolume();
    } catch (e) {
      setPdfUploadHint(
        `<span class="pdf-upload-error">${escapeHtml(e.message || String(e))}</span>`,
      );
      toast(e.message || String(e));
      refreshPdfUploadUi();
    } finally {
      if (uploadBtn.textContent.startsWith('上传中')) {
        refreshPdfUploadUi();
      }
    }
  });
}

const preprocessBtnEl = document.getElementById('preprocess-btn');
if (preprocessBtnEl) {
  preprocessBtnEl.addEventListener('click', () => runPreprocessPipeline());
}

const analysisBatchBtnEl = document.getElementById('analysis-batch-btn');
if (analysisBatchBtnEl) {
  analysisBatchBtnEl.addEventListener('click', () => startAnalysisBatch());
}

const analysisBatchCancelBtnEl = document.getElementById('analysis-batch-cancel-btn');
if (analysisBatchCancelBtnEl) {
  analysisBatchCancelBtnEl.addEventListener('click', () => cancelAnalysisBatch());
}

const blockAnchorBatchBtnEl = document.getElementById('block-anchor-batch-btn');
if (blockAnchorBatchBtnEl) {
  blockAnchorBatchBtnEl.addEventListener('click', () => startBlockBatch());
}

const blockAnchorBatchCancelBtnEl = document.getElementById('block-anchor-batch-cancel-btn');
if (blockAnchorBatchCancelBtnEl) {
  blockAnchorBatchCancelBtnEl.addEventListener('click', () => cancelBlockBatch());
}

loadVolume().catch((e) => {
  document.getElementById('page-sub').textContent = e.message;
});

function bindIntakeReturnFocus() {
  window.addEventListener('storage', (ev) => {
    if (ev.key !== INTAKE_FOCUS_KEY || !ev.newValue) return;
    window.focus();
    loadVolume().catch(() => {});
  });
}

bindIntakeReturnFocus();

function previewGridLayoutClass(pageCount) {
  if (pageCount <= 1) return 'preview-grid preview-grid--one';
  if (pageCount === 2) return 'preview-grid preview-grid--two';
  return 'preview-grid';
}

function bindPreviewImageZoom(grid) {
  grid.querySelectorAll('.preview-card img').forEach((img) => {
    img.addEventListener('click', () => {
      img.classList.toggle('is-zoomed');
    });
  });
}

function renderPreviewGrid(d) {
  const grid = document.getElementById('preview-grid');
  const title = document.getElementById('preview-title');
  title.textContent = `${d.lesson_name}（PDF ${d.page_start}–${d.page_end}）`;
  grid.className = previewGridLayoutClass(d.pages?.length || 0);
  if (!d.pages?.length) {
    grid.innerHTML = '<p class="preview-empty">无页图。请检查页码范围。</p>';
    return;
  }
  const liveHint = d.preview
    ? '<p class="hint-inline">实时预览（未保存时按输入框页码从 PDF 渲染）· 点击图片可放大查看</p>'
    : '<p class="hint-inline">点击图片可放大查看</p>';
  grid.innerHTML = liveHint + d.pages.map((p) => `
    <div class="preview-card">
      <img src="${p.url}" alt="PDF p${p.pdf_page ?? p.page_index}">
      <div>PDF p${p.pdf_page ?? p.page_index}</div>
    </div>`).join('');
  bindPreviewImageZoom(grid);
}

async function openLessonPreview(lessonUid) {
  const grid = document.getElementById('preview-grid');
  const title = document.getElementById('preview-title');
  previewOpenLessonUid = lessonUid;
  openLessonPreviewShell();
  grid.className = 'preview-grid';
  grid.innerHTML = '<p class="preview-empty">加载中…</p>';
  title.textContent = lessonUid;

  const range = readPageRange(lessonUid);
  if (!range) {
    grid.innerHTML = '<p class="preview-empty">请先填写有效的起止页码。</p>';
    return;
  }

  try {
    const qs = new URLSearchParams({
      page_start: String(range.pageStart),
      page_end: String(range.pageEnd),
    });
    const r = await fetch(
      `${API_PREFIX}/lessons/${encodeURIComponent(lessonUid)}/pages?${qs}`,
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '加载页图失败');
    renderPreviewGrid(d);
  } catch (e) {
    grid.innerHTML = `<p class="preview-empty">${e.message}</p>`;
  }
}
