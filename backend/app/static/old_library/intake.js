const VOLUME_CODE = window.INTAKE_VOLUME_CODE;
const UI_SHELL = window.UI_SHELL || 'old_library';
const IS_LIBRARY_SHELL = UI_SHELL === 'textbook_library';

function librarySlotHref(data) {
  const ed = data?.edition_id;
  if (!ed) return '/textbook-library/';
  const g = data.grade;
  const term = data.term || data.semester || '上';
  return `/textbook-library/slot?edition=${encodeURIComponent(ed)}&grade=${encodeURIComponent(g)}&term=${encodeURIComponent(term)}`;
}

function annotateHrefForLesson(lessonUid) {
  return IS_LIBRARY_SHELL
    ? `/textbook-library/lessons/${encodeURIComponent(lessonUid)}/annotate`
    : `/old-library/lessons/${encodeURIComponent(lessonUid)}/annotate`;
}
const INTAKE_FOCUS_KEY = 'old-library-intake-focus';
const INTAKE_RELOAD_CHANNEL = 'old-library-intake-reload';

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

function lessonIsBlocksLocked(les) {
  return !!(les?.blocks_locked || les?.blocks_locked_at);
}

let previewOpenLessonUid = null;
let previewRefreshTimer = null;
let previewBackdropScrollY = 0;
let lastVolumeData = null;

/** @type {Map<string, { slides: number, at: number, timer?: ReturnType<typeof setTimeout> }>} */
const cwUploadFlash = new Map();
/** @type {Map<string, { name: string, file: File }>} */
const cwZipPending = new Map();
const CW_UPLOAD_FLASH_MS = 15000;
const PDF_UPLOAD_FLASH_MS = 15000;

/** @type {{ name: string } | null} */
let pdfPending = null;
/** @type {ReturnType<typeof setTimeout> | null} */
let pdfUploadFlashTimer = null;
/** @type {ReturnType<typeof setInterval> | null} */
let parseHintTimer = null;

const PARSE_BUSY_HINTS = [
  '正在读取 PDF 结构…',
  '正在匹配各课起始页码…',
  '正在生成课时页图…',
  '扫描版 PDF 较慢，请耐心等待，勿关闭页面…',
];

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

function flashPdfUpload(message) {
  if (pdfUploadFlashTimer) clearTimeout(pdfUploadFlashTimer);
  setPdfUploadHint(hintWithDot('cw-upload-flash', escapeHtml(message)));
  pdfUploadFlashTimer = setTimeout(() => {
    pdfUploadFlashTimer = null;
    refreshPdfUploadUi();
  }, PDF_UPLOAD_FLASH_MS);
}

