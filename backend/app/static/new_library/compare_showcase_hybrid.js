/**
 * 融合方案（默认）：豆包轨道粒子 + 经典叙事大屏 + 侧栏卡片飞入
 * ?showcase=hybrid | classic | doubao
 */
(function () {
  const TIMELINE = {
    oldFlyBegin: 600,
    oldEnd: 4200,
    newBegin: 4500,
    newFlyBegin: 5000,
    newEnd: 8400,
    scanEnd: 12200,
    matchEnd: 16200,
    jumpAt: 17600,
  };

  let hySpeed = 1.45;
  let orbMap = { old: {}, new: {} };

  function CS() {
    return window.CompareShowcase;
  }

  function t(ms) {
    return Math.round(ms / hySpeed);
  }

  function wait(ms, token) {
    const cs = CS();
    return new Promise((resolve) => {
      setTimeout(() => resolve(token === cs.getToken()), t(ms));
    });
  }

  function shortBlockLabel(block, side, index) {
    const id = block?.block_id || '';
    const m = id.match(/([BN])(\d+)/i);
    if (m) return `${m[1].toUpperCase()}${m[2].padStart(2, '0')}`;
    return `${side === 'old' ? 'B' : 'N'}${String(index + 1).padStart(2, '0')}`;
  }

  function getCanvasMetrics() {
    const canvas = document.getElementById('db-orbit-canvas');
    if (!canvas) return { cx: 280, cy: 200, rx: 200, ry: 100, w: 560, h: 320 };
    const w = canvas.clientWidth || 560;
    const h = canvas.clientHeight || 320;
    return { cx: w / 2, cy: h * 0.62, rx: w * 0.4, ry: h * 0.28, w, h };
  }

  function orbitPosition(index, total, layer) {
    const { cx, cy, rx, ry } = getCanvasMetrics();
    const base = (index / Math.max(1, total)) * Math.PI * 2 - Math.PI / 2;
    const layerOff = layer === 'new' ? 0.14 : -0.14;
    const angle = base + layerOff;
    return { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle), angle, rx };
  }

  function orbCenter(el) {
    const canvas = document.getElementById('db-orbit-canvas');
    if (!el || !canvas) return { x: 0, y: 0 };
    const cr = canvas.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    return { x: er.left - cr.left + er.width / 2, y: er.top - cr.top + er.height / 2 };
  }

  function clearOrbit() {
    document.getElementById('db-orbit-blocks')?.classList.remove('is-spinning');
    document.getElementById('db-orbit-blocks')?.replaceChildren();
    document.getElementById('db-orbit-svg')?.querySelectorAll('line').forEach((l) => l.remove());
    orbMap = { old: {}, new: {} };
    ['db-old-count', 'db-new-count', 'db-pair-count'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.textContent = '0';
    });
    document.getElementById('db-radar')?.classList.remove('is-active', 'is-frozen');
    document.getElementById('showcase-doubao-stage')?.classList.remove('is-finale', 'is-flash');
  }

  function spawnOrb(block, side, index, total, token) {
    const cs = CS();
    if (token !== cs.getToken()) return null;
    const container = document.getElementById('db-orbit-blocks');
    if (!container) return null;
    const pos = orbitPosition(index, total, side);
    const el = document.createElement('div');
    el.className = `db-orb db-orb-${side}`;
    el.textContent = shortBlockLabel(block, side, index);
    el.dataset.blockId = block.block_id;
    const metrics = getCanvasMetrics();
    const startX = side === 'old' ? -30 : metrics.w + 30;
    el.style.left = `${startX}px`;
    el.style.top = `${pos.y}px`;
    container.appendChild(el);
    requestAnimationFrame(() => {
      el.style.left = `${pos.x}px`;
      el.classList.add('is-entered', 'is-pause');
      setTimeout(() => {
        if (token !== cs.getToken()) return;
        el.classList.remove('is-pause');
        el.classList.add('is-orbit');
      }, t(380));
    });
    orbMap[side][block.block_id] = el;
    return el;
  }

  function drawLine(x1, y1, x2, y2, className) {
    const svg = document.getElementById('db-orbit-svg');
    if (!svg) return null;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', x1);
    line.setAttribute('y1', y1);
    line.setAttribute('x2', x2);
    line.setAttribute('y2', y2);
    line.setAttribute('class', className);
    svg.appendChild(line);
    return line;
  }

  function countUp(elId, target, duration = 500) {
    const el = document.getElementById(elId);
    if (!el) return Promise.resolve();
    const cs = CS();
    if (cs.prefersReducedMotion()) {
      el.textContent = String(target);
      return Promise.resolve();
    }
    const start = performance.now();
    const from = parseInt(el.textContent, 10) || 0;
    return new Promise((resolve) => {
      const tick = (now) => {
        const p = Math.min(1, (now - start) / t(duration));
        el.textContent = String(Math.round(from + (target - from) * (1 - (1 - p) ** 3)));
        if (p < 1) requestAnimationFrame(tick);
        else resolve();
      };
      requestAnimationFrame(tick);
    });
  }

  function setPhase(index) {
    document.querySelectorAll('.db-progress-dot').forEach((dot, i) => {
      dot.classList.toggle('is-active', i === index);
      dot.classList.toggle('is-done', i < index);
    });
  }

  function setHud(side, status) {
    ['old', 'new', 'pair'].forEach((s) => {
      const hud = document.getElementById(`db-hud-${s}`);
      if (!hud) return;
      if (!side) {
        hud.classList.remove('is-active', 'is-done');
        return;
      }
      hud.classList.toggle('is-active', s === side && status === 'active');
      hud.classList.toggle('is-done', s === side && status === 'done');
    });
  }

  function demoPairs(state) {
    const matches = (state?.matches || []).filter((m) => m.old_block_id && m.new_block_id);
    if (matches.length) return matches;
    const old = state?.old_blocks || [];
    const neu = state?.new_blocks || [];
    return Array.from({ length: Math.min(old.length, neu.length) }, (_, i) => ({
      old_block_id: old[i]?.block_id,
      new_block_id: neu[i]?.block_id,
    })).filter((p) => p.old_block_id && p.new_block_id);
  }

  function prepareHybridStage() {
    const panel = document.getElementById('pair-panel');
    panel?.classList.add('showcase-mode-hybrid');
    panel?.classList.remove('showcase-mode-doubao');
    const stage = document.getElementById('showcase-doubao-stage');
    if (stage) {
      stage.hidden = false;
      stage.setAttribute('aria-hidden', 'false');
    }
    const wrap = document.getElementById('hybrid-narrative-wrap');
    if (wrap) wrap.hidden = false;
    clearOrbit();
    setPhase(0);
    setHud(null);
  }

  function teardownHybridStage() {
    const panel = document.getElementById('pair-panel');
    panel?.classList.remove('showcase-mode-hybrid', 'is-speed-fast');
    const stage = document.getElementById('showcase-doubao-stage');
    if (stage) {
      stage.hidden = true;
      stage.setAttribute('aria-hidden', 'true');
    }
    const wrap = document.getElementById('hybrid-narrative-wrap');
    if (wrap) wrap.hidden = true;
    CS()?.renderHybrid?.('');
  }

  async function finishHybrid(token, { autoAudit = false } = {}) {
    const cs = CS();
    if (!cs || token !== cs.getToken()) {
      cs?.setRunning(false);
      return;
    }
    teardownHybridStage();
    cs.resetShowcaseSteps();
    await cs.finish(token, { autoAudit, variant: 'hybrid' });
  }

  async function confirmMatchVisual(pair, token) {
    const cs = CS();
    const oEl = orbMap.old[pair.old_block_id];
    const nEl = orbMap.new[pair.new_block_id];
    if (oEl && nEl) {
      const a = orbCenter(oEl);
      const b = orbCenter(nEl);
      drawLine(a.x, a.y, b.x, b.y, 'db-match-line is-flow');
      oEl.classList.add('is-lit', 'is-matched');
      nEl.classList.add('is-lit', 'is-matched');
      cs.highlightSideCard('old', pair.old_block_id);
      cs.highlightSideCard('new', pair.new_block_id);
      setTimeout(() => {
        oEl.classList.remove('is-lit');
        nEl.classList.remove('is-lit');
      }, t(420));
    }
  }

  async function runShowcaseAnimationHybrid() {
    const cs = CS();
    if (!cs || cs.getViewMode() !== 'pair') return;

    const token = cs.startShowcase();
    cs.resetShowcaseSteps();
    cs.prepareShowcaseLayout();
    prepareHybridStage();

    const panel = document.getElementById('pair-panel');
    panel?.classList.add('showcase-playing');
    panel?.classList.remove('showcase-complete', 'showcase-old-lit', 'showcase-new-lit');
    cs.renderShowcaseBanner();

    const state = cs.getState();
    const oldBlocks = state?.old_blocks || [];
    const newBlocks = state?.new_blocks || [];
    const pairs = demoPairs(state);
    const pairTotal = pairs.length || Math.min(oldBlocks.length, newBlocks.length);
    const firstNew = cs.demoFirstNewBlock();
    const candidates = cs.demoMatchCandidates(firstNew);

    document.getElementById('db-pair-total').textContent = String(pairTotal);
    const matchMeta = document.getElementById('showcase-match-meta');
    if (matchMeta) matchMeta.textContent = `0 / ${pairTotal}`;

    if (cs.prefersReducedMotion()) {
      await finishHybrid(token);
      return;
    }

    const t0 = performance.now();
    const waitUntil = (targetMs, tok) => {
      const elapsed = (performance.now() - t0) * hySpeed;
      const remaining = targetMs - elapsed;
      if (remaining <= 0) return Promise.resolve(tok === cs.getToken());
      return wait(remaining, tok);
    };

    const fallback = setTimeout(() => {
      if (cs.isRunning() && token === cs.getToken()) finishHybrid(token);
    }, t(TIMELINE.jumpAt + 2500));

    try {
      // ① 旧库拉取：叙事 + 侧栏飞入 + 轨道红球
      cs.setShowcaseStep(0);
      cs.setCaption('演示 · 旧库拉取（左侧飞入 + 轨道载入）');
      cs.renderHybrid(cs.simHtmlPullOld(oldBlocks.length));
      setPhase(0);
      setHud('old', 'active');
      panel?.classList.add('showcase-old-lit');
      cs.revealShowcaseCards('old', 220);
      cs.animateSimCounter('sim-counter-old', oldBlocks.length, t(TIMELINE.oldEnd - TIMELINE.oldFlyBegin));

      const oldWindow = TIMELINE.oldEnd - TIMELINE.oldFlyBegin - 400;
      const oldStagger = oldBlocks.length > 1 ? oldWindow / (oldBlocks.length - 1) : 0;
      for (let i = 0; i < oldBlocks.length; i += 1) {
        await waitUntil(TIMELINE.oldFlyBegin + i * oldStagger, token);
        if (token !== cs.getToken()) return;
        spawnOrb(oldBlocks[i], 'old', i, oldBlocks.length, token);
        countUp('db-old-count', i + 1, 380);
      }
      await waitUntil(TIMELINE.oldEnd, token);
      if (token !== cs.getToken()) return;
      setHud('old', 'done');

      // ② 新库拉取
      cs.setShowcaseStep(1);
      cs.setCaption('演示 · 新库拉取（右侧飞入 + 雷达扫描）');
      cs.renderHybrid(cs.simHtmlPullNew(newBlocks.length));
      setPhase(1);
      document.getElementById('db-radar')?.classList.add('is-active');
      panel?.classList.remove('showcase-old-lit');
      panel?.classList.add('showcase-new-lit');
      setHud('new', 'active');
      await waitUntil(TIMELINE.newBegin, token);
      if (token !== cs.getToken()) return;

      cs.revealShowcaseCards('new', 220);
      cs.animateSimCounter('sim-counter-new', newBlocks.length, t(TIMELINE.newEnd - TIMELINE.newFlyBegin));

      const newWindow = TIMELINE.newEnd - TIMELINE.newFlyBegin - 400;
      const newStagger = newBlocks.length > 1 ? newWindow / (newBlocks.length - 1) : 0;
      for (let i = 0; i < newBlocks.length; i += 1) {
        await waitUntil(TIMELINE.newFlyBegin + i * newStagger, token);
        if (token !== cs.getToken()) return;
        spawnOrb(newBlocks[i], 'new', i, newBlocks.length, token);
        countUp('db-new-count', i + 1, 380);
      }
      await waitUntil(TIMELINE.newEnd, token);
      if (token !== cs.getToken()) return;
      document.getElementById('db-orbit-blocks')?.classList.add('is-spinning');
      document.getElementById('db-radar')?.classList.add('is-frozen');
      setHud('new', 'done');

      // ③ 新区块扫描（经典叙事 + 轨道高亮）
      cs.setShowcaseStep(2);
      cs.setCaption(`演示 · 扫描新区块 ${firstNew.block_id}`);
      cs.renderHybrid(cs.simHtmlScanNew(firstNew));
      setPhase(2);
      cs.highlightSideCard('new', firstNew.block_id);
      const nOrb = orbMap.new[firstNew.block_id];
      if (nOrb) nOrb.classList.add('is-lit');
      document.getElementById('showcase-block-meta').textContent = firstNew.block_id;
      await waitUntil(TIMELINE.scanEnd, token);
      if (token !== cs.getToken()) return;
      if (nOrb) nOrb.classList.remove('is-lit');

      // ④ 全库匹配：叙事列表 + 轨道连线
      cs.setShowcaseStep(3);
      cs.setCaption('演示 · 旧库全库扫描匹配…');
      cs.renderHybrid(cs.simHtmlMatchOld(firstNew, candidates));
      setPhase(3);
      setHud('pair', 'active');
      cs.highlightSideCard('old', candidates[0]?.old);

      const matchSpan = TIMELINE.matchEnd - TIMELINE.scanEnd - 600;
      const visualPairs = pairs.slice(0, Math.min(pairTotal, 6));
      const matchStagger = visualPairs.length > 1 ? matchSpan / (visualPairs.length - 1) : matchSpan;
      for (let i = 0; i < visualPairs.length; i += 1) {
        await waitUntil(TIMELINE.scanEnd + 400 + i * matchStagger, token);
        if (token !== cs.getToken()) return;
        await confirmMatchVisual(visualPairs[i], token);
        const shown = Math.round(((i + 1) / visualPairs.length) * pairTotal);
        await countUp('db-pair-count', shown, 320);
        if (matchMeta) matchMeta.textContent = `${shown} / ${pairTotal}`;
      }
      if (pairTotal > visualPairs.length) {
        await countUp('db-pair-count', pairTotal, t(600));
        if (matchMeta) matchMeta.textContent = `${pairTotal} / ${pairTotal}`;
      }

      const stage = document.getElementById('showcase-doubao-stage');
      stage?.classList.add('is-flash');
      await waitUntil(TIMELINE.matchEnd, token);
      if (token !== cs.getToken()) return;
      stage?.classList.remove('is-flash');
      setHud('pair', 'done');

      // ⑤ 收尾
      cs.setCaption('配对展示完成 · 浏览左右连线');
      stage?.classList.add('is-finale');
      setPhase(3);
      document.querySelectorAll('.db-progress-dot').forEach((d) => d.classList.add('is-done'));
      await waitUntil(TIMELINE.jumpAt, token);
      if (token !== cs.getToken()) return;

      await finishHybrid(token, { autoAudit: false });
    } finally {
      clearTimeout(fallback);
    }
  }

  function skipHybrid() {
    CS()?.skipToOverviewComplete?.();
  }

  function toggleHybridSpeed() {
    const panel = document.getElementById('pair-panel');
    const btn = document.getElementById('btn-showcase-speed');
    const fast = panel?.classList.toggle('is-speed-fast');
    hySpeed = fast ? 1.47 : 1;
    if (btn) btn.textContent = fast ? '快速时长' : '标准时长';
  }

  function initHybridControls() {
    document.getElementById('btn-showcase-skip')?.addEventListener('click', () => {
      const panel = document.getElementById('pair-panel');
      if (panel?.classList.contains('showcase-mode-hybrid') && panel?.classList.contains('showcase-playing')) {
        skipHybrid();
      }
    });
    document.getElementById('btn-showcase-speed')?.addEventListener('click', () => {
      const panel = document.getElementById('pair-panel');
      if (panel?.classList.contains('showcase-mode-hybrid') && panel?.classList.contains('showcase-playing')) {
        toggleHybridSpeed();
      }
    });
  }

  window.runShowcaseAnimationHybrid = runShowcaseAnimationHybrid;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initHybridControls);
  } else {
    initHybridControls();
  }
})();
