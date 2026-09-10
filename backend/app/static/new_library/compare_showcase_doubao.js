/**
 * 豆包方案：12s 椭圆轨道粒子演示（纯前端，无真实接口）
 * 切换：?showcase=hybrid（默认） | ?showcase=classic | ?showcase=doubao
 */
(function () {
  const TIMELINE = {
    oldStart: 0,
    oldStream: 300,
    oldFlyBegin: 1000,
    oldFlyEnd: 3200,
    oldDone: 3500,
    newBegin: 3500,
    radarOn: 3500,
    newStream: 4000,
    newFlyBegin: 4500,
    newFlyEnd: 6500,
    newDone: 7000,
    matchBegin: 7000,
    matchFlash: 9800,
    matchDone: 10000,
    finaleBegin: 10000,
    jumpAt: 12000,
  };

  let dbSpeed = 1.45;
  let dbToken = 0;
  let orbMap = { old: {}, new: {} };

  function CS() {
    return window.CompareShowcase;
  }

  function t(ms) {
    return Math.round(ms / dbSpeed);
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
    const prefix = side === 'old' ? 'B' : 'N';
    return `${prefix}${String(index + 1).padStart(2, '0')}`;
  }

  function demoPairs(state) {
    const matches = (state?.matches || []).filter((m) => m.old_block_id && m.new_block_id);
    if (matches.length) return matches;
    const old = state?.old_blocks || [];
    const neu = state?.new_blocks || [];
    const n = Math.max(old.length, neu.length);
    return Array.from({ length: n }, (_, i) => ({
      old_block_id: old[i]?.block_id || null,
      new_block_id: neu[i]?.block_id || null,
    })).filter((p) => p.old_block_id && p.new_block_id);
  }

  function setCaption(text) {
    const el = document.getElementById('db-stage-caption');
    if (el) {
      el.textContent = text;
      el.style.animation = 'none';
      void el.offsetWidth;
      el.style.animation = '';
    }
    const tag = document.getElementById('showcase-tagline');
    if (tag) tag.textContent = text;
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
      hud.classList.toggle('is-active', s === side && status === 'active');
      hud.classList.toggle('is-done', s === side && status === 'done');
      const arrow = hud.querySelector('.db-hud-arrow');
      if (arrow) arrow.classList.toggle('is-flowing', s === side && status === 'active');
    });
  }

  function countUp(elId, target, duration = 600) {
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

  function getCanvasMetrics() {
    const canvas = document.getElementById('db-orbit-canvas');
    if (!canvas) return { cx: 280, cy: 160, rx: 200, ry: 110 };
    const w = canvas.clientWidth || 560;
    const h = canvas.clientHeight || 320;
    return { cx: w / 2, cy: h / 2, rx: w * 0.42, ry: h * 0.36, w, h };
  }

  function orbitPosition(index, total, layer) {
    const { cx, cy, rx, ry } = getCanvasMetrics();
    const base = (index / Math.max(1, total)) * Math.PI * 2 - Math.PI / 2;
    const layerOff = layer === 'new' ? 0.12 : -0.12;
    const angle = base + layerOff;
    return {
      x: cx + rx * Math.cos(angle),
      y: cy + ry * Math.sin(angle),
      angle,
      rx,
    };
  }

  function orbCenter(el) {
    const canvas = document.getElementById('db-orbit-canvas');
    if (!el || !canvas) return { x: 0, y: 0 };
    const cr = canvas.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    return {
      x: er.left - cr.left + er.width / 2,
      y: er.top - cr.top + er.height / 2,
    };
  }

  function clearOrbitStage() {
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
    const startX = side === 'old' ? -40 : getCanvasMetrics().w + 40;
    el.style.left = `${startX}px`;
    el.style.top = `${pos.y}px`;
    el.style.setProperty('--orbit-rx', `${pos.rx}px`);
    el.style.setProperty('--orbit-start', `${pos.angle}rad`);
    el.style.setProperty('--orbit-duration', `${22 + index * 0.8}s`);
    el.style.setProperty('--orbit-delay', `${index * 0.15}s`);
    container.appendChild(el);
    requestAnimationFrame(() => {
      el.style.left = `${pos.x}px`;
      el.classList.add('is-entered', 'is-pause');
      setTimeout(() => {
        if (token !== cs.getToken()) return;
        el.classList.remove('is-pause');
        el.classList.add('is-orbit');
      }, t(400));
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

  function runCrossScan(token) {
    const cs = CS();
    const oldIds = Object.keys(orbMap.old);
    const newIds = Object.keys(orbMap.new);
    if (!oldIds.length || !newIds.length) return;
    let i = 0;
    const max = Math.min(8, oldIds.length * newIds.length);
    const tick = () => {
      if (token !== cs.getToken()) return;
      const o = orbMap.old[oldIds[i % oldIds.length]];
      const n = orbMap.new[newIds[Math.floor(i / oldIds.length) % newIds.length]];
      if (o && n) {
        const a = orbCenter(o);
        const b = orbCenter(n);
        drawLine(a.x, a.y, b.x, b.y, 'db-scan-line');
      }
      i += 1;
      if (i < max) setTimeout(tick, t(180));
    };
    tick();
  }

  async function confirmMatch(pair, index, total, token) {
    const cs = CS();
    const oEl = orbMap.old[pair.old_block_id];
    const nEl = orbMap.new[pair.new_block_id];
    if (oEl && nEl) {
      const a = orbCenter(oEl);
      const b = orbCenter(nEl);
      drawLine(a.x, a.y, b.x, b.y, 'db-match-line is-flow');
      oEl.classList.add('is-lit', 'is-matched');
      nEl.classList.add('is-lit', 'is-matched');
      setTimeout(() => {
        oEl.classList.remove('is-lit');
        nEl.classList.remove('is-lit');
      }, t(500));
    }
    await countUp('db-pair-count', index + 1, t(280));
    const pairMeta = document.getElementById('showcase-match-meta');
    if (pairMeta) pairMeta.textContent = `${index + 1} / ${total}`;
  }

  function prepareDoubaoStage() {
    const panel = document.getElementById('pair-panel');
    const stage = document.getElementById('showcase-doubao-stage');
    const sim = document.getElementById('showcase-sim-screen');
    panel?.classList.add('showcase-mode-doubao');
    if (stage) {
      stage.hidden = false;
      stage.setAttribute('aria-hidden', 'false');
    }
    if (sim) sim.hidden = true;
    clearOrbitStage();
    setPhase(0);
    ['old', 'new', 'pair'].forEach((s) => {
      const hud = document.getElementById(`db-hud-${s}`);
      hud?.classList.remove('is-active', 'is-done');
      hud?.querySelector('.db-hud-arrow')?.classList.remove('is-flowing');
    });
  }

  function teardownDoubaoStage() {
    const panel = document.getElementById('pair-panel');
    panel?.classList.remove('showcase-mode-doubao', 'is-speed-fast');
    const stage = document.getElementById('showcase-doubao-stage');
    if (stage) {
      stage.hidden = true;
      stage.setAttribute('aria-hidden', 'true');
    }
    const sim = document.getElementById('showcase-sim-screen');
    if (sim) sim.hidden = false;
  }

  async function finishDoubao(token, { autoAudit = false } = {}) {
    const cs = CS();
    if (!cs || token !== cs.getToken()) {
      cs?.setRunning(false);
      return;
    }
    teardownDoubaoStage();
    cs.resetShowcaseSteps();
    await cs.finish(token, { autoAudit, variant: 'doubao' });
  }

  async function runShowcaseAnimationDoubao() {
    const cs = CS();
    if (!cs || cs.getViewMode() !== 'pair') return;

    const token = cs.startShowcase();
    dbToken = token;

    cs.resetShowcaseSteps();
    cs.prepareShowcaseLayout();
    prepareDoubaoStage();

    const panel = document.getElementById('pair-panel');
    panel?.classList.add('showcase-playing');
    panel?.classList.remove('showcase-complete');
    cs.renderShowcaseBanner();

    const state = cs.getState();
    const oldBlocks = state?.old_blocks || [];
    const newBlocks = state?.new_blocks || [];
    const pairs = demoPairs(state);
    const pairTotal = pairs.length || Math.min(oldBlocks.length, newBlocks.length);

    document.getElementById('db-pair-total').textContent = String(pairTotal);
    const oldMeta = document.getElementById('showcase-old-meta');
    const newMeta = document.getElementById('showcase-new-meta');
    const matchMeta = document.getElementById('showcase-match-meta');
    if (oldMeta) oldMeta.textContent = `${oldBlocks.length} 个旧块`;
    if (newMeta) newMeta.textContent = `${newBlocks.length} 个新块`;
    if (matchMeta) matchMeta.textContent = `0 / ${pairTotal}`;

    if (cs.prefersReducedMotion()) {
      await finishDoubao(token);
      return;
    }

    const t0 = performance.now();
    const waitUntil = (targetMs, tok) => {
      const elapsed = (performance.now() - t0) * dbSpeed;
      const remaining = targetMs - elapsed;
      if (remaining <= 0) return Promise.resolve(tok === cs.getToken());
      return wait(remaining, tok);
    };

    const fallback = setTimeout(() => {
      if (cs.isRunning() && token === cs.getToken()) finishDoubao(token);
    }, t(TIMELINE.jumpAt + 2000));

    try {
      setPhase(0);
      setCaption('演示 · 正在从旧库拉取区块…');
      setHud('old', 'active');
      await waitUntil(TIMELINE.oldStream, token);
      if (token !== cs.getToken()) return;

      const oldWindow = TIMELINE.oldFlyEnd - TIMELINE.oldFlyBegin;
      const oldStagger = oldBlocks.length > 1 ? oldWindow / (oldBlocks.length - 1) : 0;
      for (let i = 0; i < oldBlocks.length; i += 1) {
        await waitUntil(TIMELINE.oldFlyBegin + i * oldStagger, token);
        if (token !== cs.getToken()) return;
        spawnOrb(oldBlocks[i], 'old', i, oldBlocks.length, token);
        countUp('db-old-count', i + 1, 420);
      }
      await waitUntil(TIMELINE.oldDone, token);
      if (token !== cs.getToken()) return;

      document.querySelectorAll('.db-orb-old').forEach((el) => el.classList.add('is-pause'));
      setHud('old', 'done');
      setCaption('旧库区块载入完成');
      await waitUntil(TIMELINE.oldDone + 300, token);
      if (token !== cs.getToken()) return;

      setPhase(1);
      document.getElementById('db-radar')?.classList.add('is-active');
      setCaption('正在对新库执行扫描识别…');
      await waitUntil(TIMELINE.newBegin, token);
      if (token !== cs.getToken()) return;

      setHud('new', 'active');
      await waitUntil(TIMELINE.newStream, token);
      if (token !== cs.getToken()) return;

      const newWindow = TIMELINE.newFlyEnd - TIMELINE.newFlyBegin;
      const newStagger = newBlocks.length > 1 ? newWindow / (newBlocks.length - 1) : 0;
      for (let i = 0; i < newBlocks.length; i += 1) {
        await waitUntil(TIMELINE.newFlyBegin + i * newStagger, token);
        if (token !== cs.getToken()) return;
        spawnOrb(newBlocks[i], 'new', i, newBlocks.length, token);
        countUp('db-new-count', i + 1, 420);
      }
      await waitUntil(TIMELINE.newDone, token);
      if (token !== cs.getToken()) return;

      document.getElementById('db-orbit-blocks')?.classList.add('is-spinning');
      document.getElementById('db-radar')?.classList.add('is-frozen');
      setHud('new', 'done');
      setCaption('正在对新旧区块执行全域匹配演算…');
      await waitUntil(TIMELINE.newDone + 300, token);
      if (token !== cs.getToken()) return;

      setPhase(2);
      setHud('pair', 'active');
      runCrossScan(token);

      const matchSpan = TIMELINE.matchDone - TIMELINE.matchBegin - 400;
      const matchStagger = pairTotal > 1 ? matchSpan / (pairTotal - 1) : 0;
      for (let i = 0; i < pairTotal; i += 1) {
        await waitUntil(TIMELINE.matchBegin + i * matchStagger, token);
        if (token !== cs.getToken()) return;
        const pair = pairs[i] || {
          old_block_id: oldBlocks[i]?.block_id,
          new_block_id: newBlocks[i]?.block_id,
        };
        if (!pair.old_block_id || !pair.new_block_id) continue;
        await confirmMatch(pair, i, pairTotal, token);
      }
      await waitUntil(TIMELINE.matchFlash, token);
      if (token !== cs.getToken()) return;

      const stage = document.getElementById('showcase-doubao-stage');
      stage?.classList.add('is-flash');
      setCaption(`${pairTotal} 组配对演算完成`);
      await waitUntil(TIMELINE.matchDone, token);
      if (token !== cs.getToken()) return;
      stage?.classList.remove('is-flash');

      setHud('pair', 'done');
      setPhase(3);

      stage?.classList.add('is-finale');
      setCaption('配对展示完成 · 浏览左右连线');
      if (matchMeta) matchMeta.textContent = '配对展示完成';
      await waitUntil(TIMELINE.jumpAt, token);
      if (token !== cs.getToken()) return;

      await finishDoubao(token, { autoAudit: false });
    } finally {
      clearTimeout(fallback);
    }
  }

  function skipDoubaoDemo() {
    const cs = CS();
    if (!cs) return;
    cs.skipToOverviewComplete?.();
  }

  function toggleDoubaoSpeed() {
    const panel = document.getElementById('pair-panel');
    const btn = document.getElementById('btn-showcase-speed');
    const fast = panel?.classList.toggle('is-speed-fast');
    dbSpeed = fast ? 1.54 : 1;
    if (btn) btn.textContent = fast ? '快速时长' : '标准时长';
  }

  function initDoubaoControls() {
    document.getElementById('btn-showcase-skip')?.addEventListener('click', () => {
      const panel = document.getElementById('pair-panel');
      if (panel?.classList.contains('showcase-mode-doubao') && panel?.classList.contains('showcase-playing')) {
        skipDoubaoDemo();
      }
    });
    document.getElementById('btn-showcase-speed')?.addEventListener('click', () => {
      const panel = document.getElementById('pair-panel');
      if (panel?.classList.contains('showcase-mode-doubao') && panel?.classList.contains('showcase-playing')) {
        toggleDoubaoSpeed();
      }
    });
  }

  window.runShowcaseAnimationDoubao = runShowcaseAnimationDoubao;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initDoubaoControls);
  } else {
    initDoubaoControls();
  }
})();