function setParseHint(html) {
  const el = document.getElementById('parse-hint');
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

function showParseBusy() {
  let idx = 0;
  setParseHint(hintWithDot('cw-zip-pending', PARSE_BUSY_HINTS[0]));
  clearParseHintTimer();
  parseHintTimer = setInterval(() => {
    idx = (idx + 1) % PARSE_BUSY_HINTS.length;
    setParseHint(hintWithDot('cw-zip-pending', PARSE_BUSY_HINTS[idx]));
  }, 4000);
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

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/"/g, '&quot;');
}

function cwZipPendingHtml(lessonUid) {
  const pending = cwZipPending.get(lessonUid);
  if (!pending) return '';
  const short = shortFileName(pending.name, 22);
  const full = escapeHtml(pending.name);
  return (
    `<span class="cw-zip-pending">` +
    `<span class="cw-upload-dot" aria-hidden="true"></span>` +
    `<span class="cw-pending-text">已选择 <span class="cw-pending-name" title="${full}">${escapeHtml(short)}</span></span>` +
    `</span>`
  );
}

function cwUploadFlashHtml(lessonUid) {
  const flash = cwUploadFlash.get(lessonUid);
  if (!flash || Date.now() - flash.at >= CW_UPLOAD_FLASH_MS) return '';
  return (
    `<span class="cw-upload-flash">` +
    `<span class="cw-upload-dot" aria-hidden="true"></span>` +
    `上传了 ${flash.slides} 张课件</span>`
  );
}

function annotateStatsHtml(les) {
  if (IS_LIBRARY_SHELL) {
    let base = `${les.atom_count || 0} 原子 · ${les.block_count || 0} 区块`;
    if (lessonIsBlocksLocked(les)) base += ' · 已保存';
    return `<span class="annotate-stats-line">${base}</span>`;
  }
  const uid = les.lesson_uid;
  const flash = cwUploadFlashHtml(uid);
  const pending = flash ? '' : cwZipPendingHtml(uid);
  let base = `${les.slide_count || 0} 课件 · ${les.block_count || 0} 区块`;
  if (lessonIsBlocksLocked(les)) base += ' · 已保存';
  const hint = flash || pending;
  let html = `<span class="annotate-stats-line">${base}</span>`;
  if (hint) html += `<span class="annotate-stats-hint">${hint}</span>`;
  return html;
}

/** 按是否已上传 / 是否已选 ZIP 同步「确认上传」按钮样式 */
function syncCwUploadButtonState(lessonUid) {
  const uploadBtn = document.querySelector(
    `button.cw-upload-btn[data-lesson="${lessonUid}"]`,
  );
  if (!uploadBtn) return;
  const input = document.querySelector(
    `.cw-zip-input[data-lesson="${lessonUid}"]`,
  );
  const pending = cwZipPending.get(lessonUid);
  const file = input?.files?.[0] || pending?.file;
  const les = lastVolumeData?.lessons?.find((l) => l.lesson_uid === lessonUid);
  const hasCw = (les?.slide_count || 0) > 0;

  uploadBtn.classList.remove('cw-upload-btn-idle', 'cw-upload-btn-ready', 'cw-upload-btn-done');
  if (file) {
    uploadBtn.classList.add('cw-upload-btn-ready');
    uploadBtn.disabled = false;
    uploadBtn.textContent = '确认上传';
    return;
  }
  uploadBtn.disabled = true;
  uploadBtn.textContent = hasCw ? '已上传' : '确认上传';
  uploadBtn.classList.add(hasCw ? 'cw-upload-btn-done' : 'cw-upload-btn-idle');
}

function rememberCwUploadSuccess(lessonUid, slideCount) {
  const prev = cwUploadFlash.get(lessonUid);
  if (prev?.timer) clearTimeout(prev.timer);
  const timer = setTimeout(() => {
    cwUploadFlash.delete(lessonUid);
    refreshAnnotateStatsCell(lessonUid);
  }, CW_UPLOAD_FLASH_MS);
  cwUploadFlash.set(lessonUid, {
    slides: slideCount,
    at: Date.now(),
    timer,
  });
}

function refreshAnnotateStatsCell(lessonUid) {
  const les = lastVolumeData?.lessons?.find((l) => l.lesson_uid === lessonUid);
  const cell = document.querySelector(
    `tr[data-lesson-row="${lessonUid}"] .annotate-stats`,
  );
  if (!les || !cell) return;
  cell.innerHTML = annotateStatsHtml(les);
}

function flashAnnotateStatsCell(lessonUid, slideCount) {
  const les = lastVolumeData?.lessons?.find((l) => l.lesson_uid === lessonUid);
  const cell = document.querySelector(
    `tr[data-lesson-row="${lessonUid}"] .annotate-stats`,
  );
  if (!cell) return;
  const base = les
    ? `${les.slide_count || 0} 课件 · ${les.block_count || 0} 区块`
    : `${slideCount} 课件 · — 区块`;
  cell.innerHTML =
    `<span class="annotate-stats-line">${base}</span>` +
    `<span class="annotate-stats-hint">` +
    `<span class="cw-upload-flash">` +
    `<span class="cw-upload-dot" aria-hidden="true"></span>` +
    `上传了 ${slideCount} 张课件</span>` +
    `</span>`;
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
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

/** 第一个未完成步骤的下标（与进度位置一致，不依赖后端 active_index） */
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

  walker.classList.toggle('is-idle', !allDone && ratio <= 0 && !busy);
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

function oldLibraryWorkbenchHref(editionId) {
  if (IS_LIBRARY_SHELL) return '/textbook-library/';
  if (!editionId) return '/old-library/';
  return `/old-library/?edition=${encodeURIComponent(editionId)}`;
}

function libraryPipelineLabel(key, fallback) {
  if (!IS_LIBRARY_SHELL) return fallback;
  const map = {
    pdf_upload: '整册 PDF 上传',
    lesson_split: '课次拆分',
    block_build: '区块建立',
  };
  return map[key] || String(fallback || '').replace(/^旧/, '').replace(/旧/g, '');
}

/** 教材库：去掉课件步；区块完成度不依赖课件 slides。 */
function adaptPipelineForLibraryShell(pipeline, lessons) {
  if (!pipeline?.steps?.length) return pipeline;
  const list = lessons || [];
  const n = list.length;
  const withBlocks = list.filter((l) => (l.block_count || 0) > 0).length;
  const locked = list.filter(
    (l) => l.blocks_locked && (l.block_count || 0) > 0,
  ).length;
  const steps = pipeline.steps
    .filter((s) => s.key !== 'courseware_upload')
    .map((s) => {
      const label = libraryPipelineLabel(s.key, s.label);
      if (s.key !== 'block_build') return { ...s, label };
      const done = n > 0 && withBlocks >= n;
      return {
        ...s,
        label,
        done,
        current: withBlocks,
        total: n,
        progress: done || n <= 0 ? null : `${withBlocks}/${n} 课`,
        blocks_locked_count: locked,
        ratio: n <= 0 ? 0 : Math.min(1, withBlocks / n),
      };
    });
  return {
    ...pipeline,
    steps,
    all_done: steps.length > 0 && steps.every((s) => s.done),
  };
}

function applyLibraryShellChrome() {
  if (!IS_LIBRARY_SHELL) return;
  document.body.classList.add('lib-shell-no-cw');
  const hint = document.querySelector('.intake-lessons-hint');
  if (hint) hint.textContent = '页码校对 · OCR / 建块';
  const cwTrigger = document.getElementById('cw-batch-trigger');
  if (cwTrigger) cwTrigger.hidden = true;
  const cwPanel = document.getElementById('cw-batch-panel');
  if (cwPanel) cwPanel.hidden = true;
  const batchBtn = document.getElementById('block-batch-run-btn');
  if (batchBtn && !blockBatchRunning) {
    batchBtn.textContent = '整册 OCR / 建块';
    batchBtn.title = '对本册各课：文字 OCR → 整理 → 建块 → 核对保存';
  }
  const modalTitle = document.getElementById('block-batch-modal-title');
  if (modalTitle) modalTitle.textContent = '整册 OCR / 建块';
}

function renderVolume(data) {
  lastVolumeData = data;
  applyLibraryShellChrome();
  if (IS_LIBRARY_SHELL && data.pipeline) {
    data.pipeline = adaptPipelineForLibraryShell(data.pipeline, data.lessons);
  }
  renderVolumePipeline(data);
  syncBlockBatchBar();
  const back = document.getElementById('intake-back-link');
  if (back) {
    if (IS_LIBRARY_SHELL) {
      back.textContent = '← 册次格';
      back.href = librarySlotHref(data);
    } else {
      back.href = oldLibraryWorkbenchHref(data.edition_id);
    }
  }
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
    const rows = [
      ['存储', `已绑定 PDF（${storage} · blob ${data.blob_id?.slice(0, 8)}…）`],
      data.blob_size_bytes ? ['大小', formatBytes(data.blob_size_bytes)] : null,
      ['镜像', data.mirror_path || '未写入（可检查目录权限）'],
    ].filter(Boolean);
    meta.innerHTML = rows.map(([label, val]) =>
      `<div class="intake-info-row"><span class="label">${label}</span><span>${escapeHtml(String(val))}</span></div>`,
    ).join('');
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
    return `<tr data-lesson-row="${les.lesson_uid}">
      <td>${les.unit_title}</td>
      <td class="lesson-title-cell">
        <div class="lesson-reorder">
          <span class="lesson-move-btns" aria-label="调整顺序">
            <button type="button" class="btn-icon lesson-move-btn" data-lesson="${les.lesson_uid}" data-dir="up" title="上移" ${canUp ? '' : 'disabled'}>▲</button>
            <button type="button" class="btn-icon lesson-move-btn" data-lesson="${les.lesson_uid}" data-dir="down" title="下移" ${canDown ? '' : 'disabled'}>▼</button>
          </span>
          <span class="lesson-title-text">${les.lesson_no} ${les.lesson_name}</span>
        </div>
      </td>
      <td><code>${les.old_course_id || '—'}</code></td>
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
      <td>${les.lesson_page_count ? `<span class="intake-meta-chip intake-meta-chip--table">${les.lesson_page_count} 张已存</span>` : (hasRange ? '<span class="page-pending-badge">未生成</span>' : '—')}</td>
      <td class="annotate-cell">
        <span class="annotate-stats">${annotateStatsHtml(les)}</span>
        ${lessonPipelineHtml(les)}
        <div class="annotate-actions">
          ${IS_LIBRARY_SHELL ? '' : (
            `<label class="btn btn-sm secondary zip-inline">` +
            `<input type="file" accept=".zip,application/zip" class="cw-zip-input" data-lesson="${les.lesson_uid}">` +
            `选择课件 ZIP` +
            `</label>` +
            `<button type="button" class="btn btn-sm cw-upload-btn cw-upload-btn-idle" data-lesson="${les.lesson_uid}" disabled>确认上传</button>`
          )}
          ${annotateLinkHtml(les.lesson_uid)}
        </div>
      </td>
    </tr>`;
  }).join('');

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

  tbody.querySelectorAll('.cw-zip-input').forEach((input) => {
    const lessonUid = input.dataset.lesson;
    input.addEventListener('change', () => {
      const file = input.files?.[0];
      if (file) {
        cwZipPending.set(lessonUid, { name: file.name, file });
      } else {
        cwZipPending.delete(lessonUid);
      }
      syncCwUploadButtonState(lessonUid);
      refreshAnnotateStatsCell(lessonUid);
    });
    syncCwUploadButtonState(lessonUid);
  });
  cwZipPending.forEach((_pending, lessonUid) => {
    syncCwUploadButtonState(lessonUid);
    refreshAnnotateStatsCell(lessonUid);
  });
  tbody.querySelectorAll('.cw-upload-btn').forEach((btn) => {
    btn.onclick = () => uploadCoursewareZip(btn.dataset.lesson);
  });

  document.getElementById('lessons-card').hidden = !data.lessons.length;

  const parseBtn = document.getElementById('parse-btn');
  if (parseBtn) parseBtn.disabled = !data.has_pdf;

  const parseMeta = document.getElementById('parse-meta');
  if (parseMeta) {
    if (data.parse_status === 'failed' && data.parse_error) {
      parseMeta.hidden = false;
      parseMeta.textContent = `上次失败：${data.parse_error}`;
    } else {
      parseMeta.hidden = true;
      parseMeta.textContent = '';
    }
  }
  refreshPdfUploadUi();

  const btnBoot = document.getElementById('btn-library-bootstrap');
  if (btnBoot) {
    const show = IS_LIBRARY_SHELL
      && data.subject === '科学'
      && !!data.has_old_benchmark
      && data.edition_id;
    btnBoot.hidden = !show;
    btnBoot.style.display = show ? '' : 'none';
    if (!show) {
      btnBoot.onclick = null;
    }
    if (show) {
      btnBoot.onclick = async () => {
        btnBoot.disabled = true;
        try {
          const t = String(data.term || data.semester || '').includes('下') ? 'X' : 'S';
          const url = `/api/old-library/volumes/${encodeURIComponent(data.edition_id)}/${data.grade}/${t}/bootstrap`;
          const r = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: '{}',
          });
          const d = await readJsonResponse(r);
          if (!r.ok || d.ok === false) throw new Error(d.error || '载入失败');
          toast('基准目录已载入');
          await loadVolume();
        } catch (e) {
          toast(e.message || String(e));
        } finally {
          btnBoot.disabled = false;
        }
      };
    } else {
      btnBoot.onclick = null;
    }
  }
}

