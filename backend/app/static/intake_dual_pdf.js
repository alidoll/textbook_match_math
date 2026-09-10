/**
 * 册次 intake：完整版 + 不完整修订版 双 PDF 上传。
 * 完整版：选文件 → 点上传。修订版：选文件即自动上传。
 */
(function (global) {
  const SLOTS = [
    {
      role: 'full',
      title: '完整版',
      inputId: 'pdf-input-full',
      btnId: 'upload-btn-full',
      hintId: 'pdf-upload-hint-full',
      metaId: 'pdf-meta-full',
      hasKey: 'has_pdf',
      sizeKey: 'blob_size_bytes',
      blobKey: 'blob_id',
      mirrorKey: 'mirror_path',
      storageKey: 'blob_storage',
      doneLabel: '已上传完整版',
      uploadLabel: '上传完整版',
      compareBtnId: 'compare-btn-full',
      compareKind: 'full',
      autoUpload: false,
    },
    {
      role: 'draft',
      title: '不完整修订版',
      inputId: 'pdf-input-draft',
      hintId: 'pdf-upload-hint-draft',
      listId: 'pdf-drafts-list',
      summaryId: 'pdf-drafts-summary',
      pickLabelId: 'pdf-file-btn-draft-text',
      hasKey: 'has_preview_pdf',
      uploadLabel: '选择 PDF 上传',
      appendPickLabel: '更换修订版…',
      compareBtnId: 'compare-btn-draft',
      compareKind: 'draft',
      autoUpload: true,
    },
  ];

  function shortName(name, max = 28) {
    const n = String(name || '');
    return n.length > max ? `${n.slice(0, max - 1)}…` : n;
  }

  function infoRows(rows, escapeHtml) {
    return rows
      .filter(Boolean)
      .map(
        ([k, v]) =>
          `<div class="intake-info-row"><span class="label">${escapeHtml(k)}</span><span>${v}</span></div>`,
      )
      .join('');
  }

  function draftCount(data) {
    return data?.draft_pdf_count || (data?.draft_pdfs || []).length || 0;
  }

  /** 对比入口在「本册建设」粗分之后；intake 上传页不展示对比按钮。 */
  function refreshCompareBtn(slot) {
    if (!slot.compareBtnId) return;
    const btn = document.getElementById(slot.compareBtnId);
    if (!btn) return;
    btn.hidden = true;
    btn.removeAttribute('href');
  }

  function renderDraftSummary(slot, data, state, helpers) {
    const summaryEl = slot.summaryId ? document.getElementById(slot.summaryId) : null;
    if (!summaryEl) return;
    const count = draftCount(data);
    if (state.uploading && state.role === slot.role) {
      summaryEl.hidden = false;
      summaryEl.className = 'pdf-drafts-summary pdf-drafts-summary--busy';
      summaryEl.textContent = '正在上传…';
      return;
    }
    if (state.lastUploadMessage && state.lastUploadRole === slot.role) {
      summaryEl.hidden = false;
      summaryEl.className = 'pdf-drafts-summary pdf-drafts-summary--ok';
      summaryEl.textContent = state.lastUploadMessage;
      return;
    }
    if (count > 0) {
      summaryEl.hidden = false;
      summaryEl.className = 'pdf-drafts-summary pdf-drafts-summary--saved';
      summaryEl.textContent = '已保存修订版（再上传将替换）';
      return;
    }
    summaryEl.hidden = true;
    summaryEl.textContent = '';
  }

  function renderDraftList(slot, data, cfg, helpers) {
    const listEl = document.getElementById(slot.listId);
    if (!listEl) return;
    const drafts = data?.draft_pdfs || [];
    if (!drafts.length) {
      listEl.innerHTML = '<p class="pdf-drafts-empty">暂无修订版，请点上方「选择 PDF 上传」</p>';
      return;
    }
    listEl.innerHTML = drafts
      .map((d) => {
        const latest = d.is_latest ? '<span class="pdf-draft-tag">最新</span>' : '';
        const size = d.size_bytes ? helpers.formatBytes(d.size_bytes) : '—';
        const when = d.created_at ? helpers.escapeHtml(d.created_at) : '';
        return `
          <article class="pdf-draft-item">
            <div class="pdf-draft-item-main">
              <strong>${helpers.escapeHtml(d.label || '修订版')}</strong>${latest}
              <span class="pdf-draft-item-sub">${helpers.escapeHtml(size)}${when ? ` · ${when}` : ''}</span>
            </div>
          </article>`;
      })
      .join('');
  }

  function refreshDraftPickLabel(slot, data, uploading) {
    const el = slot.pickLabelId ? document.getElementById(slot.pickLabelId) : null;
    if (!el) return;
    if (uploading) {
      el.textContent = '上传中…';
      return;
    }
    el.textContent =
      draftCount(data) > 0 ? slot.appendPickLabel || slot.uploadLabel : slot.uploadLabel;
  }

  function refreshSlot(slot, data, state, helpers) {
    if (slot.autoUpload) {
      refreshDraftPickLabel(slot, data, state.uploading && state.role === slot.role);
      const input = document.getElementById(slot.inputId);
      if (input) input.disabled = !!(state.uploading && state.role === slot.role);
      return;
    }

    const btn = document.getElementById(slot.btnId);
    const hintEl = document.getElementById(slot.hintId);
    if (!btn) return;

    const hasFile = !!(data && data[slot.hasKey]);
    const pending = state.pending && state.pendingRole === slot.role;

    if (pending) {
      if (hintEl) {
        hintEl.hidden = false;
        hintEl.innerHTML = helpers.hintWithDot(
          'cw-zip-pending',
          `已选择「${helpers.escapeHtml(shortName(state.pendingName))}」，请点击${slot.uploadLabel}`,
        );
      }
      btn.classList.remove('pdf-upload-btn-idle', 'pdf-upload-btn-done');
      btn.classList.add('pdf-upload-btn-ready');
      btn.disabled = false;
      btn.textContent = slot.uploadLabel;
      return;
    }

    if (hintEl) {
      if (state.lastUploadMessage && state.lastUploadRole === slot.role && !pending) {
        hintEl.hidden = false;
        hintEl.innerHTML = helpers.hintWithDot('cw-zip-done', helpers.escapeHtml(state.lastUploadMessage));
      } else {
        hintEl.hidden = true;
        hintEl.innerHTML = '';
      }
    }
    btn.classList.remove('pdf-upload-btn-ready', 'pdf-upload-btn-idle', 'pdf-upload-btn-done');
    if (hasFile) {
      btn.classList.add('pdf-upload-btn-done');
      btn.disabled = false;
      btn.textContent = slot.doneLabel;
    } else {
      btn.classList.add('pdf-upload-btn-idle');
      btn.disabled = false;
      btn.textContent = slot.uploadLabel;
    }
  }

  function renderMeta(slot, data, helpers) {
    if (slot.role === 'draft') return;
    const meta = document.getElementById(slot.metaId);
    if (!meta) return;
    if (!data || !data[slot.hasKey]) {
      meta.hidden = true;
      meta.innerHTML = '';
      return;
    }
    const storage =
      data[slot.storageKey] === 'disk' ? '磁盘' : data[slot.storageKey] === 'mysql' ? '数据库' : '—';
    meta.hidden = false;
    const mirrorHint = data[slot.mirrorKey]
      ? `${data[slot.mirrorKey]}（册次固定文件名，换 PDF 仍写此路径）`
      : null;
    meta.innerHTML = infoRows(
      [
        ['类型', slot.title],
        [
          '存储',
          `已绑定（${storage} · blob ${String(data[slot.blobKey] || '').slice(0, 8)}…）`,
        ],
        data[slot.sizeKey] ? ['大小', helpers.formatBytes(data[slot.sizeKey])] : null,
        mirrorHint ? ['镜像', mirrorHint] : null,
        data.upload_filename && slot.role === 'full'
          ? ['最近上传', data.upload_filename]
          : null,
      ],
      helpers.escapeHtml,
    );
  }

  function slotsFor(cfg) {
    const full = SLOTS.find((s) => s.role === 'full');
    if (!full) return SLOTS;
    if (cfg.enableDraft === false) {
      return [
        {
          ...full,
          title: 'PDF',
          uploadLabel: '确认上传',
          doneLabel: '已上传',
        },
      ];
    }
    return SLOTS;
  }

  function init(cfg) {
    const activeSlots = slotsFor(cfg);
    const state = {
      pending: false,
      pendingRole: null,
      pendingName: null,
      uploading: false,
      role: null,
      lastUploadMessage: '',
      lastUploadRole: null,
    };
    const helpers = {
      escapeHtml: cfg.escapeHtml,
      formatBytes: cfg.formatBytes,
      hintWithDot: cfg.hintWithDot,
      toast: cfg.toast,
    };

    async function uploadFile(slot, file) {
      if (!file) return;
      const sizeMb = (file.size / 1024 / 1024).toFixed(1);
      const fd = new FormData();
      fd.append('pdf', file);
      fd.append('pdf_role', slot.role);
      const hintEl = document.getElementById(slot.hintId);
      const input = document.getElementById(slot.inputId);
      const btn = slot.btnId ? document.getElementById(slot.btnId) : null;

      state.uploading = true;
      state.role = slot.role;
      state.lastUploadMessage = '';
      state.lastUploadRole = null;
      if (btn) {
        btn.disabled = true;
        btn.classList.remove('pdf-upload-btn-ready');
        btn.textContent = `上传中… ${sizeMb} MB`;
      }
      if (input) input.disabled = true;
      refreshAll(cfg.getVolumeData());
      if (hintEl) {
        hintEl.hidden = false;
        hintEl.innerHTML = helpers.hintWithDot(
          'cw-zip-pending',
          `正在上传「${helpers.escapeHtml(shortName(file.name))}」（${sizeMb} MB）…`,
        );
      }
      try {
        const volumeCode = cfg.getVolumeCode ? cfg.getVolumeCode() : cfg.volumeCode;
        if (!volumeCode) throw new Error('未选择册次');
        const r = await fetch(
          `${cfg.apiPrefix}/volumes/${encodeURIComponent(volumeCode)}/pdf`,
          { method: 'POST', body: fd },
        );
        const d = await cfg.readJson(r);
        if (!r.ok || !d.ok) throw new Error(d.error || `上传失败（HTTP ${r.status}）`);
        state.pending = false;
        state.pendingRole = null;
        state.pendingName = null;
        if (input) input.value = '';
        if (slot.role === 'draft' && d.draft_duplicate) {
          state.lastUploadMessage = `「${shortName(file.name)}」已存在，未重复保存`;
          state.lastUploadRole = slot.role;
          helpers.toast('该修订版文件已存在，未重复保存');
        } else if (slot.role === 'draft') {
          const label = d.draft_pdfs?.[0]?.label || shortName(file.name);
          state.lastUploadMessage = `✓ 已上传「${shortName(label)}」`;
          state.lastUploadRole = slot.role;
          helpers.toast(d.pdf_replaced || d.draft_appended === false ? '修订版已更换' : '修订版已上传');
        } else if (d.pdf_replaced) {
          state.lastUploadMessage = `✓ 已更换「${shortName(file.name)}」`;
          state.lastUploadRole = slot.role;
          helpers.toast(
            cfg.onUploadSuccess
              ? '完整版已更新，即将自动识别目录 → 划分页码 → 生成页图'
              : '完整版已更新。旧目录/页码/页图已清空，请重新「识别目录」→「划分页码」→「生成页图」',
          );
        } else {
          state.lastUploadMessage = `✓ ${slot.doneLabel}`;
          state.lastUploadRole = slot.role;
          helpers.toast(
            cfg.onUploadSuccess && slot.role === 'full'
              ? '完整版已上传，即将自动识别目录 → 划分页码 → 生成页图'
              : slot.doneLabel,
          );
        }
        await cfg.reloadVolume();
        if (typeof cfg.onUploadSuccess === 'function') {
          try {
            await cfg.onUploadSuccess({
              role: slot.role,
              result: d,
              replaced: !!d.pdf_replaced,
            });
          } catch (hookErr) {
            helpers.toast(hookErr.message || String(hookErr));
          }
        }
      } catch (e) {
        state.lastUploadMessage = '';
        state.lastUploadRole = null;
        if (hintEl) {
          hintEl.hidden = false;
          hintEl.innerHTML = `<span class="pdf-upload-error">${helpers.escapeHtml(e.message || String(e))}</span>`;
        }
        helpers.toast(e.message || String(e));
      } finally {
        state.uploading = false;
        state.role = null;
        if (input) input.disabled = false;
        refreshAll(cfg.getVolumeData());
      }
    }

    function refreshClearFullBtn(data) {
      const btn = document.getElementById('clear-full-pdf-btn');
      if (!btn) return;
      const hasFull = !!(data && data.has_pdf);
      btn.hidden = !hasFull;
      btn.disabled = !!(state.uploading);
    }

    async function clearFullPdf() {
      const volumeCode = cfg.getVolumeCode ? cfg.getVolumeCode() : cfg.volumeCode;
      if (!volumeCode) {
        helpers.toast('未选择册次');
        return;
      }
      if (!window.confirm('确定清除完整版 PDF？目录课时会保留，页图将清空；修订版不受影响。')) {
        return;
      }
      const btn = document.getElementById('clear-full-pdf-btn');
      if (btn) btn.disabled = true;
      try {
        const r = await fetch(
          `${cfg.apiPrefix}/volumes/${encodeURIComponent(volumeCode)}/pdf/full`,
          { method: 'DELETE' },
        );
        const d = await cfg.readJson(r);
        if (!r.ok || !d.ok) throw new Error(d.error || `清除失败（HTTP ${r.status}）`);
        helpers.toast(d.cleared ? '完整版已清除' : '当前没有完整版');
        await cfg.reloadVolume();
      } catch (e) {
        helpers.toast(e.message || String(e));
      } finally {
        refreshAll(cfg.getVolumeData());
      }
    }

    function refreshAll(data) {
      activeSlots.forEach((slot) => {
        refreshSlot(slot, data, state, helpers);
        renderMeta(slot, data, helpers);
        refreshCompareBtn(slot, data, cfg);
        if (slot.role === 'draft') {
          renderDraftSummary(slot, data, state, helpers);
          renderDraftList(slot, data, cfg, helpers);
        }
      });
      refreshClearFullBtn(data);
    }

    const clearFullBtn = document.getElementById('clear-full-pdf-btn');
    if (clearFullBtn) {
      clearFullBtn.addEventListener('click', () => {
        clearFullPdf();
      });
    }

    activeSlots.forEach((slot) => {
      const input = document.getElementById(slot.inputId);
      if (!input) return;

      input.addEventListener('change', () => {
        const file = input.files?.[0];
        if (!file) return;
        if (slot.autoUpload) {
          state.lastUploadMessage = '';
          state.lastUploadRole = null;
          uploadFile(slot, file);
          return;
        }
        state.pending = true;
        state.pendingRole = slot.role;
        state.pendingName = file.name;
        refreshAll(cfg.getVolumeData());
      });

      if (!slot.btnId) return;
      const btn = document.getElementById(slot.btnId);
      if (!btn) return;

      btn.addEventListener('click', async () => {
        const file = input.files?.[0];
        if (!file) {
          input.click();
          return;
        }
        await uploadFile(slot, file);
      });
    });

    refreshAll(cfg.getVolumeData());
    return { refresh: refreshAll };
  }

  global.IntakeDualPdf = { init, SLOTS };
})(window);
