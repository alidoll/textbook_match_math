(function () {
  const grid = document.getElementById('diff-subject-grid');
  if (!grid || !window.DIFF_SUBJECTS) return;

  // 记住上次学科，仅用于高亮；点击后进入本册建设
  const lastId = window.diffResolveActiveSubjectId
    ? window.diffResolveActiveSubjectId('huaxue')
    : 'huaxue';

  grid.innerHTML = window.DIFF_SUBJECTS.map((s) => {
    const active = s.id === lastId ? ' is-active' : '';
    const editions = (s.editions || []).map((e) => e.label).join(' · ') || '待配置';
    return `
      <button type="button" class="diff-subject-card${active}" data-subject="${s.id}">
        <span class="diff-subject-name">${s.label}</span>
        <span class="diff-subject-meta">${editions}</span>
        <span class="diff-subject-cta">进入本册建设 →</span>
      </button>`;
  }).join('');

  grid.querySelectorAll('.diff-subject-card').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.subject;
      if (window.diffSaveSubjectId) window.diffSaveSubjectId(id);
      const q = window.diffSubjectQuery ? window.diffSubjectQuery(id) : `subject=${encodeURIComponent(id)}`;
      window.location.href = `/textbook-diff/workbook?${q}`;
    });
  });
})();