function renderParseResult(d) {
  const el = document.getElementById('parse-meta');
  if (!el) return;
  if (!d.ok && d.parse_error) {
    el.hidden = false;
    el.innerHTML = `失败：${d.parse_error}`;
    return;
  }
  if (d.ok) {
    el.hidden = false;
    const unmatched = (d.parse_details || d.lessons || [])
      .filter((x) => x.matched === false)
      .map((x) => `${x.lesson_name || x.lesson_uid}`);
    el.innerHTML = [
      `已匹配 ${d.matched}/${d.lesson_count} 课`,
      d.lesson_pages_written ? `已生成 ${d.lesson_pages_written} 张页图` : '',
      d.text_mode ? `文本模式：${d.text_mode}` : '',
      d.total_pages ? `共 ${d.total_pages} 页` : '',
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

async function uploadCoursewareZip(lessonUid) {
  const input = document.querySelector(
    `.cw-zip-input[data-lesson="${lessonUid}"]`,
  );
  const btn = document.querySelector(
    `button.cw-upload-btn[data-lesson="${lessonUid}"]`,
  );
  const pending = cwZipPending.get(lessonUid);
  const file = input?.files?.[0] || pending?.file;
  if (!file) {
    toast('请先选择课件 ZIP');
    return;
  }
  const fd = new FormData();
  fd.append('zip', file);
  if (btn) {
    btn.disabled = true;
    btn.textContent = '上传中…';
  }
  try {
    const r = await fetch(
      `/api/old-library/lessons/${encodeURIComponent(lessonUid)}/courseware-zip`,
      { method: 'POST', body: fd },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '上传失败');
    const written = Number(d.slides_written) || 0;
    cwZipPending.delete(lessonUid);
    rememberCwUploadSuccess(lessonUid, written);
    flashAnnotateStatsCell(lessonUid, written);
    toast(d.warning || `上传成功：${written} 张课件`);
    if (input) input.value = '';
    syncCwUploadButtonState(lessonUid);
    await loadVolume();
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn && btn.textContent === '上传中…') {
      btn.textContent = '确认上传';
      syncCwUploadButtonState(lessonUid);
    }
  }
}

async function moveLesson(lessonUid, direction) {
  const btn = document.querySelector(
    `.lesson-move-btn[data-lesson="${lessonUid}"][data-dir="${direction}"]`,
  );
  if (btn) btn.disabled = true;
  try {
    const r = await fetch(
      `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/lessons/${encodeURIComponent(lessonUid)}/reorder`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ direction }),
      },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '调整顺序失败');
    renderVolume(d);
    toast(direction === 'up' ? '已上移' : '已下移');
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function loadVolume() {
  const r = await fetch(
    `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}?_=${Date.now()}`,
    { cache: 'no-store', headers: { Accept: 'application/json' } },
  );
  const d = await readJsonResponse(r);
  if (!r.ok || !d.ok) throw new Error(d.error || `加载失败（HTTP ${r.status}）`);
  renderVolume(d);
  await syncBlockBatchJobFromServer(false);
  return d;
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
      `/api/old-library/lessons/${encodeURIComponent(lessonUid)}/page-range`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          page_start: range.pageStart,
          page_end: range.pageEnd,
          rebuild_pages: true,
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
      const r = await fetch(`/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/pdf`, {
        method: 'POST',
        body: fd,
      });
      const d = await readJsonResponse(r);
      if (!r.ok || !d.ok) throw new Error(d.error || `上传失败（HTTP ${r.status}）`);
      const reused = d.blob_reused ? '（内容未变，复用已有 blob）' : '';
      pdfPending = null;
      input.value = '';
      flashPdfUpload(`PDF 已上传${reused}`);
      toast(`PDF 已上传 ${reused}`);
      renderVolume(d);
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

const parseBtn = document.getElementById('parse-btn');
if (parseBtn) {
  parseBtn.addEventListener('click', async () => {
    const btn = document.getElementById('parse-btn');
    btn.disabled = true;
    btn.textContent = '解析中…（扫描版可能需数分钟）';
    showParseBusy();
    try {
      const r = await fetch(`/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/parse`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const d = await readJsonResponse(r);
      if (!d.ok) {
        renderParseResult(d);
        throw new Error(d.error || d.parse_error || `解析失败（HTTP ${r.status}）`);
      }
      const summary = `解析已完成 · 已匹配 ${d.matched}/${d.lesson_count} 课 · 生成 ${d.lesson_pages_written || 0} 张页图`;
      toast(summary);
      flashParseSuccess(summary);
      renderParseResult(d);
      await loadVolume();
    } catch (e) {
      showParseError(e.message || String(e));
      toast(e.message || String(e));
    } finally {
      btn.disabled = false;
      btn.textContent = '开始解析';
    }
  });
}

/** @type {File[]} */
let cwBatchFiles = [];
/** @type {Array<{ filename: string, lesson_uid: string, score?: number, reason?: string, manual?: boolean }>} */
let cwBatchPlan = [];

/** @type {Map<string, { percent: number, state: string, label: string }>} */
const cwBatchProgress = new Map();
let cwBatchUploading = false;

/** @type {Map<string, { percent: number, state: string, label: string }>} */
const blockBatchProgress = new Map();
let blockBatchRunning = false;
/** @type {ReturnType<typeof setTimeout> | null} */
let blockBatchPollTimer = null;
let blockBatchLastFinishedAt = null;

function cwBatchProgressLabel(state, percent, customLabel) {
  if (customLabel) return customLabel;
  if (state === 'skip') return '跳过';
  if (state === 'done') return '完成';
  if (state === 'error') return '失败';
  if (state === 'uploading') return percent >= 100 ? '处理中…' : `上传 ${percent}%`;
  return '待上传';
}

function cwBatchProgressHtml(filename, plan) {
  const saved = cwBatchProgress.get(filename);
  const state = saved?.state || (plan.lesson_uid ? 'pending' : 'skip');
  const percent = saved?.percent ?? (state === 'done' ? 100 : 0);
  const label = cwBatchProgressLabel(state, percent, saved?.label);
  const stateClass = `cw-batch-progress--${state}`;
  return (
    `<td class="cw-batch-progress-cell">` +
    `<div class="cw-batch-progress ${stateClass}" role="progressbar" ` +
    `aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}" ` +
    `data-progress-file="${escapeHtml(filename)}">` +
    `<div class="cw-batch-progress-bar" style="width:${percent}%"></div>` +
    `</div>` +
    `<span class="cw-batch-progress-label ${stateClass}">${escapeHtml(label)}</span>` +
    `</td>`
  );
}

function updateCwBatchProgressRow(filename, patch) {
  const prev = cwBatchProgress.get(filename) || { percent: 0, state: 'pending', label: '' };
  const next = {
    percent: patch.percent ?? prev.percent,
    state: patch.state ?? prev.state,
    label: patch.label ?? cwBatchProgressLabel(
      patch.state ?? prev.state,
      patch.percent ?? prev.percent,
      patch.label,
    ),
  };
  cwBatchProgress.set(filename, next);

  const track = document.querySelector(
    `.cw-batch-progress[data-progress-file="${CSS.escape(filename)}"]`,
  );
  const labelEl = track?.closest('.cw-batch-progress-cell')?.querySelector('.cw-batch-progress-label');
  if (!track) return;

  track.className = `cw-batch-progress cw-batch-progress--${next.state}`;
  track.setAttribute('aria-valuenow', String(next.percent));
  const bar = track.querySelector('.cw-batch-progress-bar');
  if (bar) bar.style.width = `${next.percent}%`;
  if (labelEl) {
    labelEl.className = `cw-batch-progress-label cw-batch-progress--${next.state}`;
    labelEl.textContent = next.label;
  }
}

function uploadCoursewareZipWithProgress(lessonUid, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(
      'POST',
      `/api/old-library/lessons/${encodeURIComponent(lessonUid)}/courseware-zip`,
    );
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) return;
      const pct = Math.min(99, Math.round((ev.loaded / ev.total) * 100));
      onProgress(pct);
    };
    xhr.onload = () => {
      let d;
      try {
        d = JSON.parse(xhr.responseText || '{}');
      } catch {
        reject(new Error(`服务器返回异常（HTTP ${xhr.status}）`));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300 && d.ok) {
        onProgress(100);
        resolve(d);
      } else {
        reject(new Error(d.error || `上传失败（HTTP ${xhr.status}）`));
      }
    };
    xhr.onerror = () => reject(new Error('网络错误'));
    const fd = new FormData();
    fd.append('zip', file);
    xhr.send(fd);
  });
}

