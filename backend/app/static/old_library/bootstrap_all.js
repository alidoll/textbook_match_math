/**
 * 旧库：一键载入本版基准目录（API 封装）。
 * 教材 PDF 批量导入 UI 已与 intake「整册课件」同构，见 workbench.js。
 */
(function (global) {
  async function readJson(r) {
    const text = await r.text();
    let d;
    try {
      d = text ? JSON.parse(text) : {};
    } catch (e) {
      throw new Error(r.ok ? '服务器返回了非 JSON 响应' : `请求失败 HTTP ${r.status}`);
    }
    if (!r.ok || d.ok === false) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  async function bootstrapEditionAll(editionId, { replace = false } = {}) {
    const id = String(editionId || '').trim();
    if (!id) throw new Error('缺少版本 id');
    const r = await fetch(`/api/old-library/editions/${encodeURIComponent(id)}/bootstrap-all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ replace, only_missing: !replace }),
    });
    return readJson(r);
  }

  function formatResultToast(d) {
    const parts = [
      `新建 ${d.created_count || 0} 册`,
      `共 ${d.lessons_created_total || 0} 课`,
    ];
    if (d.skipped_count) parts.push(`跳过已有 ${d.skipped_count} 册`);
    if (d.empty_count) parts.push(`表中无课 ${d.empty_count} 册`);
    if (d.error_count) parts.push(`失败 ${d.error_count} 册`);
    const label = d.edition_label || '';
    return `${label}${label ? '：' : ''}${parts.join(' · ')}`;
  }

  function defaultToast(msg) {
    const el = document.getElementById('toast');
    if (!el) {
      window.alert(msg);
      return;
    }
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.hidden = true; }, 8000);
  }

  function mountBootstrapBar(container, opts) {
    if (!container) return null;
    const editionId = String(opts?.editionId || '').trim();
    if (!editionId) {
      container.hidden = true;
      container.innerHTML = '';
      return null;
    }
    const hint = opts?.hint
      || '目录一键载入。教材 PDF 请在「旧库建设」版本页用「一键导入本版所有教材」（选文件 → 预览匹配 → 确认上传）。';
    const compact = !!opts?.compact;
    const toast = typeof opts?.toast === 'function' ? opts.toast : defaultToast;
    container.hidden = false;
    container.classList.add('edition-toolbar');
    if (compact) container.classList.add('edition-toolbar--compact');
    container.innerHTML = `
      <p class="edition-toolbar-hint">${hint}</p>
      <div class="edition-toolbar-actions">
        <button type="button" class="btn vol-setup-cta" data-action="bootstrap-all" data-ed="${editionId}">
          一键载入本版全部基准目录
        </button>
      </div>
    `;

    const btnBootstrap = container.querySelector('[data-action="bootstrap-all"]');
    if (btnBootstrap) {
      btnBootstrap.onclick = async () => {
        try {
          btnBootstrap.disabled = true;
          const d = await bootstrapEditionAll(editionId);
          toast(formatResultToast(d));
          if (typeof opts?.onDone === 'function') opts.onDone(d);
        } catch (e) {
          toast(e.message || String(e));
        } finally {
          btnBootstrap.disabled = false;
        }
      };
    }
    return btnBootstrap;
  }

  global.OldLibraryBootstrapAll = {
    readJson,
    bootstrapEditionAll,
    formatResultToast,
    mountBootstrapBar,
  };
})(typeof window !== 'undefined' ? window : globalThis);
