(function () {
  const $ = (s) => document.querySelector(s);
  function showToast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._tid);
    el._tid = setTimeout(() => { el.hidden = true; }, 4000);
  }

  const activeSubjectId = window.diffResolveActiveSubjectId
    ? window.diffResolveActiveSubjectId('huaxue')
    : 'huaxue';
  if (window.diffSaveSubjectId) window.diffSaveSubjectId(activeSubjectId);
  const subject = (window.diffSubjectById && window.diffSubjectById(activeSubjectId))
    || { id: 'huaxue', label: '化学' };

  const lead = $('#diff-compare-lead');
  if (lead) {
    lead.textContent = `${subject.label} — 预览页或课时匹配后，进入左旧右新原子对照`;
  }
  const back = $('#diff-compare-back');
  if (back && window.diffSubjectQuery) {
    back.href = `/textbook-diff/?${window.diffSubjectQuery(activeSubjectId)}`;
  }
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('subject', activeSubjectId);
    window.history.replaceState({}, '', url.pathname + '?' + url.searchParams.toString());
  } catch (e) { /* ignore */ }

  const oldSelect = $('#old-select');
  const newSelect = $('#new-select');
  const compareBtn = $('#compare-btn');
  const compareHint = $('#compare-hint');
  const compareResultPreview = $('#compare-result-preview');
  const compareResultLessons = $('#compare-result-lessons');
  const previewSummary = $('#preview-summary');
  const previewList = $('#preview-list');
  const lessonPairList = $('#lesson-pair-list');

  const CHANGE_ORDER = ['基本一致', '局部改写', '明显修改', '大幅重写'];

  async function loadVolumes() {
    try {
      const q = window.diffSubjectQuery
        ? `?${window.diffSubjectQuery(activeSubjectId)}`
        : `?subject=${encodeURIComponent(subject.label)}`;
      const res = await fetch(`/api/textbook-diff/volumes${q}`);
      const data = await res.json();
      if (!data.ok) return;
      const oldVols = data.volumes.filter((v) => v.book_type === 'diff_old');
      const newVols = data.volumes.filter((v) => v.book_type === 'diff_new');
      oldSelect.innerHTML = '<option value="">— 选择册次 —</option>'
        + oldVols.map((v) => `<option value="${v.volume_code}">${v.display_title}</option>`).join('');
      newSelect.innerHTML = '<option value="">— 选择册次 —</option>'
        + newVols.map((v) => `<option value="${v.volume_code}">${v.display_title}</option>`).join('');
      if (!oldVols.length && !newVols.length) {
        showToast(`${subject.label}尚无对比册次，请先在上传入口建设`);
      }
    } catch (e) {
      showToast('加载册次失败：' + e);
    }
  }
  loadVolumes();

  function checkReady() {
    compareBtn.disabled = !oldSelect.value || !newSelect.value;
  }
  oldSelect.addEventListener('change', checkReady);
  newSelect.addEventListener('change', checkReady);

  compareBtn.addEventListener('click', async () => {
    const oldCode = oldSelect.value;
    const newCode = newSelect.value;
    if (!oldCode || !newCode) return;

    compareBtn.disabled = true;
    compareBtn.textContent = '分析中…';
    compareHint.hidden = true;
    compareResultPreview.hidden = true;
    compareResultLessons.hidden = true;
    previewSummary.hidden = true;

    try {
      const res = await fetch(
        `/api/textbook-diff/compare?old_code=${encodeURIComponent(oldCode)}&new_code=${encodeURIComponent(newCode)}`
      );
      const data = await res.json();
      if (!data.ok) { showToast('对比失败：' + (data.error || '未知')); return; }

      if (data.hint) {
        compareHint.textContent = data.hint;
        compareHint.hidden = false;
      }
      if (data.preview_error) showToast(data.preview_error);

      if (Array.isArray(data.preview_pairs) && data.preview_pairs.length) {
        renderPreviewSummary(data.preview_summary, data.preview_meta);
        renderPreviewList(data.preview_pairs);
        compareResultPreview.hidden = false;
      }

      if (Array.isArray(data.lesson_pairs) && data.lesson_pairs.length && !data.draft_preview) {
        renderLessonPairs(data.lesson_pairs);
        compareResultLessons.hidden = false;
      } else if (data.draft_preview && data.lesson_pairs?.length) {
        compareHint.textContent = (compareHint.textContent || '')
          + ' 未定稿预览以「预览页对照」为主；下方课时列表仅供参考。';
        compareHint.hidden = false;
      }
    } catch (e) {
      showToast('请求异常：' + e);
    } finally {
      compareBtn.disabled = false;
      compareBtn.textContent = '开始对比';
    }
  });

  function changeClass(change) {
    if (change === '基本一致') return 'change-ok';
    if (change === '局部改写') return 'change-mid';
    if (change === '明显修改') return 'change-warn';
    return 'change-high';
  }

  function renderPreviewSummary(summary, meta) {
    if (!summary || !previewSummary) {
      if (previewSummary) previewSummary.hidden = true;
      return;
    }
    const stats = CHANGE_ORDER
      .filter((k) => summary.by_change && summary.by_change[k])
      .map((k) => `<span class="diff-summary-stat ${changeClass(k)}">${esc(k)} ${summary.by_change[k]} 页</span>`)
      .join('');
    previewSummary.innerHTML = `
      <h3 class="diff-section-title">变化总览</h3>
      <div class="diff-summary-stats">
        <span class="diff-summary-stat">可对比 ${summary.comparable_count || 0} 页</span>
        ${stats}
        ${summary.skipped_count ? `<span class="diff-summary-stat change-skip">跳过 ${summary.skipped_count} 页</span>` : ''}
      </div>
      ${meta?.new_pdf_pages ? `<p class="diff-preview-meta">预览共 ${meta.new_pdf_pages} 页 · 旧书偏移 +${meta.old_offset || 7}</p>` : ''}`;
    previewSummary.hidden = false;
  }

  function renderPreviewList(pairs) {
    const rows = (pairs || []).slice().sort((a, b) => (a.new_page || 0) - (b.new_page || 0));
    if (!rows.length) {
      previewList.innerHTML = '<p class="diff-preview-empty">未识别到预览页。</p>';
      return;
    }
    previewList.innerHTML = rows.map((p) => {
      const badge = p.comparable && p.change
        ? `<span class="diff-change-badge ${changeClass(p.change)}">${esc(p.change)}</span>`
        : `<span class="diff-change-badge change-skip">${esc(p.kind || '跳过')}</span>`;
      const mapLine = p.old_page
        ? `预览 p${p.new_page} → 旧书 p${p.old_page}${p.old_lesson_label ? ' · ' + esc(p.old_lesson_label) : ''}`
        : `预览 p${p.new_page}`;
      const rowBtn = p.compare_url
        ? `<a class="btn btn-sm diff-row-compare" href="${esc(p.compare_url)}">对比</a>`
        : '';
      return `
        <div class="diff-preview-row diff-preview-row--hub">
          <div class="diff-preview-row-main">
            <strong>${esc(p.label || mapLine)}</strong>
            <span class="diff-preview-row-sub">${mapLine}</span>
            ${p.change_summary ? `<span class="diff-preview-row-change">${esc(p.change_summary)}</span>` : ''}
          </div>
          <div class="diff-preview-row-badges">${badge}${rowBtn}</div>
        </div>`;
    }).join('');
  }

  function renderLessonPairs(pairs) {
    lessonPairList.innerHTML = pairs.map((lp) => {
      const n = lp.new;
      const o = lp.old;
      if (!n) return '';
      const oldLine = o
        ? `旧：${esc(o.unit_title || '')} · ${esc(o.lesson_name || '')} p${o.page_start || '?'}–${o.page_end || '?'}`
        : '<span class="diff-preview-row-note">无对应旧课时</span>';
      const rowBtn = lp.compare_url && o
        ? `<a class="btn btn-sm diff-row-compare" href="${esc(lp.compare_url)}">对比</a>`
        : '';
      return `
        <div class="diff-preview-row diff-preview-row--hub">
          <div class="diff-preview-row-main">
            <strong>${esc(n.lesson_name || '未命名')}</strong>
            <span class="diff-preview-row-sub">新：${esc(n.unit_title || '')} p${n.page_start || '?'}–${n.page_end || '?'}</span>
            <span class="diff-preview-row-sub">${oldLine}</span>
          </div>
          <div class="diff-preview-row-badges">${rowBtn}</div>
        </div>`;
    }).join('');
  }

  function esc(s) {
    const el = document.createElement('span');
    el.textContent = s;
    return el.innerHTML;
  }
})();