function lessonOptionsHtml(selectedUid) {
  const lessons = lastVolumeData?.lessons || [];
  return lessons
    .filter((les) => les.old_course_id)
    .map((les) => {
      const label = `${les.unit_title} · ${les.lesson_no} ${les.lesson_name}`;
      const sel = les.lesson_uid === selectedUid ? ' selected' : '';
      return `<option value="${escapeHtml(les.lesson_uid)}"${sel}>${escapeHtml(label)}</option>`;
    })
    .join('');
}

function syncCwBatchBar() {
  const previewBtn = document.getElementById('cw-batch-preview-btn');
  const uploadBtn = document.getElementById('cw-batch-upload-btn');
  const batchInput = document.getElementById('cw-batch-input');
  const trigger = document.getElementById('cw-batch-trigger');
  const hasFiles = cwBatchFiles.length > 0;
  const matched = cwBatchPlan.some((p) => p.lesson_uid);
  if (previewBtn) previewBtn.disabled = !hasFiles || cwBatchUploading;
  if (uploadBtn) {
    uploadBtn.disabled = !hasFiles || !matched || cwBatchUploading;
  }
  const batchBusy = cwBatchUploading || blockBatchRunning;
  if (batchInput) batchInput.disabled = batchBusy;
  if (trigger) trigger.classList.toggle('is-disabled', batchBusy);
  syncBlockBatchBar();
}

function renderCwBatchPanel() {
  const panel = document.getElementById('cw-batch-panel');
  const tbody = document.getElementById('cw-batch-rows');
  const summary = document.getElementById('cw-batch-summary');
  if (!panel || !tbody) return;

  if (!cwBatchFiles.length) {
    panel.hidden = true;
    cwBatchPlan = [];
    cwBatchProgress.clear();
    syncCwBatchBar();
    return;
  }

  panel.hidden = false;
  const matched = cwBatchPlan.filter((p) => p.lesson_uid).length;
  if (summary) {
    summary.textContent = `已选 ${cwBatchFiles.length} 个 ZIP · 已匹配 ${matched} 课`;
  }

  const planByName = new Map(cwBatchPlan.map((p) => [p.filename, p]));
  tbody.innerHTML = cwBatchFiles.map((file, idx) => {
    const plan = planByName.get(file.name) || { filename: file.name, lesson_uid: '' };
    const scorePct = plan.score != null ? `${Math.round(plan.score * 100)}%` : '—';
    const status = plan.lesson_uid
      ? (plan.manual ? '手动指定' : (plan.reason || '自动匹配'))
      : '未匹配';
    const statusClass = plan.lesson_uid ? 'cw-batch-ok' : 'cw-batch-warn';
    const selectDisabled = cwBatchUploading ? ' disabled' : '';
    return `<tr data-batch-idx="${idx}" data-batch-filename="${escapeHtml(file.name)}">
      <td class="cw-batch-filename" title="${escapeHtml(file.name)}">${escapeHtml(shortFileName(file.name, 36))}</td>
      <td>
        <select class="cw-batch-lesson-select" data-filename="${escapeHtml(file.name)}"${selectDisabled}>
          <option value="">— 未匹配 —</option>
          ${lessonOptionsHtml(plan.lesson_uid)}
        </select>
      </td>
      <td>${scorePct}</td>
      <td class="${statusClass}">${escapeHtml(status)}</td>
      ${cwBatchProgressHtml(file.name, plan)}
    </tr>`;
  }).join('');

  tbody.querySelectorAll('.cw-batch-lesson-select').forEach((sel) => {
    sel.addEventListener('change', () => {
      if (cwBatchUploading) return;
      const filename = sel.dataset.filename;
      const lessonUid = sel.value;
      const existing = cwBatchPlan.find((p) => p.filename === filename);
      if (lessonUid) {
        const row = existing || { filename, score: 0, reason: '手动指定' };
        row.lesson_uid = lessonUid;
        row.manual = true;
        if (!existing) cwBatchPlan.push(row);
        else Object.assign(existing, row);
      } else if (existing) {
        existing.lesson_uid = '';
      }
      syncCwBatchBar();
      renderCwBatchPanel();
    });
  });
  syncCwBatchBar();
}

async function previewCwBatchMatch() {
  if (!cwBatchFiles.length) {
    toast('请先点「整册上传 ZIP」选择文件');
    return;
  }
  const btn = document.getElementById('cw-batch-preview-btn');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '匹配中…';
  }
  try {
    const r = await fetch(
      `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/courseware-zip-batch/preview`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filenames: cwBatchFiles.map((f) => f.name) }),
      },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '匹配失败');

    cwBatchPlan = (d.matches || []).map((m) => ({
      filename: m.filename,
      lesson_uid: m.lesson_uid,
      score: m.score,
      reason: m.reason,
      manual: false,
    }));
    cwBatchProgress.clear();
    const matchedNames = new Set(cwBatchPlan.map((p) => p.filename));
    for (const row of d.unmatched || []) {
      if (matchedNames.has(row.filename)) continue;
      const sug = row.suggestions?.[0];
      cwBatchPlan.push({
        filename: row.filename,
        lesson_uid: sug?.lesson_uid || '',
        score: sug?.score,
        reason: sug ? `建议：${sug.reason}` : '未匹配',
        manual: false,
      });
    }
    renderCwBatchPanel();
    const n = cwBatchPlan.filter((p) => p.lesson_uid).length;
    toast(`匹配完成：${n}/${cwBatchFiles.length} 个 ZIP 已对应课时，请核对后确认上传`);
  } catch (e) {
    toast(e.message || String(e));
  } finally {
    if (btn) {
      btn.textContent = '预览匹配';
      syncCwBatchBar();
    }
  }
}

async function uploadCwBatch() {
  const tasks = cwBatchFiles.map((file) => {
    const row = cwBatchPlan.find((p) => p.filename === file.name);
    return { file, row };
  }).filter((t) => t.row?.lesson_uid);

  if (!tasks.length) {
    toast('没有可上传的匹配项，请先预览匹配或手动选择课时');
    return;
  }

  const btn = document.getElementById('cw-batch-upload-btn');
  const previewBtn = document.getElementById('cw-batch-preview-btn');
  cwBatchUploading = true;
  syncCwBatchBar();
  if (btn) btn.textContent = '上传中…';

  let okCount = 0;
  let errCount = 0;

  for (const file of cwBatchFiles) {
    const row = cwBatchPlan.find((p) => p.filename === file.name);
    if (!row?.lesson_uid) {
      updateCwBatchProgressRow(file.name, { state: 'skip', percent: 0, label: '跳过' });
      continue;
    }
    updateCwBatchProgressRow(file.name, { state: 'uploading', percent: 0, label: '上传 0%' });
    try {
      const d = await uploadCoursewareZipWithProgress(
        row.lesson_uid,
        file,
        (pct) => updateCwBatchProgressRow(file.name, {
          state: 'uploading',
          percent: pct,
          label: pct >= 100 ? '处理中…' : `上传 ${pct}%`,
        }),
      );
      updateCwBatchProgressRow(file.name, { state: 'done', percent: 100, label: '完成' });
      rememberCwUploadSuccess(row.lesson_uid, Number(d.slides_written) || 0);
      okCount += 1;
    } catch (e) {
      updateCwBatchProgressRow(file.name, {
        state: 'error',
        percent: 100,
        label: (e.message || '失败').slice(0, 24),
      });
      errCount += 1;
    }
  }

  cwBatchUploading = false;
  if (btn) btn.textContent = '确认上传';
  if (previewBtn) previewBtn.textContent = '预览匹配';
  syncCwBatchBar();

  let msg = `课件上传完成：成功 ${okCount} 课`;
  if (errCount) msg += `，失败 ${errCount}`;
  toast(msg);

  if (okCount > 0) {
    await loadVolume();
  }
  if (errCount === 0 && okCount > 0) {
    cwBatchFiles = [];
    cwBatchPlan = [];
    cwBatchProgress.clear();
    const input = document.getElementById('cw-batch-input');
    if (input) input.value = '';
    renderCwBatchPanel();
  }
}

