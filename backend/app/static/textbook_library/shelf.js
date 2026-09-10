(function () {
  const API = '/api/textbook-library';
  const subjectsEl = document.getElementById('lib-subjects');
  const editionsEl = document.getElementById('lib-editions');
  const gridEl = document.getElementById('lib-grid');
  let subject = '化学';
  let editionId = '';

  async function readJson(r) {
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.ok === false) throw new Error(d.error || r.statusText || '请求失败');
    return d;
  }

  function statusClass(status) {
    if (status === '已上架') return 'is-ready';
    if (status === '建设中') return 'is-building';
    return 'is-empty';
  }

  async function loadSubjects() {
    const d = await readJson(await fetch(`${API}/subjects`));
    const list = d.subjects || [];
    subject = list[0] || '化学';
    subjectsEl.innerHTML = list.map((s) =>
      `<button type="button" data-subject="${s}" class="${s === subject ? 'is-on' : ''}">${s}</button>`
    ).join('');
    subjectsEl.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        subject = btn.getAttribute('data-subject') || '化学';
        subjectsEl.querySelectorAll('button').forEach((b) => b.classList.toggle('is-on', b === btn));
        loadEditions();
      });
    });
    await loadEditions();
  }

  async function loadEditions() {
    editionsEl.innerHTML = '<span class="lib-empty">加载版本…</span>';
    gridEl.innerHTML = '<p class="lib-empty">选择版本…</p>';
    const d = await readJson(await fetch(`${API}/editions?subject=${encodeURIComponent(subject)}`));
    const eds = d.editions || [];
    if (!eds.length) {
      editionsEl.innerHTML = '<span class="lib-empty">暂无版本</span>';
      return;
    }
    editionId = eds[0].edition_id;
    editionsEl.innerHTML = eds.map((e) =>
      `<button type="button" data-ed="${e.edition_id}" class="${e.edition_id === editionId ? 'is-on' : ''}">${e.label}</button>`
    ).join('');
    editionsEl.querySelectorAll('button').forEach((btn) => {
      btn.addEventListener('click', () => {
        editionId = btn.getAttribute('data-ed') || '';
        editionsEl.querySelectorAll('button').forEach((b) => b.classList.toggle('is-on', b === btn));
        loadVolumes();
      });
    });
    await loadVolumes();
  }

  async function loadVolumes() {
    gridEl.innerHTML = '<p class="lib-empty">加载册次…</p>';
    const d = await readJson(await fetch(`${API}/editions/${encodeURIComponent(editionId)}/volumes`));
    const vols = d.volumes || [];
    if (!vols.length) {
      gridEl.innerHTML = '<p class="lib-empty">该版本暂无册次格</p>';
      return;
    }
    gridEl.innerHTML = vols.map((v) => {
      const href = `/textbook-library/slot?edition=${encodeURIComponent(v.edition_id)}`
        + `&grade=${encodeURIComponent(v.grade)}`
        + `&term=${encodeURIComponent(v.term)}`;
      const count = v.copy_count != null ? v.copy_count : 0;
      const badge = count ? `${v.status} · ${count}本` : v.status;
      return (
        `<a class="lib-slot ${statusClass(v.status)}" href="${href}">`
        + `<div class="lib-spine"><span class="lib-spine-name">${v.label}</span></div>`
        + `<div class="lib-slot-label">${v.label}</div>`
        + `<span class="lib-badge">${badge}</span>`
        + `</a>`
      );
    }).join('');
  }

  loadSubjects().catch((e) => {
    gridEl.innerHTML = `<p class="lib-empty">${e.message || e}</p>`;
  });
})();
