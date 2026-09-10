/* 教材库课级 work — 单侧教材 OCR（无课件） */
(function () {
  const lessonUid = window.LESSON_UID || '';
  const $ = (id) => document.getElementById(id);

  let pages = [];
  let pageIndex = 1;
  let atoms = [];
  let textDone = false;
  let imageDone = false;
  let aiCurated = false;
  let blockCount = 0;
  let pageBlocks = [];
  let figureCentric = false;
  let semanticBlocks = false;
  let ocrBusy = false;
  let volumeCode = '';

  function setStatus(msg, isError) {
    const el = $('lw-status');
    if (!el) return;
    if (!msg) {
      el.hidden = true;
      el.textContent = '';
      return;
    }
    el.hidden = false;
    el.textContent = msg;
    el.style.color = isError ? '#b91c1c' : '';
  }

  function escapeHtml(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /** JSON \\f 被吃成换页符后界面像 ↑rac；顺带修箭头污染 */
  function repairLatexCtrl(s) {
    return String(s ?? '')
      .replace(/\u000c/g, '\\f')
      .replace(/\u0008/g, '\\b')
      .replace(/[↑⬆]\s*rac\b/g, '\\frac')
      .replace(/[↑⬆]\s*begin\b/g, '\\begin')
      .replace(/[↑⬆]\s*beta\b/g, '\\beta');
  }

  /** 转义普通文字，并把 $...$ / $$...$$ 交给 KaTeX 渲染 */
  function formatAtomHtml(raw) {
    const text = repairLatexCtrl(raw);
    if (!text) return '';
    if (typeof katex === 'undefined') {
      return escapeHtml(text).replace(/\n/g, '<br>');
    }
    const parts = [];
    const re = /\$\$([\s\S]+?)\$\$|\$([^$\n]+?)\$/g;
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) {
        parts.push(escapeHtml(text.slice(last, m.index)).replace(/\n/g, '<br>'));
      }
      const display = m[1] != null;
      const tex = display ? m[1] : m[2];
      try {
        parts.push(
          katex.renderToString(tex, {
            throwOnError: false,
            displayMode: display,
            strict: 'ignore',
          }),
        );
      } catch {
        parts.push(escapeHtml(m[0]));
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) {
      parts.push(escapeHtml(text.slice(last)).replace(/\n/g, '<br>'));
    }
    return parts.join('');
  }

  function atomBbox(a) {
    if (a.bbox && typeof a.bbox === 'object') return a.bbox;
    return {
      x_start: a.x_start,
      x_end: a.x_end,
      y_start: a.y_start,
      y_end: a.y_end,
    };
  }

  function atomPlain(a) {
    const t = String(a.atom_type || '').trim();
    // 插图主题以 content 为准，避免旧 display_text 盖住真实命名
    const sources =
      t === 'image' || t === 'figure'
        ? [a.content, a.ocr_text, a.display_text, a.text]
        : [a.display_text, a.ocr_text, a.content, a.text];
    let raw = '';
    for (const v of sources) {
      const s = String(v || '').trim();
      if (s) {
        raw = s;
        break;
      }
    }
    if (raw) {
      raw = repairLatexCtrl(raw);
      // 版面 LLM：content 形如「[插图] 主题描述」；空占位不算已命名
      const m = raw.match(/^\[(?:插图|image|图片)\]\s*(.*)$/i);
      if (m) {
        const theme = (m[1] || '').trim();
        return theme || '插图（识别未命名，请重跑图片 OCR）';
      }
      if (
        raw === '插图' ||
        raw === '插图（未命名）' ||
        raw === 'image' ||
        raw === 'figure' ||
        raw === '插图（识别未命名，请重跑图片 OCR）'
      ) {
        return '插图（识别未命名，请重跑图片 OCR）';
      }
      return raw;
    }
    if (t === 'image' || t === 'figure') {
      return '插图（识别未命名，请重跑图片 OCR）';
    }
    return t;
  }

  function blockKindLabel(b) {
    const sk = String(b.semantic_kind || '').trim();
    if (sk === 'legend') return '栏目';
    if (sk === 'figure') return '图块';
    if (sk === 'text') return '正文';
    if (sk === 'activity') return '活动';
    if (sk === 'other') return '其它';
    if (b.kind === 'figure') return '图块';
    if (b.kind === 'legend_row') return '栏目行';
    return '正文';
  }

  function buildBlocksButtonLabel() {
    if (blockCount > 0) return `建块（${blockCount}）`;
    if (semanticBlocks) return '语义建块';
    if (figureCentric) return '图核建块';
    return '建块';
  }

  function buildBlocksButtonTitle() {
    if (semanticBlocks) return '按页语义分组；可跨页合并或续页';
    if (figureCentric) return '图 + 图下图注各成一块；正文按段落切岛';
    return '导入原子并按栏目建块';
  }

  function refreshButtons() {
    const hasPage = pages.length > 0 && pages[pageIndex - 1];
    const busy = ocrBusy;
    const textBtn = $('btn-ocr-text');
    const imgBtn = $('btn-ocr-images');
    const lessonBtn = $('btn-ocr-lesson-full');
    const curateBtn = $('btn-curate-lesson');
    const blocksBtn = $('btn-build-blocks');
    if (textBtn) {
      textBtn.disabled = !hasPage || busy;
      textBtn.textContent = busy === 'text' ? '文字 OCR 中…' : '文字 OCR';
    }
    if (imgBtn) {
      imgBtn.disabled = !hasPage || busy || (!textDone && !atoms.some((a) => (a.atom_type || '') !== 'image'));
      imgBtn.textContent = busy === 'images' ? '图片 OCR 中…' : '图片 OCR';
    }
    if (lessonBtn) {
      lessonBtn.disabled = !pages.length || !!busy;
      lessonBtn.textContent = busy === 'lesson-full' ? '整课 OCR 中…' : '整课 OCR';
    }
    const lessonTextDone = pages.length > 0 && pages.every((p) => p.text_ocr_done);
    const lessonImageDone = pages.length > 0 && pages.every((p) => p.image_ocr_done);
    if (curateBtn) {
      // 语义建块与化学图核均不依赖 AI 整理；保持隐藏
      const hideCurate = !!semanticBlocks || !!figureCentric;
      curateBtn.hidden = hideCurate;
      curateBtn.disabled = !pages.length || !!busy || !lessonTextDone || hideCurate;
      curateBtn.textContent = busy === 'curate' ? 'AI 整理中…' : 'AI 整理';
    }
    if (blocksBtn) {
      blocksBtn.disabled = !pages.length || !!busy || !lessonTextDone || !lessonImageDone;
      blocksBtn.textContent = busy === 'blocks'
        ? (semanticBlocks ? '语义建块中…' : '建块中…')
        : buildBlocksButtonLabel();
      blocksBtn.title = buildBlocksButtonTitle();
    }
    const prev = $('prev-page-btn');
    const next = $('next-page-btn');
    if (prev) prev.disabled = pageIndex <= 1 || !!busy;
    if (next) next.disabled = pageIndex >= pages.length || !!busy;
  }

  function atomCode(a) {
    return String(a.atom_code || a.atom_id || '').trim();
  }

  function blockForAtom(code) {
    if (!code) return null;
    return pageBlocks.find((b) => (b.atom_codes || []).includes(code)) || null;
  }

  const BLOCK_HUES = [210, 330, 145, 35, 270, 185, 15, 250];

  function blockHue(blockCode, kind) {
    const idx = pageBlocks.findIndex((b) => b.block_code === blockCode);
    const base = BLOCK_HUES[(idx >= 0 ? idx : 0) % BLOCK_HUES.length];
    return kind === 'figure' ? base : (base + 40) % 360;
  }

  function isFigurePart(a) {
    return Boolean(String(a.parent_figure_code || '').trim()) || a.figure_role === 'part';
  }

  function isFigureParent(a) {
    if (a.figure_role === 'parent') return true;
    const parts = a.part_atom_codes;
    return (
      (a.atom_type === 'image' || a.atom_type === 'figure')
      && Array.isArray(parts)
      && parts.length > 0
    );
  }

  function isPageChrome(a) {
    return a.page_chrome === true || a.figure_role === 'page_chrome';
  }

  function isTableAtom(a) {
    return (a.atom_type || '') === 'table' || Boolean(a.table_grid && a.table_grid.columns);
  }

  function isTableSuperseded(a) {
    return a.table_superseded === true;
  }

  function renderTableGridHtml(grid) {
    const cols = Array.isArray(grid.columns) ? grid.columns : [];
    const rows = Array.isArray(grid.rows) ? grid.rows : [];
    if (!cols.length) return '';
    const head = cols.map((c) => `<th>${escapeHtml(String(c || ''))}</th>`).join('');
    const body = rows.map((row) => {
      const cells = cols.map((_, i) => {
        const val = Array.isArray(row) ? (row[i] ?? '') : '';
        return `<td>${escapeHtml(String(val || ''))}</td>`;
      }).join('');
      return `<tr>${cells}</tr>`;
    }).join('');
    return `<table class="lw-table-grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function atomTypeLabel(a) {
    if (isTableAtom(a)) return '表格';
    if (a.part_kind === 'interior') return '图内说明';
    const isImg = (a.atom_type || '') === 'image' || (a.atom_type || '') === 'figure';
    if (isImg) return '插图';
    if (isFigurePart(a)) return '散件';
    return escapeHtml(a.atom_type || 'text');
  }

  function atomListItemHtml(a, opts = {}) {
    const { indent = false, extraClass = '' } = opts;
    if (isTableSuperseded(a)) return '';
    const isImg = (a.atom_type || '') === 'image' || (a.atom_type || '') === 'figure';
    const isTable = isTableAtom(a);
    const typeLabel = atomTypeLabel(a);
    let text = formatAtomHtml(atomPlain(a) || '—');
    if (isTable && a.table_grid) {
      text = renderTableGridHtml(a.table_grid);
    }
    if (a.figure_role === 'unbound_part') {
      text += ' <span class="lw-atom-badge lw-atom-badge-unbound">待挂接</span>';
    }
    const cls = [
      'lw-atom-row',
      indent ? 'lw-atom-part' : '',
      isImg ? 'lw-atom-figure' : '',
      isTable ? 'lw-atom-table' : '',
      extraClass,
    ].filter(Boolean).join(' ');
    const code = atomCode(a);
    const cropUrl = String(a.crop_url || '').trim();
    const cropHtml = isImg && cropUrl
      ? `<img class="lw-atom-crop" src="${escapeHtml(cropUrl)}" alt="${escapeHtml(atomPlain(a) || '插图')}" loading="lazy">`
      : '';
    return (
      `<li class="${cls}" data-atom-code="${escapeHtml(code)}">`
      + cropHtml
      + `<div class="lw-atom-body"><span class="atom-type">${typeLabel}</span>${text}</div>`
      + `</li>`
    );
  }

  function buildPartsIndex(atomsByCode) {
    const partsByParent = {};
    Object.values(atomsByCode).forEach((a) => {
      const parent = String(a.parent_figure_code || '').trim();
      if (!parent) return;
      (partsByParent[parent] ||= []).push(a);
    });
    return partsByParent;
  }

  function sortAtomsByBbox(list) {
    return [...list].sort((a, b) => {
      const ba = atomBbox(a);
      const bb = atomBbox(b);
      const dy = (Number(ba.y_start) || 0) - (Number(bb.y_start) || 0);
      if (Math.abs(dy) > 0.01) return dy;
      return (Number(ba.x_start) || 0) - (Number(bb.x_start) || 0);
    });
  }

  function renderGroupedAtomRows(codes, atomsByCode, partsByParent, claimedParts) {
    const rows = [];
    sortAtomsByBbox(codes.map((c) => atomsByCode[c]).filter(Boolean)).forEach((a) => {
      const code = atomCode(a);
      if (isPageChrome(a)) return;
      if (isTableSuperseded(a)) return;
      if (claimedParts.has(code)) return;
      rows.push(atomListItemHtml(a));
      const partCodes = new Set([
        ...(Array.isArray(a.part_atom_codes) ? a.part_atom_codes : []),
        ...(partsByParent[code] || []).map((p) => atomCode(p)),
      ]);
      sortAtomsByBbox([...partCodes].map((c) => atomsByCode[c]).filter(Boolean)).forEach((p) => {
        claimedParts.add(atomCode(p));
        rows.push(atomListItemHtml(p, { indent: true }));
      });
    });
    return rows.join('');
  }

  let overlayBoxes = new Map();

  function clearFigureHighlight() {
    overlayBoxes.forEach((box) => {
      box.classList.remove('is-linked', 'lw-atom-parent-highlight');
    });
  }

  function highlightFigureGroup(parentCode) {
    clearFigureHighlight();
    const parentBox = overlayBoxes.get(parentCode);
    if (parentBox) parentBox.classList.add('lw-atom-parent-highlight');
    const parentAtom = atoms.find((a) => atomCode(a) === parentCode);
    if (!parentAtom) return;
    const partCodes = new Set([
      ...(Array.isArray(parentAtom.part_atom_codes) ? parentAtom.part_atom_codes : []),
      ...atoms.filter((a) => String(a.parent_figure_code || '').trim() === parentCode).map((a) => atomCode(a)),
    ]);
    partCodes.forEach((c) => {
      const box = overlayBoxes.get(c);
      if (box) box.classList.add('is-linked');
    });
  }

  function renderAtomOverlay() {
    const overlay = $('atom-overlay');
    const toggle = $('toggle-atom-overlay');
    if (!overlay) return;
    overlay.innerHTML = '';
    const show = toggle ? toggle.checked : true;
    if (!show || !atoms.length) {
      overlay.hidden = true;
      return;
    }
    overlay.hidden = false;
    overlayBoxes = new Map();
    atoms.forEach((a) => {
      if (isTableSuperseded(a)) return;
      const bb = atomBbox(a);
      const x0 = Number(bb.x_start);
      const x1 = Number(bb.x_end);
      const y0 = Number(bb.y_start);
      const y1 = Number(bb.y_end);
      if (![x0, x1, y0, y1].every((n) => Number.isFinite(n))) return;
      const box = document.createElement('div');
      const isImg = (a.atom_type || '') === 'image' || (a.atom_type || '') === 'figure';
      const isTable = isTableAtom(a);
      const code = atomCode(a);
      const blk = blockForAtom(code);
      box.className = `lw-atom-box${isImg ? ' is-image' : ''}${isTable ? ' is-table' : ''}${blk ? ' is-blocked' : ''}`;
      if (blk) {
        const hue = blockHue(blk.block_code, blk.kind);
        const solid = blk.kind === 'figure';
        box.style.borderColor = `hsla(${hue}, 72%, 42%, ${solid ? 0.9 : 0.75})`;
        box.style.background = `hsla(${hue}, 72%, 52%, ${solid ? 0.14 : 0.1})`;
        box.style.borderWidth = solid ? '2px' : '1.5px';
      }
      box.style.left = `${x0 * 100}%`;
      box.style.top = `${y0 * 100}%`;
      box.style.width = `${Math.max(0.5, (x1 - x0) * 100)}%`;
      box.style.height = `${Math.max(0.5, (y1 - y0) * 100)}%`;
      const titleBits = [];
      if (blk) titleBits.push(`${blk.block_code} ${blk.block_name || ''}`.trim());
      if (isFigurePart(a) && a.parent_figure_code) {
        titleBits.push(`所属图 ${a.parent_figure_code}`);
      }
      titleBits.push(atomPlain(a).slice(0, 120));
      box.title = titleBits.filter(Boolean).join(' · ');
      box.dataset.atomCode = code;
      if (isFigureParent(a)) {
        box.addEventListener('mouseenter', () => highlightFigureGroup(code));
        box.addEventListener('mouseleave', clearFigureHighlight);
      } else if (isFigurePart(a)) {
        const parent = String(a.parent_figure_code || '').trim();
        if (parent) {
          box.addEventListener('mouseenter', () => highlightFigureGroup(parent));
          box.addEventListener('mouseleave', clearFigureHighlight);
        }
      }
      overlayBoxes.set(code, box);
      overlay.appendChild(box);
    });
  }

  function renderAtomList() {
    const list = $('atom-list');
    const meta = $('atoms-meta');
    if (!list) return;
    const flags = [
      textDone ? '文字已 OCR' : '文字未 OCR',
      imageDone ? '图片已 OCR' : '图片未 OCR',
      aiCurated ? '已 AI 整理' : '未整理',
      blockCount > 0 ? `${blockCount} 块` : '未建块',
      pageBlocks.length ? `本页 ${pageBlocks.length} 块` : null,
      `${atoms.length} 个原子`,
    ].filter(Boolean).join(' · ');
    if (meta) meta.textContent = flags;
    if (!atoms.length) {
      list.innerHTML = '<li class="hint">暂无原子。点「文字 OCR」开始。</li>';
      return;
    }
    const atomsByCode = Object.fromEntries(
      atoms.map((a) => [atomCode(a), a]).filter(([c]) => c),
    );
    const partsByParent = buildPartsIndex(atomsByCode);
    const claimedParts = new Set();
    if (pageBlocks.length) {
      const claimed = new Set();
      const html = pageBlocks.map((b) => {
        const codes = (b.atom_codes || []).filter((c) => atomsByCode[c]);
        codes.forEach((c) => claimed.add(c));
        const excerpt = (b.legend_excerpt || '').trim();
        if (!codes.length && !excerpt) return '';
        const kindLabel = blockKindLabel(b);
        let items = '';
        if (excerpt) {
          items = `<li><span class="atom-type">说明</span>${escapeHtml(excerpt)}</li>`;
        } else {
          items = renderGroupedAtomRows(codes, atomsByCode, partsByParent, claimedParts);
        }
        if (excerpt && codes.length) {
          const figCodes = codes.filter((c) => {
            const a = atomsByCode[c];
            return (a.atom_type || '') === 'image' || (a.atom_type || '') === 'figure';
          });
          const extra = renderGroupedAtomRows(figCodes, atomsByCode, partsByParent, claimedParts);
          items = extra + items;
        }
        const name = escapeHtml(b.block_name || b.block_code || '');
        return (
          `<li class="lw-block-group">`
          + `<div class="lw-block-head">`
          + `<span class="lw-block-code">${escapeHtml(b.block_code || '')}</span>`
          + `<span class="lw-block-kind">${kindLabel}</span>`
          + `<span class="lw-block-name">${name}</span>`
          + `</div>`
          + `<ul class="lw-block-atoms">${items}</ul>`
          + `</li>`
        );
      }).join('');
      const orphanCodes = atoms
        .filter((a) => !claimed.has(atomCode(a)) && !isTableSuperseded(a))
        .map((a) => atomCode(a))
        .filter(Boolean);
      const orphan = renderGroupedAtomRows(orphanCodes, atomsByCode, partsByParent, claimedParts);
      list.innerHTML = html + (orphan ? `<li class="lw-block-group lw-block-orphan"><div class="lw-block-head"><span class="lw-block-kind">未分块</span></div><ul class="lw-block-atoms">${orphan}</ul></li>` : '');
      return;
    }
    const orderedCodes = sortAtomsByBbox(atoms.filter((a) => !isTableSuperseded(a)))
      .map((a) => atomCode(a))
      .filter(Boolean);
    list.innerHTML = renderGroupedAtomRows(orderedCodes, atomsByCode, partsByParent, claimedParts)
      || '<li class="hint">暂无可见原子。</li>';
  }

  function showPage() {
    const img = $('page-img');
    const empty = $('page-empty');
    const nav = $('page-nav');
    const label = $('page-nav-label');
    const cur = pages[pageIndex - 1];
    if (!cur || !cur.url) {
      if (img) {
        img.hidden = true;
        img.removeAttribute('src');
      }
      if (empty) empty.hidden = false;
      if (nav) nav.hidden = true;
      atoms = [];
      textDone = false;
      imageDone = false;
      renderAtomOverlay();
      renderAtomList();
      refreshButtons();
      return;
    }
    if (empty) empty.hidden = true;
    if (img) {
      img.hidden = false;
      img.src = cur.url;
    }
    if (nav) nav.hidden = pages.length < 2;
    if (label) {
      const pdf = cur.pdf_page != null ? `（PDF p${cur.pdf_page}）` : '';
      label.textContent = `${pageIndex} / ${pages.length}${pdf}`;
    }
    renderAtomOverlay();
    renderAtomList();
    refreshButtons();
  }

  async function loadWork(pi) {
    setStatus('加载中…');
    const q = new URLSearchParams({ page_index: String(pi || 1) });
    const res = await fetch(`/api/textbook-library/lessons/${encodeURIComponent(lessonUid)}/work?${q}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      setStatus(data.error || '加载失败', true);
      refreshButtons();
      return;
    }
    pages = data.pages || [];
    pageIndex = data.page_index || 1;
    volumeCode = data.volume_code || '';
    atoms = data.atoms || [];
    textDone = !!data.text_ocr_done;
    imageDone = !!data.image_ocr_done;
    aiCurated = !!data.ai_curated;
    blockCount = Number(data.block_count) || 0;
    pageBlocks = data.page_blocks || [];
    figureCentric = !!data.figure_centric_blocks;
    semanticBlocks = !!data.semantic_blocks;

    const title = $('page-title');
    const sub = $('page-sub');
    const name = [data.lesson_no, data.lesson_name].filter(Boolean).join(' ') || '课时标注';
    if (title) title.textContent = name;
    if (sub) {
      const bits = [];
      if (volumeCode) bits.push(volumeCode);
      if (data.unit_title) bits.push(data.unit_title);
      if (data.page_start && data.page_end) bits.push(`p${data.page_start}–${data.page_end}`);
      sub.textContent = bits.join(' · ') || '单侧教材 OCR';
    }
    const back = $('lw-back');
    if (back && data.intake_url) back.href = data.intake_url;
    else if (back && window.INTAKE_RETURN_URL) back.href = window.INTAKE_RETURN_URL;

    setStatus('');
    showPage();
  }

  async function runOcr(phase, opts) {
    if (ocrBusy || !lessonUid) return;
    const allPages = !!(opts && opts.allPages);
    const phases =
      opts && Array.isArray(opts.phases) && opts.phases.length
        ? opts.phases
        : null;
    const cur = pages[pageIndex - 1];
    if (!allPages && (!cur || !cur.pdf_page)) {
      setStatus('当前页无 PDF 页码', true);
      return;
    }
    ocrBusy = allPages ? 'lesson-full' : phase;
    refreshButtons();
    if (allPages) {
      setStatus('整课 OCR 进行中（逐页文字+图片）…');
    } else {
      setStatus(`${phase === 'text' ? '文字' : '图片'} OCR 进行中…`);
    }
    try {
      const body = allPages
        ? {
            all_pages: true,
            skip_cached: true,
            phases: phases || ['text', 'images'],
          }
        : {
            phase,
            page_index: cur.page_index,
            pdf_page: cur.pdf_page,
            skip_cached: false,
          };
      const res = await fetch(
        `/api/textbook-library/lessons/${encodeURIComponent(lessonUid)}/ocr`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        },
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setStatus(data.error || 'OCR 失败', true);
        return;
      }
      if (allPages) {
        const ph = (data.phases || ['text', 'images']).join('+');
        setStatus(`整课完成（${ph}）：${data.page_count || 0} 页`);
        await loadWork(pageIndex);
        return;
      }
      atoms = data.atoms || [];
      textDone = !!data.text_ocr_done;
      imageDone = !!data.image_ocr_done;
      if (cur) {
        cur.text_ocr_done = textDone;
        cur.image_ocr_done = imageDone;
        cur.atom_count = data.atom_count || atoms.length;
      }
      const ran = (data.ran || []).join(',') || 'ok';
      setStatus(`完成（${ran}）：${atoms.length} 个原子`);
      renderAtomOverlay();
      renderAtomList();
    } catch (err) {
      setStatus(String(err.message || err), true);
    } finally {
      ocrBusy = false;
      refreshButtons();
    }
  }

  async function runCurate() {
    if (ocrBusy || !lessonUid) return;
    ocrBusy = 'curate';
    refreshButtons();
    setStatus('整课 AI 整理进行中…');
    try {
      const res = await fetch(
        `/api/textbook-library/lessons/${encodeURIComponent(lessonUid)}/curate`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ skip_curated: true }),
        },
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setStatus(data.error || 'AI 整理失败', true);
        return;
      }
      setStatus(`AI 整理完成：${data.page_count || 0} 页`);
      await loadWork(pageIndex);
    } catch (err) {
      setStatus(String(err.message || err), true);
    } finally {
      ocrBusy = false;
      refreshButtons();
    }
  }

  async function runBuildBlocks() {
    if (ocrBusy || !lessonUid) return;
    const replace = blockCount > 0
      && window.confirm('本课已有区块，是否覆盖重建？');
    if (blockCount > 0 && !replace) return;
    ocrBusy = 'blocks';
    refreshButtons();
    setStatus(
      semanticBlocks
        ? '语义建块进行中（按页分组；可跨页合并或续页）…'
        : (figureCentric
          ? '图核建块进行中（图+图注）…'
          : '建块进行中（导入原子 → 栏目聚类）…'),
    );
    try {
      const res = await fetch(
        `/api/textbook-library/lessons/${encodeURIComponent(lessonUid)}/blocks`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ replace_existing: replace }),
        },
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        setStatus(data.error || '建块失败', true);
        return;
      }
      const n = data.block_count || (data.blocks && data.blocks.block_count) || 0;
      setStatus(`建块完成：${n} 块`);
      await loadWork(pageIndex);
    } catch (err) {
      setStatus(String(err.message || err), true);
    } finally {
      ocrBusy = false;
      refreshButtons();
    }
  }

  function bind() {
    $('btn-ocr-text')?.addEventListener('click', () => runOcr('text'));
    $('btn-ocr-images')?.addEventListener('click', () => runOcr('images'));
    $('btn-ocr-lesson-full')?.addEventListener('click', () =>
      runOcr('all', { allPages: true, phases: ['text', 'images'] }),
    );
    $('btn-curate-lesson')?.addEventListener('click', () => runCurate());
    $('btn-build-blocks')?.addEventListener('click', () => runBuildBlocks());
    $('toggle-atom-overlay')?.addEventListener('change', renderAtomOverlay);
    $('prev-page-btn')?.addEventListener('click', () => {
      if (pageIndex > 1) loadWork(pageIndex - 1);
    });
    $('next-page-btn')?.addEventListener('click', () => {
      if (pageIndex < pages.length) loadWork(pageIndex + 1);
    });
    $('page-img')?.addEventListener('load', renderAtomOverlay);
  }

  bind();
  if (!lessonUid) {
    setStatus('缺少 lesson_uid', true);
  } else {
    loadWork(1);
  }
})();