function bindCwBatchUpload() {
  const input = document.getElementById('cw-batch-input');
  const previewBtn = document.getElementById('cw-batch-preview-btn');
  const uploadBtn = document.getElementById('cw-batch-upload-btn');
  const closeBtn = document.getElementById('cw-batch-close');

  if (input) {
    input.addEventListener('change', () => {
      if (cwBatchUploading) return;
      cwBatchFiles = Array.from(input.files || []);
      cwBatchPlan = [];
      cwBatchProgress.clear();
      renderCwBatchPanel();
      if (cwBatchFiles.length) {
        previewCwBatchMatch();
      }
    });
  }
  if (previewBtn) previewBtn.onclick = () => previewCwBatchMatch();
  if (uploadBtn) uploadBtn.onclick = () => uploadCwBatch();
  if (closeBtn) {
    closeBtn.onclick = () => {
      if (cwBatchUploading) return;
      cwBatchFiles = [];
      cwBatchPlan = [];
      cwBatchProgress.clear();
      if (input) input.value = '';
      renderCwBatchPanel();
    };
  }
  syncCwBatchBar();
}

bindCwBatchUpload();

/** 单课串行整册建块（湘科四下默认；其它册次可用 env 覆盖） */
const SEQUENTIAL_BLOCK_BATCH_VOLUMES = new Set(['XK-4X-OLD']);

function blockBatchPipelineLabel(strategy) {
  return strategy === 'sequential' ? '单课串行' : '三阶段并行';
}

function formatBlockBatchDuration(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return '';
  if (s < 60) return `${Math.round(s)} 秒`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return r ? `${m} 分 ${r} 秒` : `${m} 分`;
}

function resolveBlockBatchPipelineForVolume() {
  return SEQUENTIAL_BLOCK_BATCH_VOLUMES.has(VOLUME_CODE) ? 'sequential' : 'parallel';
}

function defaultBlockBatchProgress() {
  return {
    state: 'pending',
    label: '排队中…',
    ocr: 'pending',
    curate: 'pending',
    seed: 'pending',
    ocrPage: 0,
    ocrTotal: 0,
  };
}

function blockBatchSegClass(status) {
  if (status === 'running' || status === 'partial') return 'is-running';
  if (status === 'done') return 'is-done';
  if (status === 'skip') return 'is-skip';
  if (status === 'error') return 'is-error';
  return 'is-pending';
}

const LESSON_PIPELINE_STEP_NAMES = {
  ocr: 'OCR',
  ocr_text: '文字OCR',
  ocr_image: '图片OCR',
  curate: '原子整理',
  prematch: '课件预匹配',
  seed: '建块',
  confirm: '确认',
};

const LEGACY_LESSON_PIPELINE_ORDER = ['ocr', 'curate', 'seed', 'confirm'];

function lessonPipelineStepOrder(meta) {
  const steps = meta?.steps || [];
  if (steps.length) return steps.map((step) => step.key);
  return LEGACY_LESSON_PIPELINE_ORDER;
}

function isDoubaoPipelineMeta(meta) {
  return meta?.variant === 'doubao'
    || (meta?.steps || []).some((step) => step.key === 'ocr_text');
}

function inferDoubaoBuildPipeline(les) {
  const pages = les.lesson_page_count || 0;
  const blocks = les.block_count || 0;
  const slides = les.slide_count || 0;
  const locked = lessonIsBlocksLocked(les);

  let textStatus = 'pending';
  let textDetail = pages ? `0/${pages} 页` : '待页图';
  let imageStatus = 'pending';
  let imageDetail = slides ? '待识别' : '—';
  if (blocks > 0) {
    textStatus = 'done';
    textDetail = pages ? `${pages} 页` : '已完成';
    imageStatus = 'done';
    imageDetail = '已完成';
  }

  let seedStatus = 'pending';
  let seedDetail = slides ? '待建块' : (IS_LIBRARY_SHELL ? '待建块' : '待课件');
  if (blocks > 0) {
    seedStatus = 'done';
    seedDetail = `${blocks} 区块`;
  } else if (textStatus !== 'done' || imageStatus !== 'done') {
    seedDetail = '待 OCR';
  }

  let confirmStatus = 'pending';
  let confirmDetail = '—';
  if (locked) {
    confirmStatus = 'done';
    confirmDetail = '已保存';
  } else if (blocks > 0) {
    confirmDetail = '待核对保存';
  }

  let summary = '待开始';
  if (locked) summary = '已完成并保存';
  else if (blocks > 0) summary = '待核对保存';
  else if (seedStatus === 'pending' && textStatus === 'done' && imageStatus === 'done') {
    summary = '待建块';
  } else if (textStatus !== 'done' || imageStatus !== 'done') summary = '待 OCR';

  return {
    variant: 'doubao',
    steps: [
      { key: 'ocr_text', label: '文字OCR', status: textStatus, detail: textDetail },
      { key: 'ocr_image', label: '图片OCR', status: imageStatus, detail: imageDetail },
      { key: 'seed', label: '建块', status: seedStatus, detail: seedDetail },
      { key: 'confirm', label: '确认', status: confirmStatus, detail: confirmDetail },
    ],
    summary,
  };
}

function inferLessonBuildPipeline(les) {
  const pages = les.lesson_page_count || 0;
  const atoms = les.atom_count || 0;
  const blocks = les.block_count || 0;
  const slides = les.slide_count || 0;
  const locked = lessonIsBlocksLocked(les);
  const mode = les.bootstrap_mode || 'full';

  let ocrStatus = 'pending';
  let ocrDetail = pages ? `0/${pages} 页` : '待页图';
  if (blocks > 0 || atoms > 0) {
    ocrStatus = 'done';
    ocrDetail = pages ? `${pages} 页` : '已完成';
  }

  let curateStatus = 'pending';
  let curateDetail = '待 OCR';
  if (blocks > 0 || mode === 'seed_only') {
    curateStatus = 'done';
    curateDetail = '已整理';
  } else if (ocrStatus === 'done' && mode === 'curate_and_seed') {
    curateDetail = '待整理';
  } else if (ocrStatus === 'done') {
    curateDetail = '待整理';
  }

  let seedStatus = 'pending';
  let seedDetail = slides ? '待建块' : (IS_LIBRARY_SHELL ? '待建块' : '待课件');
  if (blocks > 0) {
    seedStatus = 'done';
    seedDetail = `${blocks} 区块`;
  } else if (curateStatus !== 'done') {
    seedDetail = '待整理';
  }

  let confirmStatus = 'pending';
  let confirmDetail = '—';
  if (locked) {
    confirmStatus = 'done';
    confirmDetail = '已保存';
  } else if (blocks > 0) {
    confirmDetail = '待核对保存';
  }

  let summary = '待开始';
  if (locked) summary = '已完成并保存';
  else if (blocks > 0) summary = '待核对保存';
  else if (curateStatus === 'done') summary = '待 AI 建块';
  else if (ocrStatus === 'done') summary = '待原子整理';

  return {
    steps: [
      { key: 'ocr', label: 'OCR', status: ocrStatus, detail: ocrDetail },
      { key: 'curate', label: '原子整理', status: curateStatus, detail: curateDetail },
      { key: 'seed', label: 'AI建块', status: seedStatus, detail: seedDetail },
      { key: 'confirm', label: '确认', status: confirmStatus, detail: confirmDetail },
    ],
    summary,
  };
}

