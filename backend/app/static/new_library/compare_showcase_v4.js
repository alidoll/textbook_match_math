/**
 * V4（默认）：经典大屏叙事 + 侧栏拉取 + 独立配对竞技场（大球快动效）
 * ?showcase=v4 | classic | doubao | hybrid
 */
(function () {
  let v4Speed = 0.55;

  function CS() {
    return window.CompareShowcase;
  }

  function t(ms) {
    const panel = document.getElementById('pair-panel');
    const fast = panel?.classList.contains('is-speed-fast');
    const mul = fast ? 0.65 : v4Speed;
    return Math.round(ms * mul);
  }

  function wait(ms, token) {
    const cs = CS();
    return new Promise((resolve) => {
      setTimeout(() => resolve(token === cs.getToken()), t(ms));
    });
  }

  function shortLabel(block, side, index) {
    const id = block?.block_id || '';
    const m = id.match(/([BN])(\d+)/i);
    if (m) return `${m[1].toUpperCase()}${m[2].padStart(2, '0')}`;
    return `${side === 'old' ? 'B' : 'N'}${String(index + 1).padStart(2, '0')}`;
  }

  function labelForId(blockId, blocks, side) {
    const idx = blocks.findIndex((b) => b.block_id === blockId);
    const block = blocks[idx];
    if (block) return shortLabel(block, side, Math.max(0, idx));
    const m = (blockId || '').match(/([BN]\d+)/i);
    return m ? m[1].toUpperCase() : (blockId || '?').slice(-4);
  }

  function buildPairs(state) {
    const matches = (state?.matches || []).filter((m) => m.old_block_id && m.new_block_id);
    if (matches.length) return matches;
    const old = state?.old_blocks || [];
    const neu = state?.new_blocks || [];
    return Array.from({ length: Math.min(old.length, neu.length) }, (_, i) => ({
      old_block_id: old[i]?.block_id,
      new_block_id: neu[i]?.block_id,
    })).filter((p) => p.old_block_id && p.new_block_id);
  }

  function resetArena() {
    document.getElementById('pa-old-slot')?.replaceChildren();
    document.getElementById('pa-new-slot')?.replaceChildren();
    document.getElementById('pa-matched-row')?.replaceChildren();
    document.getElementById('pa-link-svg')?.querySelectorAll('line').forEach((l) => l.remove());
    document.getElementById('pa-scan-beam')?.classList.remove('is-active');
    document.getElementById('pa-bridge')?.classList.remove('is-live');
    const st = document.getElementById('pa-status');
    if (st) {
      st.textContent = '—';
      st.classList.remove('is-hit');
    }
  }

  function showArena() {
    const arena = document.getElementById('showcase-pair-arena');
    if (arena) {
      arena.hidden = false;
      arena.setAttribute('aria-hidden', 'false');
    }
  }

  function hideArena() {
    const arena = document.getElementById('showcase-pair-arena');
    if (arena) {
      arena.hidden = true;
      arena.setAttribute('aria-hidden', 'true');
    }
    resetArena();
  }

  function makeOrb(label, side) {
    const orb = document.createElement('div');
    orb.className = `pa-orb pa-orb-${side}`;
    orb.textContent = label;
    const check = document.createElement('span');
    check.className = 'pa-orb-check';
    check.textContent = '✓';
    orb.appendChild(check);
    return orb;
  }

  function setStatus(text, hit = false) {
    const st = document.getElementById('pa-status');
    if (!st) return;
    st.textContent = text;
    st.classList.toggle('is-hit', hit);
  }

  function drawBeam() {
    const svg = document.getElementById('pa-link-svg');
    const oldSlot = document.getElementById('pa-old-slot');
    const newSlot = document.getElementById('pa-new-slot');
    if (!svg || !oldSlot || !newSlot) return;
    svg.querySelectorAll('line').forEach((l) => l.remove());
    const arena = document.getElementById('showcase-pair-arena');
    const ar = arena.getBoundingClientRect();
    const o = oldSlot.getBoundingClientRect();
    const n = newSlot.getBoundingClientRect();
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', o.right - ar.left);
    line.setAttribute('y1', o.top - ar.top + o.height / 2);
    line.setAttribute('x2', n.left - ar.left);
    line.setAttribute('y2', n.top - ar.top + n.height / 2);
    line.setAttribute('class', 'pa-beam');
    svg.appendChild(line);
  }

  function addMatchedChip(newL, oldL) {
    const row = document.getElementById('pa-matched-row');
    if (!row) return;
    const chip = document.createElement('span');
    chip.className = 'pa-matched-chip';
    chip.textContent = `${newL} ↔ ${oldL}`;
    row.appendChild(chip);
    if (row.children.length > 8) row.firstElementChild?.remove();
  }

  function spawnTrail(slotEl, side) {
    const arena = document.getElementById('showcase-pair-arena');
    if (!arena || !slotEl) return;
    const tr = document.createElement('div');
    tr.className = `pa-orb-trail side-${side}`;
    const r = slotEl.getBoundingClientRect();
    const a = arena.getBoundingClientRect();
    tr.style.left = `${r.left - a.left + r.width / 2 - 4}px`;
    tr.style.top = `${r.top - a.top + r.height / 2 - 4}px`;
    arena.appendChild(tr);
    setTimeout(() => tr.remove(), t(560));
  }

  async function playScanTarget(newBlock, newLabel, token) {
    const cs = CS();
    if (token !== cs.getToken()) return;
    showArena();
    resetArena();
    setStatus(`${newLabel} 特征已提取 · 准备检索旧库`);
    const newSlot = document.getElementById('pa-new-slot');
    const orb = makeOrb(newLabel, 'new');
    newSlot?.appendChild(orb);
    requestAnimationFrame(() => orb.classList.add('is-seek'));
    document.getElementById('pa-scan-beam')?.classList.add('is-active');
    await wait(2400, token);
    if (token !== cs.getToken()) return;
    document.getElementById('pa-scan-beam')?.classList.remove('is-active');
  }

  async function playPairDock(pair, state, token, { hero = false } = {}) {
    const cs = CS();
    if (token !== cs.getToken()) return;
    const oldBlocks = state?.old_blocks || [];
    const newBlocks = state?.new_blocks || [];
    const newL = labelForId(pair.new_block_id, newBlocks, 'new');
    const oldL = labelForId(pair.old_block_id, oldBlocks, 'old');

    resetArena();
    setStatus(`${newL} 检索中…`);
    const newSlot = document.getElementById('pa-new-slot');
    const oldSlot = document.getElementById('pa-old-slot');
    const newOrb = makeOrb(newL, 'new');
    newSlot?.appendChild(newOrb);
    requestAnimationFrame(() => newOrb.classList.add('is-pop', 'is-seek'));

    document.getElementById('pa-scan-beam')?.classList.add('is-active');
    await wait(hero ? 520 : 320, token);
    if (token !== cs.getToken()) return;

    setStatus(`命中候选 · 锁定 ${oldL}`);
    const oldOrb = makeOrb(oldL, 'old');
    oldSlot?.appendChild(oldOrb);
    requestAnimationFrame(() => oldOrb.classList.add('is-found'));
    spawnTrail(oldSlot, 'old');
    document.getElementById('pa-scan-beam')?.classList.remove('is-active');

    await wait(hero ? 380 : 240, token);
    if (token !== cs.getToken()) return;

    setStatus(`${newL} ↔ ${oldL}`, true);
    newOrb.classList.remove('is-seek');
    oldOrb.classList.remove('is-found');
    newOrb.classList.add('is-dock-new');
    oldOrb.classList.add('is-dock-old');
    document.getElementById('pa-bridge')?.classList.add('is-live');
    drawBeam();

    cs.highlightSideCard('new', pair.new_block_id);
    cs.highlightSideCard('old', pair.old_block_id);

    await wait(hero ? 480 : 360, token);
    if (token !== cs.getToken()) return;

    newOrb.classList.add('is-linked');
    oldOrb.classList.add('is-linked');
    addMatchedChip(newL, oldL);
    const matchMeta = document.getElementById('showcase-match-meta');
    const row = document.getElementById('pa-matched-row');
    if (matchMeta && row) matchMeta.textContent = `已配 ${row.children.length} 组`;

    await wait(hero ? 420 : 280, token);
  }

  function prepareV4() {
    document.getElementById('pair-panel')?.classList.add('showcase-mode-v4');
    hideArena();
    const sim = document.getElementById('showcase-sim-screen');
    if (sim) sim.hidden = false;
  }

  function teardownV4() {
    document.getElementById('pair-panel')?.classList.remove('showcase-mode-v4', 'is-speed-fast');
    hideArena();
  }

  async function finishV4(token, { autoAudit = false } = {}) {
    const cs = CS();
    if (!cs || token !== cs.getToken()) {
      cs?.setRunning(false);
      return;
    }
    hideArena();
    teardownV4();
    await cs.finish(token, { autoAudit, variant: 'v4' });
  }

  async function runShowcaseAnimationV4() {
    const cs = CS();
    if (!cs || cs.getViewMode() !== 'pair') return;

    const token = cs.startShowcase();
    cs.resetShowcaseSteps();
    cs.prepareShowcaseLayout();
    prepareV4();

    const panel = document.getElementById('pair-panel');
    panel?.classList.add('showcase-playing');
    panel?.classList.remove('showcase-complete', 'showcase-outro', 'showcase-old-lit', 'showcase-new-lit');
    cs.renderShowcaseBannerCompact();

    const state = cs.getState();
    const { oldN, newN } = cs.showcaseStats();
    const firstNew = cs.demoFirstNewBlock();
    const candidates = cs.demoMatchCandidates(firstNew);
    const pairs = buildPairs(state);
    const newLabel = shortLabel(firstNew, 'new', 0);

    if (cs.prefersReducedMotion()) {
      await finishV4(token, { autoAudit: false });
      return;
    }

    const pullStagger = 120;
    const oldPullHold = cs.calcPullHold(oldN, pullStagger);
    const newPullHold = cs.calcPullHold(newN, pullStagger);

    const fallback = setTimeout(() => {
      if (cs.isRunning() && token === cs.getToken()) finishV4(token, { autoAudit: false });
    }, t(18000));

    try {
      cs.setShowcaseDemoProgress(0, '');
      cs.setShowcaseStep(0);
      cs.renderSim(cs.simHtmlPullOld(oldN, oldPullHold));
      panel?.classList.add('showcase-old-lit');
      cs.revealShowcaseCards('old', pullStagger);
      cs.animateSimCounter('sim-counter-old', oldN, oldPullHold - 200);
      if (!(await wait(oldPullHold, token))) return;

      cs.setShowcaseDemoProgress(30, '');
      cs.setShowcaseStep(1);
      cs.renderSim(cs.simHtmlPullNew(newN, newPullHold));
      panel?.classList.remove('showcase-old-lit');
      panel?.classList.add('showcase-new-lit');
      cs.revealShowcaseCards('new', pullStagger);
      cs.animateSimCounter('sim-counter-new', newN, newPullHold - 200);
      if (!(await wait(newPullHold, token))) return;

      cs.setShowcaseDemoProgress(55, '');
      cs.setShowcaseStep(2);
      cs.renderSim(cs.simHtmlScanNew(firstNew));
      cs.highlightSideCard('new', firstNew.block_id);
      document.getElementById('showcase-block-meta').textContent = firstNew.block_id;

      const scanArena = playScanTarget(firstNew, newLabel, token);
      if (!(await wait(2800, token))) return;
      await scanArena;

      cs.setShowcaseStep(3);
      cs.setShowcaseDemoProgress(78, '');
      cs.renderSim(cs.simHtmlMatchOld(firstNew, candidates, { compact: true }));
      cs.highlightSideCard('old', candidates[0]?.old);
      showArena();
      resetArena();

      const heroPair = pairs[0] || {
        old_block_id: candidates[0]?.old || candidates[0]?.old_block_id,
        new_block_id: firstNew.block_id,
      };
      if (heroPair.old_block_id && heroPair.new_block_id) {
        await playPairDock(heroPair, state, token, { hero: true });
      }

      const montage = pairs.slice(1, Math.min(pairs.length, 5));
      for (let i = 0; i < montage.length; i += 1) {
        if (token !== cs.getToken()) return;
        cs.setShowcaseDemoProgress(78 + Math.round((i + 1) / Math.max(1, montage.length + 1) * 18), '');
        await playPairDock(montage[i], state, token, { hero: false });
      }

      if (pairs.length > 5) {
        setStatus(`${pairs.length} 组配对演算完成`, true);
        const matchMeta = document.getElementById('showcase-match-meta');
        if (matchMeta) matchMeta.textContent = `${pairs.length} 组`;
        cs.setShowcaseDemoProgress(96, `${pairs.length} 组配对完成`);
        await wait(500, token);
      }

      await finishV4(token, { autoAudit: false });
    } finally {
      clearTimeout(fallback);
    }
  }

  function initV4Controls() {
    document.getElementById('btn-showcase-skip')?.addEventListener('click', () => {
      const panel = document.getElementById('pair-panel');
      if (panel?.classList.contains('showcase-mode-v4') && panel?.classList.contains('showcase-playing')) {
        CS()?.skipToOverviewComplete?.();
      }
    });
  }

  window.ShowcasePairArena = { reset: resetArena };
  window.runShowcaseAnimationV4 = runShowcaseAnimationV4;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initV4Controls);
  } else {
    initV4Controls();
  }
})();
