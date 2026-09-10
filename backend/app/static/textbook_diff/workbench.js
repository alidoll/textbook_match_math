(function () {
  const BOOK_TYPE = window.DIFF_BOOK_TYPE; // 'diff_old' or 'diff_new'
  const TYPE_LABEL = BOOK_TYPE === 'diff_old' ? '旧教材' : '新教材';
  const SUBJECTS = window.DIFF_SUBJECTS || [];

  const activeSubjectId = window.diffResolveActiveSubjectId
    ? window.diffResolveActiveSubjectId('huaxue')
    : 'huaxue';
  if (window.diffSaveSubjectId) window.diffSaveSubjectId(activeSubjectId);

  const subject = (window.diffSubjectById && window.diffSubjectById(activeSubjectId))
    || SUBJECTS[0]
    || { id: 'huaxue', label: '化学', editions: [] };

  let activeEditionId = (subject.editions && subject.editions[0] && subject.editions[0].id) || '';

  function toast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.hidden = false;
    setTimeout(() => { el.hidden = true; }, 4000);
  }

  async function ensureVolume(code) {
    const r = await fetch('/api/textbook-diff/volumes/ensure', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ volume_code: code }),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || '创建册次失败');
    return d;
  }

  function volCard(edition, gt) {
    const btSuffix = BOOK_TYPE === 'diff_old' ? 'DOLD' : 'DNEW';
    const termChar = gt.term === '上' ? 'S' : 'X';
    const code = `${edition.code_prefix}-${gt.grade}${termChar}-${btSuffix}`;
    const intakePath = BOOK_TYPE === 'diff_old' ? 'old' : 'new';
    return `<article class="vol-card">
      <div class="vol-card-head">
        <span class="vol-grade-badge">${gt.grade}</span>
        <div>
          <h3 class="vol-card-title">${gt.label}</h3>
          <p class="vol-card-sub" id="sub-${code}">—</p>
        </div>
      </div>
      <a class="vol-build-cta" href="/textbook-diff/${intakePath}/intake?code=${code}" data-code="${code}" data-ensure="1" id="cta-${code}">进入本册建设 →</a>
    </article>`;
  }

  function syncChrome() {
    const lead = document.getElementById('wb-lead');
    if (lead) {
      lead.textContent = `${subject.label} — 上传整册 PDF，识别目录，划分页码，生成页图`;
    }
    const back = document.getElementById('wb-back-diff');
    if (back && window.diffSubjectQuery) {
      back.href = `/textbook-diff/?${window.diffSubjectQuery(activeSubjectId)}`;
    }
  }

  function render() {
    syncChrome();
    const root = document.getElementById('editions');
    if (!root) return;

    const editions = subject.editions || [];
    let edition = editions.find((e) => e.id === activeEditionId);
    if (!edition) edition = editions[0];
    if (edition) activeEditionId = edition.id;

    if (!editions.length) {
      root.innerHTML = `
        <p class="wb-filter-label">${TYPE_LABEL} · ${subject.label}</p>
        <p class="wb-empty-hint">该学科尚无配置版本，请返回首页改选其他学科。</p>`;
      return;
    }

    const editionTabs = editions.map((e) =>
      `<button type="button" class="tab edition-tab${e.id === activeEditionId ? ' active' : ''}" data-edition="${e.id}">${e.label}</button>`
    ).join('');

    const grades = (edition && edition.grades) || [];
    const gradeBlock = grades.length
      ? `<div class="vol-grid" id="vol-grid">${grades.map((gt) => volCard(edition, gt)).join('')}</div>`
      : `<p class="wb-empty-hint">「${subject.label} · ${edition.label}」册次配置中，敬请期待。可返回首页选择化学继续建设。</p>`;

    root.innerHTML = `
      <p class="wb-filter-label">${TYPE_LABEL} · ${subject.label}</p>
      <p class="wb-filter-label">教材版本</p>
      <div class="edition-tabs" id="edition-tabs">${editionTabs}</div>
      <p class="wb-filter-label">年级</p>
      ${gradeBlock}
    `;

    root.querySelectorAll('#edition-tabs .tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        activeEditionId = btn.dataset.edition;
        render();
      });
    });

    root.querySelectorAll('.vol-build-cta[data-ensure="1"]').forEach((a) => {
      a.addEventListener('click', async function (e) {
        e.preventDefault();
        const code = this.dataset.code;
        const href = this.getAttribute('href');
        this.textContent = '初始化…';
        try {
          await ensureVolume(code);
          window.location.href = href;
        } catch (err) {
          toast('创建失败：' + err.message);
          this.textContent = '进入本册建设 →';
        }
      });
    });

    if (grades.length) loadVolumeStatuses();
  }

  async function loadVolumeStatuses() {
    try {
      const q = window.diffSubjectQuery
        ? `?${window.diffSubjectQuery(activeSubjectId)}`
        : `?subject=${encodeURIComponent(subject.label)}`;
      const r = await fetch(`/api/textbook-diff/volumes${q}`);
      const d = await r.json();
      if (!d.ok) return;
      const byCode = {};
      d.volumes.forEach((v) => { byCode[v.volume_code] = v; });

      document.querySelectorAll('.vol-card').forEach((card) => {
        const a = card.querySelector('a[data-code]');
        if (!a) return;
        const code = a.dataset.code;
        const v = byCode[code];
        if (!v) return;
        const sub = document.getElementById('sub-' + code);
        const cta = document.getElementById('cta-' + code);
        if (v.lesson_count > 0) {
          if (sub) sub.textContent = `${v.lesson_count} 课`;
        }
        if (v.parse_status === 'done') {
          if (cta) cta.textContent = '进入编辑 →';
          card.classList.add('vol-card--complete');
        }
      });
    } catch (e) { /* ignore */ }
  }

  // Keep subject in URL for deep links / refresh
  try {
    const url = new URL(window.location.href);
    url.searchParams.set('subject', activeSubjectId);
    window.history.replaceState({}, '', url.pathname + '?' + url.searchParams.toString());
  } catch (e) { /* ignore */ }

  render();
})();