function reconcileLessonBuildPipeline(les, pipeline) {
  const steps = (pipeline?.steps || []).map((step) => ({ ...step }));
  const byKey = Object.fromEntries(steps.map((step) => [step.key, step]));
  const blocks = les.block_count || 0;
  const order = lessonPipelineStepOrder(pipeline);

  if (blocks > 0) {
    for (const key of order) {
      if (key === 'confirm') continue;
      const step = byKey[key] || { key, label: LESSON_PIPELINE_STEP_NAMES[key] || key };
      step.status = 'done';
      if (key === 'seed') step.detail = `${blocks} 区块`;
      else if (key === 'ocr_text' || key === 'ocr_image' || key === 'ocr') {
        step.detail = step.detail || '已完成';
      } else if (key === 'curate') step.detail = step.detail || '已整理';
      byKey[key] = step;
    }
  }

  const confirm = byKey.confirm || { key: 'confirm', label: '确认' };
  if (lessonIsBlocksLocked(les)) {
    confirm.status = 'done';
    confirm.detail = '已保存';
  } else if (blocks > 0) {
    confirm.status = confirm.status === 'done' ? 'done' : 'pending';
    confirm.detail = confirm.detail || '待核对保存';
  }
  byKey.confirm = confirm;

  let summary = pipeline?.summary || '';
  if (lessonIsBlocksLocked(les)) summary = '已完成并保存';
  else if (blocks > 0) summary = '待核对保存';

  return {
    variant: pipeline?.variant || (isDoubaoPipelineMeta(pipeline) ? 'doubao' : 'legacy'),
    steps: order.map((key) => byKey[key] || {
      key,
      label: LESSON_PIPELINE_STEP_NAMES[key] || key,
      status: 'pending',
      detail: '',
    }),
    summary,
    all_done: lessonIsBlocksLocked(les),
  };
}

function scrubLibraryCwCopy(text) {
  return String(text || '')
    .replace(/待课件/g, '待建块')
    .replace(/课件预匹配/g, '版面整理')
    .replace(/有课件/g, '有页图')
    .replace(/上传课件 ZIP/g, '完成解析')
    .replace(/课件/g, '教材');
}

function adaptPipelineMetaForLibraryShell(meta) {
  if (!IS_LIBRARY_SHELL || !meta) return meta;
  const steps = (meta.steps || [])
    .filter((s) => s.key !== 'prematch')
    .map((s) => ({
      ...s,
      label: scrubLibraryCwCopy(s.label || LESSON_PIPELINE_STEP_NAMES[s.key] || s.key),
      detail: scrubLibraryCwCopy(s.detail),
    }));
  return {
    ...meta,
    steps,
    summary: scrubLibraryCwCopy(meta.summary),
  };
}

function lessonBuildPipelineMeta(les) {
  const base = les.build_pipeline?.steps?.length
    ? les.build_pipeline
    : (IS_LIBRARY_SHELL ? inferLessonBuildPipeline(les) : inferDoubaoBuildPipeline(les));
  return adaptPipelineMetaForLibraryShell(reconcileLessonBuildPipeline(les, base));
}

function applyBlockBatchToPipelineStep(step, batch) {
  if (!batch) return step;
  const key = step.key;
  if (key === 'ocr' || key === 'ocr_text' || key === 'ocr_image') {
    const status = batch.ocr === 'skip' ? 'done' : (batch.ocr || step.status);
    return { ...step, status };
  }
  if (key === 'curate') {
    const status = batch.curate === 'skip' ? 'done' : (batch.curate || step.status);
    return { ...step, status };
  }
  if (key === 'seed') {
    const status = batch.seed === 'skip' ? 'done' : (batch.seed || step.status);
    return { ...step, status };
  }
  return step;
}

function resolveLessonPipelineSteps(les) {
  const meta = lessonBuildPipelineMeta(les);
  let order = lessonPipelineStepOrder(meta);
  if (IS_LIBRARY_SHELL) order = order.filter((key) => key !== 'prematch');
  const base = meta.steps || [];
  const byKey = Object.fromEntries(
    base.map((step) => [step.key, { ...step }]),
  );
  if (blockBatchRunning) {
    const batch = blockBatchProgress.get(les.lesson_uid);
    if (batch) {
      for (const key of order) {
        if (key === 'confirm') continue;
        const prev = byKey[key] || { key, label: LESSON_PIPELINE_STEP_NAMES[key] || key };
        byKey[key] = applyBlockBatchToPipelineStep(prev, batch);
      }
    }
  }
  return order.map((key) => {
    const step = byKey[key];
    if (step) return step;
    return {
      key,
      label: LESSON_PIPELINE_STEP_NAMES[key] || key,
      status: 'pending',
      detail: '',
    };
  });
}

function lessonPipelineFootLabel(les) {
  if (blockBatchRunning && blockBatchProgress.has(les.lesson_uid)) {
    const label = deriveBlockBatchLabel(blockBatchProgress.get(les.lesson_uid));
    if (label) return label;
  }
  return lessonBuildPipelineMeta(les).summary || '';
}

function lessonPipelineHtml(les) {
  const meta = lessonBuildPipelineMeta(les);
  const steps = resolveLessonPipelineSteps(les);
  const foot = lessonPipelineFootLabel(les);
  const batch = blockBatchProgress.get(les.lesson_uid);
  const rowClass = meta.all_done || batch?.state === 'done'
    ? ' block-batch-progress-row--done'
    : '';
  const stateClass = lessonIsBlocksLocked(les)
    ? ' cw-batch-progress--done'
    : (batch?.state ? ` cw-batch-progress--${batch.state}` : '');
  const cols = steps.map((step) => {
    const name = LESSON_PIPELINE_STEP_NAMES[step.key] || step.label || step.key;
    const title = step.detail ? ` title="${escapeHtml(step.detail)}"` : '';
    return (
      `<div class="block-batch-segcol"${title}>` +
      `<span class="block-batch-seg seg-${step.key} ${blockBatchSegClass(step.status)}"></span>` +
      `<span class="block-batch-seg-name ${blockBatchSegClass(step.status)}">${name}</span>` +
      `</div>`
    );
  }).join('');
  const footHtml = foot
    ? `<span class="cw-batch-progress-label${stateClass}">${escapeHtml(foot)}</span>`
    : '';
  const stepCount = steps.length || 4;
  const ariaSteps = steps.map((step) => LESSON_PIPELINE_STEP_NAMES[step.key] || step.label || step.key).join('、');
  return (
    `<div class="lesson-pipeline block-batch-progress-row${rowClass}" ` +
    `data-lesson-pipeline="${escapeHtml(les.lesson_uid)}">` +
    `<div class="block-batch-segwrap lesson-pipeline-segtrack" role="progressbar" ` +
    `aria-valuemin="0" aria-valuemax="${stepCount}" aria-label="${escapeHtml(ariaSteps)}">` +
    `<div class="block-batch-segtrack">${cols}</div>` +
    `</div>` +
    footHtml +
    `</div>`
  );
}

function refreshLessonPipelineDom(lessonUid) {
  const les = lastVolumeData?.lessons?.find((l) => l.lesson_uid === lessonUid);
  const cell = document.querySelector(
    `tr[data-lesson-row="${CSS.escape(lessonUid)}"] .annotate-cell`,
  );
  if (!les || !cell) return;
  const html = lessonPipelineHtml(les);
  const existing = cell.querySelector('[data-lesson-pipeline]');
  if (existing) {
    existing.outerHTML = html;
  } else {
    const stats = cell.querySelector('.annotate-stats');
    if (stats) stats.insertAdjacentHTML('afterend', html);
  }
}

function refreshAllLessonPipelines() {
  for (const les of lastVolumeData?.lessons || []) {
    refreshLessonPipelineDom(les.lesson_uid);
  }
}

