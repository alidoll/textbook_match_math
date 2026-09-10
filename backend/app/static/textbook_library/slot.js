(function () {
  const API = '/api/textbook-library';
  const params = new URLSearchParams(window.location.search);
  const editionId = (params.get('edition') || '').trim();
  const grade = params.get('grade');
  const term = params.get('term') || '上';
  const grid = document.getElementById('slot-grid');
  const title = document.getElementById('slot-title');
  const lead = document.getElementById('slot-lead');
  const back = document.getElementById('slot-back');
  const btnAppend = document.getElementById('btn-append');
  const btnManage = document.getElementById('btn-manage');
  const btnDelete = document.getElementById('btn-delete-selected');
  const btnDone = document.getElementById('btn-manage-done');

  let manageMode = false;
  const selected = new Set();

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

  function esc(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function syncManageChrome() {
    btnAppend.hidden = manageMode;
    btnManage.hidden = manageMode;
    btnDelete.hidden = !manageMode;
    btnDone.hidden = !manageMode;
    btnDelete.textContent = `删除所选 (${selected.size})`;
    btnDelete.disabled = selected.size === 0;
    document.body.classList.toggle('lib-manage-mode', manageMode);
  }

  function setManageMode(on) {
    manageMode = !!on;
    if (!manageMode) selected.clear();
    syncManageChrome();
    load().catch((e) => {
      grid.innerHTML = `<p class="lib-empty">${esc(e.message || e)}</p>`;
    });
  }

  function toggleSelect(code, el) {
    if (selected.has(code)) {
      selected.delete(code);
      el.classList.remove('is-selected');
    } else {
      selected.add(code);
      el.classList.add('is-selected');
    }
    syncManageChrome();
  }

  async function load() {
    if (!editionId || grade == null) {
      grid.innerHTML = '<p class="lib-empty">缺少 edition / grade</p>';
      return;
    }
    const q = `edition_id=${encodeURIComponent(editionId)}&grade=${encodeURIComponent(grade)}&term=${encodeURIComponent(term)}`;
    const d = await readJson(await fetch(`${API}/slots/copies?${q}`));
    title.textContent = `${d.edition_label || ''} · ${d.label || ''}`;
    lead.textContent = manageMode
      ? `管理模式：点选副本，再删除所选 · 格内 ${d.copy_count || 0} 本`
      : `格内 ${d.copy_count || 0} 本副本 · 格状态：${d.status || '待入库'}`;
    back.href = `/textbook-library/`;
    const copies = d.copies || [];
    const alive = new Set(copies.map((c) => c.volume_code));
    [...selected].forEach((code) => {
      if (!alive.has(code)) selected.delete(code);
    });
    if (!copies.length) {
      grid.innerHTML = '<p class="lib-empty">尚无副本。点「追加副本」创建第一本。</p>';
      syncManageChrome();
      return;
    }
    grid.innerHTML = copies.map((c) => {
      const href = `/textbook-library/volumes/${encodeURIComponent(c.volume_code)}/intake`;
      const sel = selected.has(c.volume_code) ? ' is-selected' : '';
      const rename = manageMode
        ? ''
        : `<button type="button" class="lib-rename" data-code="${esc(c.volume_code)}">改名</button>`;
      const inner = (
        (manageMode
          ? `<span class="lib-check" aria-hidden="true"></span>`
          : '')
        + `<div class="lib-spine"><span class="lib-spine-name">${esc(c.name)}</span></div>`
        + `<div class="lib-slot-label">${esc(c.name)}</div>`
        + `<span class="lib-badge">${esc(c.status)}</span>`
      );
      const body = manageMode
        ? `<div role="button" tabindex="0" data-code="${esc(c.volume_code)}" class="lib-slot-link">${inner}</div>`
        : `<a class="lib-slot-link" href="${href}">${inner}</a>`;
      return (
        `<div class="lib-slot ${statusClass(c.status)}${sel}" data-code="${esc(c.volume_code)}">`
        + body
        + rename
        + `</div>`
      );
    }).join('');

    if (manageMode) {
      grid.querySelectorAll('.lib-slot').forEach((el) => {
        const code = el.getAttribute('data-code');
        const activate = (ev) => {
          ev.preventDefault();
          toggleSelect(code, el);
        };
        el.addEventListener('click', activate);
        el.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            activate(ev);
          }
        });
      });
    } else {
      grid.querySelectorAll('.lib-rename').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const code = btn.getAttribute('data-code');
          const next = window.prompt('副本显示名（如：2026秋 · 定稿）');
          if (next == null) return;
          try {
            await readJson(await fetch(`${API}/volumes/${encodeURIComponent(code)}`, {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ version_label: next }),
            }));
            await load();
          } catch (e) {
            window.alert(e.message || String(e));
          }
        });
      });
    }
    syncManageChrome();
  }

  btnAppend.addEventListener('click', async () => {
    const hint = window.prompt('新副本显示名（可留空用上传日默认）', '');
    if (hint === null) return;
    try {
      const r = await fetch(`${API}/volumes/append`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          edition_id: editionId,
          grade: Number(grade),
          term,
          version_label: hint || undefined,
        }),
      });
      const d = await readJson(r);
      window.location.href = `/textbook-library/volumes/${encodeURIComponent(d.volume_code)}/intake`;
    } catch (e) {
      window.alert(e.message || String(e));
    }
  });

  btnManage.addEventListener('click', () => setManageMode(true));
  btnDone.addEventListener('click', () => setManageMode(false));

  btnDelete.addEventListener('click', async () => {
    const codes = [...selected];
    if (!codes.length) return;
    const ok = window.confirm(
      `将永久删除选中的 ${codes.length} 本副本（课时、页图、OCR 等一并清除，不可恢复）。确定？`
    );
    if (!ok) return;
    btnDelete.disabled = true;
    try {
      await readJson(await fetch(`${API}/volumes/batch-delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ volume_codes: codes }),
      }));
      selected.clear();
      await load();
    } catch (e) {
      window.alert(e.message || String(e));
      syncManageChrome();
    }
  });

  syncManageChrome();
  load().catch((e) => {
    grid.innerHTML = `<p class="lib-empty">${esc(e.message || e)}</p>`;
  });
})();
