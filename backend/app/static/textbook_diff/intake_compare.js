(function () {
  const code = window.DIFF_INTAKE_COMPARE_CODE || '';
  const kind = window.DIFF_INTAKE_COMPARE_KIND || 'full';
  const previewBlobId = window.DIFF_INTAKE_PREVIEW_BLOB_ID || '';
  const $ = (s) => document.querySelector(s);

  const CHANGE_ORDER = ['基本一致', '局部改写', '明显修改', '大幅重写'];

  function showToast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._tid);
    el._tid = setTimeout(() => { el.hidden = true; }, 4000);
  }

  function esc(s) {
    const el = document.createElement('span');
    el.textContent = s;
    return el.innerHTML;
  }

  function changeClass(change) {
    if (change === '基本一致') return 'change-ok';
    if (change === '局部改写') return 'change-mid';
    if (change === '明显修改') return 'change-warn';
    return 'change-high';
  }

  function setLead(text) {
    const lead = $('#page-lead');
    if (lead) lead.textContent = text;
  }

  function showPreviewLoading(show, message) {
    const list = $('#preview-list');
    if (!list) return;
    if (show) {
      list.innerHTML = `<p class="diff-preview-empty diff-preview-loading">${esc(message || '正在粗分预览页…')}</p>`;
    }
  }

  function renderSteps(steps) {
    const panel = $('#steps-panel');
    const list = $('#step-list');
    if (!steps?.length) {
      panel.hidden = true;
      return;
    }
    list.innerHTML = steps.map((s) => {
      const cls = s.done ? 'is-done' : 'is-pending';
      const mark = s.done ? '✓' : '…';
      return `<li class="${cls}"><span>${mark}</span><span>${esc(s.label)}</span></li>`;
    }).join('');
    panel.hidden = false;
  }

  function renderBlockers(blockers) {
    const panel = $('#blockers-panel');
    const list = $('#blockers-list');
    if (!blockers?.length) {
      panel.hidden = true;
      return;
    }
    list.innerHTML = blockers.map((b) => `<li>${esc(b)}</li>`).join('');
    panel.hidden = false;
  }

  function renderPreviewSummary(summary, meta) {
    const el = $('#preview-summary');
    if (!summary || !el) {
      if (el) el.hidden = true;
      return;
    }
    const stats = CHANGE_ORDER
      .filter((k) => summary.by_change && summary.by_change[k])
      .map(
        (k) =>
          `<span class="diff-summary-stat ${changeClass(k)}">${esc(k)} ${summary.by_change[k]} 页</span>`,
      )
      .join('');
    const     tierNote =
      meta?.build_tier === 'fast'
        ? '<p class="diff-preview-meta diff-preview-meta--warn">当前按页脚「目录页码 + 单元/课时」粗分；点「重新粗分」可 OCR 全书校验并估算改动程度。</p>'
        : '';
    el.innerHTML = `
      <h3 class="diff-section-title">变化总览</h3>
      <div class="diff-summary-stats">
        <span class="diff-summary-stat">可对比 ${summary.comparable_count || 0} 页</span>
        ${stats}
        ${summary.skipped_count ? `<span class="diff-summary-stat change-skip">跳过 ${summary.skipped_count} 页</span>` : ''}
      </div>
      ${meta?.new_pdf_pages ? `<p class="diff-preview-meta">修订版共 ${meta.new_pdf_pages} 页 · 旧书偏移 +${meta.old_offset || 7}</p>` : ''}
      ${tierNote}`;
    el.hidden = false;
  }

  function renderPreviewList(pairs) {
    const list = $('#preview-list');
    const rows = (pairs || []).slice().sort((a, b) => (a.new_page || 0) - (b.new_page || 0));
    if (!rows.length) {
      list.innerHTML = '<p class="diff-preview-empty">未识别到预览页。</p>';
      return;
    }
    list.innerHTML = rows
      .map((p) => {
        const badge =
          p.comparable && p.change
            ? `<span class="diff-change-badge ${changeClass(p.change)}">${esc(p.change)}</span>`
            : `<span class="diff-change-badge change-skip">${esc(p.kind || '跳过')}</span>`;
        const mapLine = p.comparable && p.old_page
          ? `修订 p${p.new_page} → 旧书 p${p.old_page}${p.old_lesson_label ? ' · ' + esc(p.old_lesson_label) : ''}`
          : `修订 p${p.new_page}${p.note ? ' · ' + esc(p.note) : ''}`;
        const compareBtn = p.comparable && p.compare_url
          ? `<a class="btn btn-sm diff-row-compare" href="${esc(p.compare_url)}">对比</a>`
          : '';
        return `
        <div class="diff-preview-row diff-preview-row--hub">
          <div class="diff-preview-row-main">
            <strong>${esc(p.label || mapLine)}</strong>
            <span class="diff-preview-row-sub">${mapLine}</span>
            ${p.change_summary ? `<span class="diff-preview-row-change">${esc(p.change_summary)}</span>` : ''}
            ${p.note ? `<span class="diff-preview-row-note">${esc(p.note)}</span>` : ''}
          </div>
          <div class="diff-preview-row-badges">${badge}${compareBtn}</div>
        </div>`;
      })
      .join('');
  }

  function renderLessonPairs(pairs) {
    const list = $('#lesson-pair-list');
    list.innerHTML = (pairs || [])
      .map((lp) => {
        const n = lp.new;
        const o = lp.old;
        if (!n) return '';
        const oldLine = o
          ? `旧：${esc(o.unit_title || '')} · ${esc(o.lesson_name || '')} p${o.page_start || '?'}–${o.page_end || '?'}`
          : '<span class="diff-preview-row-note">无对应旧课时</span>';
        const compareBtn =
          lp.compare_url && o
            ? `<a class="btn btn-sm diff-row-compare" href="${esc(lp.compare_url)}">对比</a>`
            : '';
        return `
        <div class="diff-preview-row diff-preview-row--hub">
          <div class="diff-preview-row-main">
            <strong>${esc(n.lesson_name || '未命名')}</strong>
            <span class="diff-preview-row-sub">新：${esc(n.unit_title || '')} p${n.page_start || '?'}–${n.page_end || '?'}</span>
            <span class="diff-preview-row-sub">${oldLine}</span>
          </div>
          <div class="diff-preview-row-badges">${compareBtn}</div>
        </div>`;
      })
      .join('');
  }

  function buildQuery({ quick = false, preview = false, force = false } = {}) {
    const q = new URLSearchParams({ code, kind });
    if (previewBlobId) q.set('preview_blob_id', previewBlobId);
    if (quick) q.set('quick', '1');
    if (preview) q.set('preview', '1');
    if (force) q.set('force', '1');
    return q;
  }

  async function fetchCompare(queryOpts) {
    if (!code) throw new Error('缺少册次 code');
    const res = await fetch(`/api/textbook-diff/intake-compare?${buildQuery(queryOpts)}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '加载失败');
    return data;
  }

  function applyData(data) {
    const back = $('#back-link');
    if (back && data.intake_back_url) back.href = data.intake_back_url;

    const title = $('#page-title');
    if (title) title.textContent = data.title || '教材对比';

    const lead = $('#page-lead');
    if (lead) {
      lead.textContent =
        data.hint ||
        `${data.old_volume?.display_title || ''} ↔ ${data.new_volume?.display_title || ''}`;
    }

    renderSteps(data.steps);
    renderBlockers(data.blockers);

    const hint = $('#compare-hint');
    if (data.preview_error) {
      hint.textContent = data.preview_error;
      hint.hidden = false;
    } else if (data.mode === 'draft' && data.preview_meta?.build_tier === 'fast' && !data.preview_error) {
      hint.textContent =
        '已按页序快速粗分全部预览页，可先点「对比」查看。需要 OCR 精确对齐时，再点「重新粗分」。';
      hint.hidden = false;
    } else if (hint) {
      hint.hidden = true;
    }

    const pairLabel = $('#pair-label');
    if (pairLabel) {
      pairLabel.textContent = `${data.old_code || ''} ↔ ${data.new_code || ''}`;
    }

    if (data.mode === 'full') {
      $('#full-result').hidden = false;
      $('#draft-result').hidden = true;
      renderLessonPairs(data.lesson_pairs);
    } else {
      $('#full-result').hidden = true;
      $('#draft-result').hidden = false;
      renderPreviewSummary(data.preview_summary, data.preview_meta);
      if (data.preview_pairs?.length) {
        renderPreviewList(data.preview_pairs);
      }
    }
  }

  async function init(force) {
    setLead('正在加载…');
    if (kind === 'draft') {
      $('#draft-result').hidden = false;
      showPreviewLoading(true, '正在粗分预览页，请稍候…');
    }
    try {
      const shell = await fetchCompare({
        quick: kind === 'draft' && !force,
        force: !!force && kind !== 'draft',
      });
      applyData(shell);

      if (kind === 'draft' && !shell.blockers?.length && (shell.preview_pending || force)) {
        if (force) {
          setLead('正在 OCR 精确粗分，约需 3–10 分钟，请勿关闭页面…');
        } else {
          setLead('正在粗分预览页…');
        }
        showPreviewLoading(true, force ? '正在 OCR 精确粗分…' : '正在粗分预览页…');
        const preview = await fetchCompare({ preview: true, force: !!force });
        Object.assign(shell, preview);
        applyData(shell);
      }
    } catch (e) {
      setLead('加载失败');
      showPreviewLoading(false);
      showToast(String(e.message || e));
    }
  }

  init(false);

  const refreshBtn = $('#refresh-draft-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      refreshBtn.textContent = '粗分中…';
      setLead('正在 OCR 精确粗分，约需 3–10 分钟，请勿关闭页面…');
      showPreviewLoading(true, '正在 OCR 精确粗分，请耐心等待…');
      try {
        const preview = await fetchCompare({ preview: true, force: true });
        const shell = await fetchCompare({ quick: true });
        Object.assign(shell, preview);
        applyData(shell);
        showToast('已重新粗分');
      } catch (e) {
        setLead('粗分失败');
        showToast(String(e.message || e));
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.textContent = '重新粗分';
      }
    });
  }
})();