function deriveBlockBatchLabel(data) {
  if (data.label && (data.state === 'skip' || data.state === 'error' || data.state === 'done')) {
    if (data.state === 'done' && data.elapsed_seconds != null) {
      const dur = formatBlockBatchDuration(data.elapsed_seconds);
      if (dur && !String(data.label).includes(dur)) {
        return `${data.label} · ${dur}`;
      }
    }
    return data.label;
  }
  if (data.ocr === 'running') {
    return data.ocrTotal
      ? `OCR ${data.ocrPage}/${data.ocrTotal} 页`
      : 'OCR…';
  }
  if (data.curate === 'running') return 'AI 整理…';
  if (data.seed === 'running') return '建块…';
  if (data.ocr === 'done' && data.curate === 'pending') return 'OCR 完成，等待整理';
  if (data.curate === 'done' && data.seed === 'pending') return 'OCR 完成，等待建块';
  if (data.ocr === 'done' && data.seed === 'pending') return 'OCR 完成，等待建块';
  if (data.state === 'pending') return '排队中…';
  return data.label || '';
}

function normalizeBlockBatchStages(data) {
  const out = { ...data };
  if (out.state === 'done') {
    if (out.ocr !== 'skip') out.ocr = 'done';
    if (out.curate !== 'skip') out.curate = 'done';
    out.seed = 'done';
  }
  return out;
}

function blockBatchSegColumn(key, name, status) {
  const cls = blockBatchSegClass(status);
  return (
    `<div class="block-batch-segcol">` +
    `<span class="block-batch-seg seg-${key} ${cls}"></span>` +
    `<span class="block-batch-seg-name ${cls}">${name}</span>` +
    `</div>`
  );
}

function blockBatchProgressHtml(lessonUid) {
  const les = lastVolumeData?.lessons?.find((l) => l.lesson_uid === lessonUid);
  if (!les || !blockBatchProgress.has(lessonUid)) return '';
  return lessonPipelineHtml(les);
}

function ensureBlockBatchProgressDom(lessonUid) {
  refreshLessonPipelineDom(lessonUid);
  return document.querySelector(
    `[data-lesson-pipeline="${CSS.escape(lessonUid)}"]`,
  );
}

function updateBlockBatchProgress(lessonUid, patch) {
  const prev = blockBatchProgress.get(lessonUid) || defaultBlockBatchProgress();
  let next = { ...prev, ...patch };
  next = normalizeBlockBatchStages(next);
  if (patch.label === undefined) {
    next.label = deriveBlockBatchLabel(next);
  }
  blockBatchProgress.set(lessonUid, next);
  refreshLessonPipelineDom(lessonUid);
}

function annotateLinkLabelForLesson(lessonUid) {
  const les = lastVolumeData?.lessons?.find((l) => l.lesson_uid === lessonUid);
  const prog = blockBatchProgress.get(lessonUid);
  if (blockBatchRunning && prog?.state === 'done') return '核对';
  if ((les?.block_count || 0) > 0 && !lessonIsBlocksLocked(les)) return '核对';
  return '建块';
}

function annotateLinkHtml(lessonUid) {
  const prog = blockBatchProgress.get(lessonUid);
  const batch = blockBatchRunning;
  const done = prog?.state === 'done';
  const label = annotateLinkLabelForLesson(lessonUid);
  const cls = [
    'btn btn-sm btn-annotate',
    batch ? 'btn-annotate--batch' : '',
    batch && done ? 'btn-annotate--ready' : '',
    label === '核对' ? 'btn-annotate--ready' : '',
  ].filter(Boolean).join(' ');
  const title = batch
    ? '在新标签打开本课建块页，本册页继续显示进度'
    : label === '核对'
      ? '进入本课核对区块并保存锁定'
      : '进入本课建块页';
  return (
    `<a class="${cls}" href="${annotateHrefForLesson(lessonUid)}"` +
    ` target="_blank" rel="noopener"` +
    ` title="${escapeHtml(title)}">${label}</a>`
  );
}

function syncBlockBatchHint() {
  const el = document.getElementById('block-batch-hint');
  if (el) el.hidden = !blockBatchRunning;
}

function syncAnnotateLinksForBatch() {
  document.querySelectorAll('.btn-annotate').forEach((a) => {
    const row = a.closest('tr[data-lesson-row]');
    const uid = row?.dataset?.lessonRow;
    if (!uid) return;
    const label = annotateLinkLabelForLesson(uid);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener');
    a.setAttribute('href', annotateHrefForLesson(uid));
    if (blockBatchRunning) {
      a.title = '在新标签打开本课建块页，本册页继续显示进度';
    } else {
      a.title = label === '核对'
        ? '在新标签打开本课核对区块并保存锁定'
        : '在新标签打开本课建块页';
    }
    a.textContent = label;
    a.classList.toggle('btn-annotate--batch', blockBatchRunning);
    a.classList.toggle('btn-annotate--ready', label === '核对');
  });
}

function syncBlockBatchBar() {
  syncBlockBatchHint();
  syncAnnotateLinksForBatch();
  const btn = document.getElementById('block-batch-run-btn');
  if (!btn) return;
  btn.disabled = blockBatchRunning || cwBatchUploading;
  if (blockBatchRunning) {
    btn.textContent = IS_LIBRARY_SHELL ? '处理中…' : '建块中…';
  } else {
    btn.textContent = IS_LIBRARY_SHELL ? '整册 OCR / 建块' : '整册建块';
  }
}

function lessonsEligibleForBlockBatch() {
  const lessons = lastVolumeData?.lessons || [];
  if (IS_LIBRARY_SHELL) {
    return lessons.filter(
      (les) => (les.lesson_page_count || 0) > 0 || (les.page_start && les.page_end),
    );
  }
  return lessons.filter((les) => (les.slide_count || 0) > 0);
}

function openBlockBatchModal() {
  const lessons = lessonsEligibleForBlockBatch();
  if (!lessons.length) {
    toast(
      IS_LIBRARY_SHELL
        ? '本册尚无已划页的课时，请先完成解析并确认页码'
        : '本册尚无已上传课件的课时，请先上传课件 ZIP',
    );
    return;
  }
  const withBlocks = lessons.filter((les) => (les.block_count || 0) > 0);
  const locked = withBlocks.filter((les) => lessonIsBlocksLocked(les));
  const modal = document.getElementById('block-batch-modal');
  const body = document.getElementById('block-batch-modal-body');
  const list = document.getElementById('block-batch-modal-list');
  if (!modal || !body || !list) return;

  body.textContent = IS_LIBRARY_SHELL
    ? (
      `共 ${lessons.length} 课可处理。其中 ${withBlocks.length} 课已有区块`
      + (locked.length ? `（${locked.length} 课已保存锁定）` : '')
      + '。默认跳过已有区块的课时，对其余课执行 OCR → 整理 → 建块。'
    )
    : (
      `共 ${lessons.length} 课有课件。其中 ${withBlocks.length} 课已有区块`
      + (locked.length ? `（${locked.length} 课已保存锁定）` : '')
      + '。默认将跳过这些课时，仅处理尚未建块的课时。'
      + (resolveBlockBatchPipelineForVolume() === 'sequential'
        ? ' 本册采用单课串行：每课 OCR→整理→建块 完成后才处理下一课（便于记录总耗时）。'
        : ' 本册采用三阶段并行：各课 OCR / 整理 / 建块流水线同时进行。')
    );

  const preview = withBlocks.slice(0, 10).map((les) => {
    const tag = lessonIsBlocksLocked(les) ? '已保存' : `${les.block_count} 区块`;
    return `<li>${escapeHtml(les.lesson_no)} ${escapeHtml(les.lesson_name)} · ${escapeHtml(tag)}</li>`;
  }).join('');
  const more = withBlocks.length > 10
    ? `<li class="hint">… 另有 ${withBlocks.length - 10} 课</li>`
    : '';
  list.innerHTML = withBlocks.length
    ? `<li class="hint">已有区块的课时：</li>${preview}${more}`
    : '<li class="hint">尚无课时完成建块，将全部执行一键全流程。</li>';

  const skipRadio = modal.querySelector('input[name="block-batch-mode"][value="skip"]');
  if (skipRadio) skipRadio.checked = true;
  modal.hidden = false;
}

function closeBlockBatchModal() {
  const modal = document.getElementById('block-batch-modal');
  if (modal) modal.hidden = true;
}

function normalizeServerBlockProgress(prog) {
  return {
    state: prog.state,
    label: prog.label,
    ocr: prog.ocr,
    curate: prog.curate,
    seed: prog.seed,
    ocrPage: prog.ocr_page ?? prog.ocrPage ?? 0,
    ocrTotal: prog.ocr_total ?? prog.ocrTotal ?? 0,
    block_count: prog.block_count,
    elapsed_seconds: prog.elapsed_seconds,
  };
}

function applyBlockBatchJob(job) {
  if (!job) return;
  blockBatchRunning = !!job.running;
  syncBlockBatchBar();
  if (job.running) {
    for (const [uid, prog] of Object.entries(job.lessons || {})) {
      updateBlockBatchProgress(uid, normalizeServerBlockProgress(prog));
      if (prog.block_count != null && lastVolumeData?.lessons) {
        const les = lastVolumeData.lessons.find((l) => l.lesson_uid === uid);
        if (les) {
          les.block_count = prog.block_count;
          refreshAnnotateStatsCell(uid);
        }
      }
    }
  } else {
    blockBatchProgress.clear();
    refreshAllLessonPipelines();
  }
  renderVolumePipeline(lastVolumeData);
  syncAnnotateLinksForBatch();
}

function stopBlockBatchPolling() {
  if (blockBatchPollTimer) {
    clearTimeout(blockBatchPollTimer);
    blockBatchPollTimer = null;
  }
}

function showBlockBatchSummary(summary, job) {
  if (!summary) return;
  let msg = `本册建块完成：成功 ${summary.ok_count || 0} 课`;
  if (summary.skip_count) msg += `，跳过 ${summary.skip_count}`;
  if (summary.err_count) msg += `，失败 ${summary.err_count}`;
  const strategy = summary.pipeline_strategy || job?.pipeline_strategy;
  if (strategy) msg += ` · ${blockBatchPipelineLabel(strategy)}`;
  if (summary.elapsed_seconds != null) {
    msg += ` · 总耗时 ${formatBlockBatchDuration(summary.elapsed_seconds)}`;
  }
  toast(msg);
}

function startBlockBatchPolling() {
  stopBlockBatchPolling();
  blockBatchPollTimer = setTimeout(() => syncBlockBatchJobFromServer(true), 1500);
}

function clearBlockBatchProgressUi() {
  blockBatchProgress.clear();
  refreshAllLessonPipelines();
  syncBlockBatchBar();
}

async function syncBlockBatchJobFromServer(fromPoll) {
  try {
    const r = await fetch(
      `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/block-batch`,
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.job) {
      stopBlockBatchPolling();
      if (blockBatchRunning) {
        toast('整册建块任务已中断（可能因重启服务），请重新点击「整册建块」');
      }
      blockBatchRunning = false;
      clearBlockBatchProgressUi();
      return;
    }
    const job = d.job;
    const wasRunning = blockBatchRunning;
    applyBlockBatchJob(job);

    if (job.running) {
      startBlockBatchPolling();
      return;
    }

    stopBlockBatchPolling();
    if (job.finished_at && job.finished_at !== blockBatchLastFinishedAt) {
      blockBatchLastFinishedAt = job.finished_at;
      if (wasRunning || fromPoll) {
        showBlockBatchSummary(job.summary, job);
        const vol = await fetch(
          `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}?_=${Date.now()}`,
          { cache: 'no-store' },
        );
        const volData = await readJsonResponse(vol);
        if (vol.ok && volData.ok) {
          renderVolume(volData);
          applyBlockBatchJob(job);
        }
      }
    }
  } catch {
    if (fromPoll) startBlockBatchPolling();
  }
}

async function runBlockBatch(mode) {
  closeBlockBatchModal();
  try {
    const r = await fetch(
      `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/block-batch`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode }),
      },
    );
    const d = await readJsonResponse(r);
    if (r.status === 409 && d.job) {
      applyBlockBatchJob(d.job);
      startBlockBatchPolling();
      toast(d.message || '本册建块进行中');
      return;
    }
    if (!r.ok || !d.ok || !d.job) throw new Error(d.error || '启动整册建块失败');
    blockBatchLastFinishedAt = null;
    applyBlockBatchJob(d.job);
    startBlockBatchPolling();
    toast(`整册建块已在后台运行（${blockBatchPipelineLabel(resolveBlockBatchPipelineForVolume())}），可离开本页查看单课`);
  } catch (e) {
    toast(e.message || String(e));
  }
}

async function cancelBlockBatch() {
  if (!blockBatchRunning) return;
  try {
    const r = await fetch(
      `/api/old-library/volumes/${encodeURIComponent(VOLUME_CODE)}/block-batch`,
      { method: 'DELETE' },
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '取消失败');
    stopBlockBatchPolling();
    blockBatchRunning = false;
    blockBatchProgress.clear();
    syncBlockBatchBar();
    refreshAllLessonPipelines();
    toast('已取消整册建块，可重新点击「整册建块」');
  } catch (e) {
    toast(e.message || String(e));
  }
}

function bindBlockBatch() {
  const runBtn = document.getElementById('block-batch-run-btn');
  const cancelBtn = document.getElementById('block-batch-modal-cancel');
  const startBtn = document.getElementById('block-batch-modal-start');
  const cancelJobBtn = document.getElementById('block-batch-cancel-btn');
  const modal = document.getElementById('block-batch-modal');

  if (runBtn) runBtn.onclick = () => openBlockBatchModal();
  if (cancelJobBtn) cancelJobBtn.onclick = () => cancelBlockBatch();
  if (cancelBtn) cancelBtn.onclick = () => closeBlockBatchModal();
  if (startBtn) {
    startBtn.onclick = () => {
      const selected = modal?.querySelector('input[name="block-batch-mode"]:checked');
      const mode = selected?.value === 'rebuild' ? 'rebuild' : 'skip';
      runBlockBatch(mode);
    };
  }
  if (modal) {
    modal.addEventListener('click', (ev) => {
      if (ev.target === modal) closeBlockBatchModal();
    });
  }
  syncBlockBatchBar();
}

function bindIntakeReturnFocus() {
  window.addEventListener('storage', (ev) => {
    if (ev.key !== INTAKE_FOCUS_KEY || !ev.newValue) return;
    window.focus();
    loadVolume().catch(() => {});
  });
}

function openAnnotateTab(url) {
  const u = new URL(url, window.location.origin);
  u.searchParams.set('from', 'intake');
  window.open(u.toString(), '_blank');
}

function bindAnnotateBatchLinks() {
  const card = document.getElementById('lessons-card');
  if (!card || card.dataset.annotateBatchBound) return;
  card.dataset.annotateBatchBound = '1';
  const openFromLink = (ev, a) => {
    ev.preventDefault();
    ev.stopPropagation();
    openAnnotateTab(a.href);
  };
  card.addEventListener('click', (ev) => {
    const a = ev.target.closest('a.btn-annotate--batch');
    if (!a) return;
    openFromLink(ev, a);
  });
  card.addEventListener('auxclick', (ev) => {
    if (ev.button !== 1) return;
    const a = ev.target.closest('a.btn-annotate--batch');
    if (!a) return;
    openFromLink(ev, a);
  });
}

bindIntakeReturnFocus();
bindAnnotateBatchLinks();
bindBlockBatch();

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || !lastVolumeData) return;
  loadVolume().catch(() => {});
});

window.addEventListener('pageshow', (ev) => {
  if (!ev.persisted && !lastVolumeData) return;
  loadVolume().catch(() => {});
});

loadVolume().catch((e) => {
  document.getElementById('page-sub').textContent = e.message;
});

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
      `/api/old-library/lessons/${encodeURIComponent(lessonUid)}/pages?${qs}`,
    );
    const d = await readJsonResponse(r);
    if (!r.ok || !d.ok) throw new Error(d.error || '加载页图失败');
    renderPreviewGrid(d);
  } catch (e) {
    grid.innerHTML = `<p class="preview-empty">${e.message}</p>`;
  }
}
