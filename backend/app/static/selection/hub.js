(function () {
  var state = {
    lessonUid: null,
    lessonName: null,
    product: "practice",
    readiness: null,
    briefDraft: null,
    pool: null,
    latestHandoff: null,
    lastHandoffId: null,
    filterCatalog: [],
    searchTimer: null,
    workspaceState: null,
    workspaceId: null,
    demandRound1: null,
    demandNext: null,
    cacheByQueId: {},
  };

  function $(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(url, opts) {
    var res = await fetch(url, opts);
    var data = null;
    try {
      data = await res.json();
    } catch (e) {
      data = { ok: false, error: "invalid json" };
    }
    return { res: res, data: data };
  }

  function showLessons() {
    $("panel-lessons").hidden = false;
    $("panel-workspace").hidden = true;
  }

  function showWorkspace() {
    $("panel-lessons").hidden = true;
    $("panel-workspace").hidden = false;
  }

  function setMsg(text, kind) {
    var el = $("ws-msg");
    el.textContent = text || "";
    el.classList.remove("is-ok", "is-err", "is-busy");
    if (kind) el.classList.add(kind);
  }

  function fillSelect(el, values, placeholder, selected) {
    var opts = ['<option value="">' + esc(placeholder) + "</option>"];
    values.forEach(function (v) {
      var sv = String(v);
      opts.push(
        '<option value="' +
          esc(sv) +
          '"' +
          (selected != null && String(selected) === sv ? " selected" : "") +
          ">" +
          esc(sv) +
          "</option>"
      );
    });
    el.innerHTML = opts.join("");
  }

  function gradesForEdition(edition) {
    var set = {};
    state.filterCatalog.forEach(function (row) {
      if (row.edition === edition) set[row.grade] = true;
    });
    return Object.keys(set)
      .map(Number)
      .sort(function (a, b) {
        return a - b;
      });
  }

  function semestersFor(edition, grade) {
    var set = {};
    state.filterCatalog.forEach(function (row) {
      if (row.edition === edition && Number(row.grade) === Number(grade)) {
        set[row.semester] = true;
      }
    });
    return Object.keys(set).sort(function (a, b) {
      var ra = a.indexOf("上") >= 0 ? 0 : a.indexOf("下") >= 0 ? 1 : 2;
      var rb = b.indexOf("上") >= 0 ? 0 : b.indexOf("下") >= 0 ? 1 : 2;
      return ra - rb || (a < b ? -1 : a > b ? 1 : 0);
    });
  }

  function bookTypeLabel(bt) {
    if (bt === "diff_new") return '<span class="sel-badge sel-badge-diff">比对册</span>';
    if (bt === "new") return '<span class="sel-badge sel-badge-new">新教材</span>';
    return esc(bt || "");
  }

  function formatKp(kps) {
    if (!kps || !kps.length) return "（暂无 KP）";
    try {
      return JSON.stringify(kps, null, 2);
    } catch (e) {
      return String(kps);
    }
  }

  function formatJson(val) {
    if (val == null || val === "") return "—";
    if (typeof val === "string") return val;
    try {
      return JSON.stringify(val, null, 2);
    } catch (e) {
      return String(val);
    }
  }

  function detailText(g) {
    if (g == null) return "";
    if (typeof g.detail === "string") return g.detail;
    if (g.detail == null) return "";
    try {
      return JSON.stringify(g.detail);
    } catch (e) {
      return String(g.detail);
    }
  }

  function rememberCacheItems(items) {
    (items || []).forEach(function (it) {
      if (it && it.que_id) state.cacheByQueId[it.que_id] = it;
    });
  }

  function stemFromPayload(payload) {
    if (!payload || typeof payload !== "object") return "";
    return String(payload.stem || payload.content || "").trim();
  }

  function stemPreview(text, maxLen) {
    var s = String(text || "").trim();
    if (!s) return "—";
    var n = maxLen || 160;
    return s.length <= n ? s : s.slice(0, n - 1) + "…";
  }

  function isPostHandoffStatus(status) {
    return (
      status === "handed_off" ||
      status === "reviewing" ||
      status === "candidates_received" ||
      status === "finalized"
    );
  }

  function updateHandoffEnabled() {
    var ready = !!(state.readiness && state.readiness.ok);
    var dispOk = !!(state.workspaceState && state.workspaceState.dispositions_complete);
    var wsStatus = (state.workspaceState && state.workspaceState.status) || "";
    var postHandoff = isPostHandoffStatus(wsStatus);
    var handoffBtn = $("btn-handoff");
    if (postHandoff) {
      handoffBtn.disabled = true;
      handoffBtn.textContent = "已交虾 — 请用 demand-next";
    } else {
      handoffBtn.disabled = !(ready && dispOk);
      handoffBtn.textContent = "交虾（下载需求包）";
    }
    var loadBtn = $("btn-load-demand");
    if (loadBtn) loadBtn.disabled = !dispOk || !state.workspaceId;
    var completeBtn = $("btn-disp-complete");
    if (completeBtn) {
      completeBtn.disabled = !dispOk || !state.workspaceId;
    }
  }

  function renderGates(r) {
    var gates = (r && r.gates) || {};
    var order = [
      ["diff", "新旧比对"],
      ["pool", "旧题池"],
      ["courseware_kp", "新课件 KP"],
      ["demand", "题量/需求"],
    ];
    $("gates").innerHTML = order
      .map(function (pair) {
        var key = pair[0];
        var label = pair[1];
        var g = gates[key] || {};
        var ok = !!g.ok;
        var fill =
          !ok && g.fill_url
            ? ' <a href="' + esc(g.fill_url) + '" target="_blank" rel="noopener">去补 →</a>'
            : "";
        return (
          '<div class="sel-gate ' +
          (ok ? "is-ok" : "is-bad") +
          '">' +
          '<span class="sel-gate-light" aria-hidden="true"></span>' +
          '<div class="sel-gate-body"><strong>' +
          label +
          "</strong>" +
          (ok ? "通过" : "未齐") +
          " — " +
          esc(detailText(g)) +
          fill +
          "</div></div>"
        );
      })
      .join("");

    var ready = !!(r && r.ok);
    $("brief-box").hidden = false;
    updateHandoffEnabled();
    var dispOk = !!(state.workspaceState && state.workspaceState.dispositions_complete);
    var wsStatus = (state.workspaceState && state.workspaceState.status) || "";
    if (isPostHandoffStatus(wsStatus)) {
      setMsg(
        "已进入交虾后流程（" +
          wsStatus +
          "）。第 N 轮请用「加载 demand-next」，勿再点交虾。",
        "is-ok"
      );
    } else if (ready && dispOk) {
      setMsg(
        "齐套且旧题已处置，可编辑 KP / 备注后交虾。目标题量 " + (r.target_count || ""),
        "is-ok"
      );
    } else if (ready) {
      setMsg("齐套，请完成旧题处置（每题 reuse/replace/drop）后再交虾。", "is-err");
    } else {
      setMsg("未齐套，请按「去补」处理后再交虾。", "is-err");
    }
  }

  function renderBrief(draft, readiness) {
    var d = draft || {};
    if (!$("brief-kp").dataset.touched) {
      $("brief-kp").value = formatKp(d.kp);
    }
    if (!$("brief-notes").dataset.touched) {
      $("brief-notes").value = d.notes || "";
    }
    var plan = d.item_type_plan;
    if (!plan && readiness && readiness.target_count != null) {
      plan = [{ type: state.product, count: readiness.target_count }];
    }
    if (!$("brief-plan").dataset.touched) {
      $("brief-plan").value = plan ? JSON.stringify(plan, null, 2) : "";
    }
  }

  function renderGap(gap) {
    var g = gap || {};
    $("gap-summary").innerHTML =
      '<span class="sel-gap-chip">目标 <strong>' +
      esc(g.target != null ? g.target : "—") +
      '</strong></span>' +
      '<span class="sel-gap-chip is-reuse">reuse <strong>' +
      esc(g.reuse != null ? g.reuse : 0) +
      '</strong></span>' +
      '<span class="sel-gap-chip">replace <strong>' +
      esc(g.replace != null ? g.replace : 0) +
      '</strong></span>' +
      '<span class="sel-gap-chip">drop <strong>' +
      esc(g.drop != null ? g.drop : 0) +
      '</strong></span>' +
      '<span class="sel-gap-chip is-gap">缺口 <strong>' +
      esc(g.gap != null ? g.gap : 0) +
      "</strong></span>";
  }

  function actionOptions(selected) {
    var actions = ["", "reuse", "replace", "drop"];
    return actions
      .map(function (a) {
        var label = a || "（未处置）";
        return (
          '<option value="' +
          esc(a) +
          '"' +
          (selected === a || (!selected && !a) ? " selected" : "") +
          ">" +
          esc(label) +
          "</option>"
        );
      })
      .join("");
  }

  function renderOldDispositions(ws) {
    var box = $("old-disp-box");
    if (!ws || !ws.workspace_id) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    renderGap(ws.gap);
    var rows = ws.dispositions || [];
    var tb = $("old-disp-tbody");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="sel-empty">暂无旧题池 id</td></tr>';
      updateHandoffEnabled();
      return;
    }
    tb.innerHTML = rows
      .map(function (row, idx) {
        var qid = row.que_id || "";
        var cached = state.cacheByQueId[qid];
        var status = row.cache_status || (cached && cached.status) || "未爬取";
        var summary =
          row.summary ||
          (cached && stemPreview(stemFromPayload(cached.payload), 80)) ||
          "—";
        var action = row.action || "";
        var note = row.note || "";
        var statusClass =
          status === "ready"
            ? "is-ready"
            : status === "fetch_failed"
              ? "is-fail"
              : status === "pending"
                ? "is-pending"
                : "";
        return (
          "<tr data-que-id=\"" +
          esc(qid) +
          '">' +
          "<td>" +
          (idx + 1) +
          "</td><td><code>" +
          esc(qid) +
          "</code></td><td><span class=\"sel-cache-status " +
          statusClass +
          '">' +
          esc(status) +
          "</span></td><td class=\"sel-stem-cell\">" +
          esc(summary) +
          '</td><td><select class="sel-disp-action" data-que-id="' +
          esc(qid) +
          '">' +
          actionOptions(action) +
          '</select></td><td><input class="sel-disp-note sel-input" data-que-id="' +
          esc(qid) +
          '" value="' +
          esc(note) +
          '" placeholder="备注"></td><td><button type="button" class="sel-btn sel-btn-sm sel-paste-qid" data-que-id="' +
          esc(qid) +
          '">粘贴</button></td></tr>'
        );
      })
      .join("");
    updateHandoffEnabled();
  }

  function candidateStem(item) {
    if (!item) return "—";
    if (item.source === "diy" && item.diy) {
      return stemPreview(stemFromPayload(item.diy) || formatJson(item.diy), 200);
    }
    var qid = item.que_id;
    if (qid && state.cacheByQueId[qid]) {
      var c = state.cacheByQueId[qid];
      var stem = stemFromPayload(c.payload);
      if (stem) return stemPreview(stem, 200);
      return "[" + (c.status || "no payload") + "] " + qid;
    }
    return qid ? "queId " + qid + "（待爬取/粘贴）" : "—";
  }

  function renderCandidates(ws) {
    var box = $("candidates-box");
    var fin = $("finalize-box");
    if (!ws || !ws.workspace_id) {
      box.hidden = true;
      fin.hidden = true;
      return;
    }
    box.hidden = false;
    fin.hidden = false;
    var items = ws.candidates || [];
    var list = $("candidates-list");
    if (!items.length) {
      list.innerHTML = '<p class="sel-hint">尚无候选。登记虾回传后在此审题。</p>';
      renderFinalizePreview(ws);
      return;
    }
    list.innerHTML = items
      .map(function (it) {
        var verdict = it.verdict || "pending";
        if (verdict === "superseded") {
          return (
            '<div class="sel-cand-card is-superseded" data-item-id="' +
            esc(it.id) +
            '">' +
            '<div class="sel-cand-meta">' +
            "<strong>#" +
            esc(it.slot) +
            "</strong> · r" +
            esc(it.round) +
            " · " +
            esc(it.source) +
            (it.que_id ? " · <code>" + esc(it.que_id) + "</code>" : "") +
            ' · 结论 <span class="sel-verdict">superseded</span></div>' +
            '<div class="sel-cand-stem">' +
            esc(candidateStem(it)) +
            "</div>" +
            '<p class="sel-hint">已被后续轮次替代</p></div>'
          );
        }
        var bankNeedsCache =
          (it.source === "bank" || it.source === "adapt") && !!it.que_id;
        var cacheReady =
          !bankNeedsCache ||
          (state.cacheByQueId[it.que_id] &&
            state.cacheByQueId[it.que_id].status === "ready");
        var keepDisabled = bankNeedsCache && !cacheReady;
        return (
          '<div class="sel-cand-card" data-item-id="' +
          esc(it.id) +
          '">' +
          '<div class="sel-cand-meta">' +
          "<strong>#" +
          esc(it.slot) +
          "</strong> · r" +
          esc(it.round) +
          " · " +
          esc(it.source) +
          (it.que_id ? " · <code>" + esc(it.que_id) + "</code>" : "") +
          ' · 结论 <span class="sel-verdict">' +
          esc(verdict) +
          "</span></div>" +
          '<div class="sel-cand-stem">' +
          esc(candidateStem(it)) +
          "</div>" +
          '<div class="sel-cand-actions">' +
          '<button type="button" class="sel-btn sel-btn-sm sel-verdict-btn" data-id="' +
          esc(it.id) +
          '" data-verdict="keep"' +
          (keepDisabled ? " disabled title=\"需先爬取/粘贴题面缓存\"" : "") +
          ">keep</button>" +
          '<button type="button" class="sel-btn sel-btn-sm sel-verdict-btn" data-id="' +
          esc(it.id) +
          '" data-verdict="replace">replace</button>' +
          '<button type="button" class="sel-btn sel-btn-sm sel-verdict-btn" data-id="' +
          esc(it.id) +
          '" data-verdict="revise">revise</button>' +
          '<button type="button" class="sel-btn sel-btn-sm sel-verdict-btn" data-id="' +
          esc(it.id) +
          '" data-verdict="reject">reject</button>' +
          '<input class="sel-input sel-verdict-note" data-id="' +
          esc(it.id) +
          '" placeholder="revise/replace 说明" value="' +
          esc(it.verdict_note || "") +
          '">' +
          "</div></div>"
        );
      })
      .join("");
    renderFinalizePreview(ws);
  }

  function renderFinalizePreview(ws) {
    var el = $("finalize-preview");
    if (!ws) {
      el.innerHTML = "";
      return;
    }
    var gap = ws.gap || {};
    var reuse = Number(gap.reuse || 0);
    var keep = (ws.candidates || []).filter(function (c) {
      return (c.verdict || "") === "keep";
    }).length;
    var target = gap.target != null ? gap.target : ws.target_count;
    var ok = reuse + keep >= Number(target || 0);
    var lines = [];
    (ws.dispositions || []).forEach(function (d) {
      if (d.action !== "reuse") return;
      var cached = state.cacheByQueId[d.que_id];
      lines.push(
        '<div class="sel-fin-item"><span class="sel-badge">reuse</span> <code>' +
          esc(d.que_id) +
          "</code> — " +
          esc(
            d.summary ||
              (cached && stemPreview(stemFromPayload(cached.payload), 100)) ||
              "—"
          ) +
          "</div>"
      );
    });
    (ws.candidates || []).forEach(function (c) {
      if ((c.verdict || "") !== "keep") return;
      lines.push(
        '<div class="sel-fin-item"><span class="sel-badge">keep</span> ' +
          esc(c.source) +
          (c.que_id ? " <code>" + esc(c.que_id) + "</code>" : "") +
          " — " +
          esc(candidateStem(c)) +
          "</div>"
      );
    });
    el.innerHTML =
      '<p class="sel-hint">reuse ' +
      reuse +
      " + keep " +
      keep +
      " / 目标 " +
      esc(target) +
      (ok ? " · 可定稿" : " · 尚未达标") +
      "</p>" +
      (lines.length ? lines.join("") : '<p class="sel-hint">尚无 reuse/keep 题面。</p>');
    $("btn-finalize").disabled = !ok || ws.status === "finalized";
    if (ws.status === "finalized") {
      $("btn-finalize").textContent = "已定稿";
    } else {
      $("btn-finalize").textContent = "确认定稿";
    }
  }

  function bindPaperSave() {
    var btn = $("btn-paper");
    if (!btn) return;
    btn.onclick = async function () {
      var url = ($("paper-url") && $("paper-url").value.trim()) || "";
      var hid = state.lastHandoffId;
      if (!hid) {
        setMsg("无 handoff，无法登记 paper", "is-err");
        return;
      }
      var pr = await api("/api/selection/handoffs/" + encodeURIComponent(hid) + "/paper", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paper_url: url }),
      });
      if (pr.data.ok) {
        setMsg("paper_received", "is-ok");
        await refreshWorkspace();
      } else {
        setMsg("登记失败：" + (pr.data.error || ""), "is-err");
      }
    };
  }

  function renderReview(latest) {
    var box = $("review-box");
    var readonly = $("review-readonly");
    if (!latest) {
      box.hidden = true;
      readonly.hidden = true;
      $("paper-link").innerHTML = "";
      return;
    }
    box.hidden = false;
    state.lastHandoffId = latest.handoff_id || state.lastHandoffId;

    if (latest.status === "paper_received" && latest.paper_url) {
      $("paper-link").innerHTML =
        'Paper：<a href="' +
        esc(latest.paper_url) +
        '" target="_blank" rel="noopener">' +
        esc(latest.paper_url) +
        "</a>";
      readonly.hidden = false;
      $("review-diff").textContent = formatJson(latest.diff_summary);
      $("review-kp").textContent = formatKp(latest.kp);
      $("review-qids").textContent =
        latest.question_ids ||
        (latest.question_ids_list && latest.question_ids_list.length
          ? latest.question_ids_list.join(",")
          : "—");
      setMsg("paper_received · 可对照比对/KP 审题并下载换题说明", "is-ok");
    } else {
      readonly.hidden = true;
      $("paper-link").innerHTML =
        '登记 paper：<input id="paper-url" placeholder="https://..." type="url"> ' +
        '<button type="button" id="btn-paper" class="sel-btn">保存 paper</button>';
      bindPaperSave();
    }
  }

  function parseJsonField(el, label) {
    var raw = (el.value || "").trim();
    if (!raw || raw === "（暂无 KP）" || raw === "—") return null;
    try {
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) {
        throw new Error(label + " 须为 JSON 数组");
      }
      return parsed;
    } catch (e) {
      throw new Error(label + " JSON 无效：" + (e.message || e));
    }
  }

  async function hydrateCandidateCaches(ws) {
    if (!ws || !ws.workspace_id) return;
    var ids = [];
    (ws.candidates || []).forEach(function (c) {
      if (c.que_id && !state.cacheByQueId[c.que_id]) ids.push(c.que_id);
    });
    (ws.dispositions || []).forEach(function (d) {
      if (d.que_id && d.cache_status && !state.cacheByQueId[d.que_id]) {
        /* status known from list; still try fetch to get payload for stem */
      }
      if (d.que_id && !state.cacheByQueId[d.que_id] && d.cache_status === "ready") {
        ids.push(d.que_id);
      }
    });
    ids = ids.filter(function (id, i, arr) {
      return arr.indexOf(id) === i;
    });
    if (!ids.length) return;
    var out = await api(
      "/api/selection/workspaces/" + encodeURIComponent(ws.workspace_id) + "/fetch-old",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: ids }),
      }
    );
    if (out.data.ok) rememberCacheItems(out.data.items);
  }

  async function loadFilterOptions() {
    var out = await api("/api/selection/new-lessons/filters");
    if (!out.data.ok) {
      $("list-meta").textContent = "筛选项加载失败";
      return;
    }
    state.filterCatalog = out.data.catalog || [];
    fillSelect($("f-edition"), out.data.editions || [], "请选择版本", "");
    $("f-grade").disabled = true;
    $("f-semester").disabled = true;
    fillSelect($("f-grade"), [], "请先选版本", "");
    fillSelect($("f-semester"), [], "请先选年级", "");
  }

  function onEditionChange() {
    var ed = $("f-edition").value;
    $("f-q").value = "";
    if (!ed) {
      $("f-grade").disabled = true;
      $("f-semester").disabled = true;
      fillSelect($("f-grade"), [], "请先选版本", "");
      fillSelect($("f-semester"), [], "请先选年级", "");
      $("lesson-tbody").innerHTML =
        '<tr><td colspan="7" class="sel-empty">请先选择版本（下拉框）</td></tr>';
      $("list-meta").textContent = "";
      return;
    }
    var grades = gradesForEdition(ed);
    $("f-grade").disabled = false;
    fillSelect($("f-grade"), grades, "请选择年级", grades.length === 1 ? grades[0] : "");
    $("f-semester").disabled = true;
    fillSelect($("f-semester"), [], "请先选年级", "");
    if (grades.length === 1) {
      onGradeChange();
    } else {
      $("lesson-tbody").innerHTML =
        '<tr><td colspan="7" class="sel-empty">请继续选择年级</td></tr>';
      $("list-meta").textContent = "";
    }
  }

  function onGradeChange() {
    var ed = $("f-edition").value;
    var g = $("f-grade").value;
    $("f-q").value = "";
    if (!ed || !g) {
      $("f-semester").disabled = true;
      fillSelect($("f-semester"), [], "请先选年级", "");
      return;
    }
    var sems = semestersFor(ed, g);
    $("f-semester").disabled = false;
    fillSelect($("f-semester"), sems, "请选择学期", sems.length === 1 ? sems[0] : "");
    if (sems.length === 1) {
      loadLessons();
    } else {
      $("lesson-tbody").innerHTML =
        '<tr><td colspan="7" class="sel-empty">请继续选择学期</td></tr>';
      $("list-meta").textContent = "";
    }
  }

  async function loadLessons() {
    var ed = $("f-edition").value;
    var g = $("f-grade").value;
    var sem = $("f-semester").value;
    var q = ($("f-q").value || "").trim();
    if (!ed || !g || !sem) {
      return;
    }
    var params = new URLSearchParams();
    params.set("edition", ed);
    params.set("grade", g);
    params.set("semester", sem);
    if (q) params.set("q", q);
    $("list-meta").textContent = "加载中…";
    showLessons();
    var out = await api("/api/selection/new-lessons?" + params.toString());
    var tb = $("lesson-tbody");
    if (!out.data.ok) {
      $("list-meta").textContent = "";
      tb.innerHTML = '<tr><td colspan="7" class="sel-empty">加载失败</td></tr>';
      return;
    }
    $("list-meta").textContent = "共 " + out.data.total + " 课" + (q ? "（已按「" + q + "」筛选）" : "");
    if (!out.data.items.length) {
      tb.innerHTML =
        '<tr><td colspan="7" class="sel-empty">无匹配新课。可清空搜索，或换学期/版本。</td></tr>';
      return;
    }
    tb.innerHTML = out.data.items
      .map(function (it) {
        return (
          "<tr>" +
          "<td>" +
          esc(it.edition) +
          "</td><td>" +
          bookTypeLabel(it.book_type) +
          "</td><td>" +
          esc(it.grade) +
          " · " +
          esc(it.semester) +
          "</td><td>" +
          esc(it.unit_title) +
          "</td><td>" +
          esc(it.lesson_no) +
          "</td><td>" +
          esc(it.lesson_name) +
          '</td><td><button type="button" class="sel-btn sel-btn-primary sel-open" data-uid="' +
          esc(it.lesson_uid) +
          '" data-name="' +
          esc(it.lesson_name) +
          '">打开选题</button></td></tr>'
        );
      })
      .join("");
  }

  function scheduleSearch() {
    if (state.searchTimer) clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(function () {
      if ($("f-edition").value && $("f-grade").value && $("f-semester").value) {
        loadLessons();
      }
    }, 250);
  }

  async function refreshWorkspace() {
    if (!state.lessonUid) return;
    setMsg("刷新齐套中…", "is-busy");
    var out = await api(
      "/api/selection/new-lessons/" +
        encodeURIComponent(state.lessonUid) +
        "/workspace?product=" +
        encodeURIComponent(state.product)
    );
    if (!out.data.ok) {
      setMsg("工作台加载失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    state.readiness = out.data.readiness;
    state.briefDraft = out.data.brief_draft;
    state.pool = out.data.pool;
    state.latestHandoff = out.data.latest_handoff || null;
    state.workspaceState = out.data.workspace_state || null;
    state.workspaceId = (state.workspaceState && state.workspaceState.workspace_id) || null;
    if (state.latestHandoff && state.latestHandoff.handoff_id) {
      state.lastHandoffId = state.latestHandoff.handoff_id;
    }
    var target = (state.readiness && state.readiness.target_count) || "";
    var wsStatus = (state.workspaceState && state.workspaceState.status) || "";
    $("ws-sub").textContent =
      "产物 " +
      state.product +
      (target !== "" ? " · 目标题量 " + target : "") +
      (state.pool && state.pool.resource_id ? " · 旧题池已挂" : "") +
      (wsStatus ? " · ws:" + wsStatus : "") +
      (state.latestHandoff ? " · " + (state.latestHandoff.status || "handoff") : "");
    renderGates(state.readiness);
    renderBrief(state.briefDraft, state.readiness);
    renderOldDispositions(state.workspaceState);
    await hydrateCandidateCaches(state.workspaceState);
    renderOldDispositions(state.workspaceState);
    renderCandidates(state.workspaceState);
    renderReview(state.latestHandoff);
  }

  async function doHandoff() {
    if (
      state.workspaceState &&
      isPostHandoffStatus(state.workspaceState.status || "")
    ) {
      setMsg("已交虾过，第 N 轮请用「加载 demand-next」", "is-err");
      return;
    }
    if (!(state.workspaceState && state.workspaceState.dispositions_complete)) {
      setMsg("请先完成旧题处置（每题有 action）", "is-err");
      return;
    }
    setMsg("交虾中…", "is-busy");
    var notes = $("brief-notes").value.trim();
    var body = { product: state.product, notes: notes };
    try {
      var kp = parseJsonField($("brief-kp"), "KP");
      if (kp !== null) body.kp = kp;
      var plan = parseJsonField($("brief-plan"), "题型计划");
      if (plan !== null) body.item_type_plan = plan;
    } catch (e) {
      setMsg("失败：" + (e.message || e), "is-err");
      return;
    }
    var out = await api(
      "/api/selection/new-lessons/" + encodeURIComponent(state.lessonUid) + "/handoff",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );
    if (!out.res.ok || !out.data.ok) {
      setMsg("失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    state.lastHandoffId = out.data.handoff_id;
    setMsg(
      "已交虾 handoff=" +
        out.data.handoff_id +
        "；gap=" +
        (out.data.gap_count != null ? out.data.gap_count : "?") +
        "；question_ids=" +
        (out.data.question_ids || ""),
      "is-ok"
    );
    window.open(
      "/api/selection/handoffs/" + encodeURIComponent(out.data.handoff_id) + "/payload.json",
      "_blank"
    );
    await refreshWorkspace();
  }

  async function postDisposition(queId, action, note) {
    if (!state.workspaceId) return;
    if (!action) return;
    var out = await api(
      "/api/selection/workspaces/" + encodeURIComponent(state.workspaceId) + "/dispositions",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ que_id: queId, action: action, note: note || "" }),
      }
    );
    if (!out.data.ok) {
      setMsg("处置失败：" + (out.data.error || out.res.status), "is-err");
      return false;
    }
    return true;
  }

  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    var ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
  }

  function setDemandPreview(preId, text, show) {
    var el = $(preId);
    if (!el) return;
    el.hidden = !show;
    el.textContent = text || "";
  }

  $("f-edition").onchange = onEditionChange;
  $("f-grade").onchange = onGradeChange;
  $("f-semester").onchange = function () {
    $("f-q").value = "";
    loadLessons();
  };
  $("f-q").addEventListener("input", scheduleSearch);

  $("btn-back").onclick = function () {
    showLessons();
  };

  $("btn-refresh-ws").onclick = refreshWorkspace;
  $("btn-handoff").onclick = doHandoff;

  $("brief-notes").addEventListener("input", function () {
    $("brief-notes").dataset.touched = "1";
  });
  $("brief-kp").addEventListener("input", function () {
    $("brief-kp").dataset.touched = "1";
  });
  $("brief-plan").addEventListener("input", function () {
    $("brief-plan").dataset.touched = "1";
  });

  $("btn-fetch-old").onclick = async function () {
    if (!state.workspaceId) return;
    setMsg("爬取旧题中…", "is-busy");
    var out = await api(
      "/api/selection/workspaces/" + encodeURIComponent(state.workspaceId) + "/fetch-old",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      }
    );
    if (!out.data.ok) {
      setMsg("爬取失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    rememberCacheItems(out.data.items);
    setMsg("已请求爬取 " + (out.data.items || []).length + " 题（manual 时多为 pending，请粘贴缓存）", "is-ok");
    await refreshWorkspace();
  };

  $("btn-disp-complete").onclick = async function () {
    if (!state.workspaceId) return;
    var out = await api(
      "/api/selection/workspaces/" +
        encodeURIComponent(state.workspaceId) +
        "/dispositions/complete",
      { method: "POST" }
    );
    if (!out.data.ok) {
      setMsg("标记失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    setMsg("旧题处置完成 · status=" + (out.data.status || "old_reviewed"), "is-ok");
    await refreshWorkspace();
  };

  $("old-disp-tbody").onchange = async function (ev) {
    var sel = ev.target.closest(".sel-disp-action");
    if (!sel) return;
    var qid = sel.getAttribute("data-que-id");
    var action = sel.value;
    var row = sel.closest("tr");
    var noteEl = row ? row.querySelector(".sel-disp-note") : null;
    var note = noteEl ? noteEl.value : "";
    if (!action) return;
    setMsg("保存处置 " + qid + "…", "is-busy");
    var ok = await postDisposition(qid, action, note);
    if (ok) {
      setMsg("已保存 " + qid + " → " + action, "is-ok");
      await refreshWorkspace();
    } else {
      await refreshWorkspace();
    }
  };

  $("old-disp-tbody").onclick = function (ev) {
    var btn = ev.target.closest(".sel-paste-qid");
    if (!btn) return;
    $("manual-cache-qid").value = btn.getAttribute("data-que-id") || "";
    $("manual-cache-payload").focus();
  };

  $("btn-manual-cache").onclick = async function () {
    var qid = ($("manual-cache-qid").value || "").trim();
    var raw = ($("manual-cache-payload").value || "").trim();
    if (!qid) {
      setMsg("请填写 que_id", "is-err");
      return;
    }
    var payload;
    try {
      payload = JSON.parse(raw || "{}");
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new Error("payload 须为对象");
      }
    } catch (e) {
      setMsg("payload JSON 无效：" + (e.message || e), "is-err");
      return;
    }
    var out = await api("/api/selection/question-cache/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ que_id: qid, payload: payload }),
    });
    if (!out.data.ok) {
      setMsg("写入缓存失败：" + (out.data.error || ""), "is-err");
      return;
    }
    if (out.data.item) rememberCacheItems([out.data.item]);
    setMsg("缓存已就绪：" + qid, "is-ok");
    await refreshWorkspace();
  };

  $("btn-load-demand").onclick = async function () {
    if (!state.workspaceId) return;
    setMsg("加载第1轮 demand…", "is-busy");
    var out = await api(
      "/api/selection/workspaces/" +
        encodeURIComponent(state.workspaceId) +
        "/demand.md?round=1"
    );
    if (!out.data.ok) {
      setMsg("加载失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    state.demandRound1 = out.data;
    setDemandPreview(
      "demand-preview",
      (out.data.markdown || "") +
        "\n\n--- JSON ---\n" +
        formatJson(out.data.json),
      true
    );
    $("btn-copy-demand-md").disabled = false;
    $("btn-copy-demand-json").disabled = false;
    $("btn-dl-demand-json").disabled = false;
    setMsg(
      "第1轮缺口=" +
        ((out.data.json && out.data.json.gap_count) != null
          ? out.data.json.gap_count
          : "?"),
      "is-ok"
    );
  };

  $("btn-copy-demand-md").onclick = async function () {
    if (!state.demandRound1) return;
    await copyText(state.demandRound1.markdown || "");
    setMsg("已复制 Markdown", "is-ok");
  };
  $("btn-copy-demand-json").onclick = async function () {
    if (!state.demandRound1) return;
    await copyText(JSON.stringify(state.demandRound1.json || {}, null, 2));
    setMsg("已复制 JSON", "is-ok");
  };
  $("btn-dl-demand-json").onclick = function () {
    if (!state.demandRound1) return;
    var blob = new Blob([JSON.stringify(state.demandRound1.json || {}, null, 2)], {
      type: "application/json",
    });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "selection-demand-round1.json";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  $("btn-register-cand").onclick = async function () {
    if (!state.workspaceId) return;
    var round = parseInt($("cand-round").value, 10) || 1;
    var items;
    try {
      items = JSON.parse(($("cand-items").value || "").trim() || "[]");
      if (!Array.isArray(items)) throw new Error("须为数组");
    } catch (e) {
      setMsg("items JSON 无效：" + (e.message || e), "is-err");
      return;
    }
    setMsg("登记候选中…", "is-busy");
    var body = {
      round: round,
      items: items,
      handoff_id: state.lastHandoffId || null,
    };
    var out = await api(
      "/api/selection/workspaces/" + encodeURIComponent(state.workspaceId) + "/candidates",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }
    );
    if (!out.data.ok) {
      setMsg("登记失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    setMsg("已登记 " + (out.data.items || []).length + " 题", "is-ok");
    $("cand-items").value = "";
    await refreshWorkspace();
  };

  $("candidates-list").onclick = async function (ev) {
    var btn = ev.target.closest(".sel-verdict-btn");
    if (!btn) return;
    var id = btn.getAttribute("data-id");
    var verdict = btn.getAttribute("data-verdict");
    var card = btn.closest(".sel-cand-card");
    var noteEl = card ? card.querySelector(".sel-verdict-note") : null;
    var note = noteEl ? noteEl.value : "";
    if (verdict === "revise" && !(note || "").trim()) {
      setMsg("revise 须填写修改说明", "is-err");
      return;
    }
    setMsg("保存结论…", "is-busy");
    var out = await api("/api/selection/candidate-items/" + encodeURIComponent(id) + "/verdict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verdict: verdict, note: note }),
    });
    if (!out.data.ok) {
      setMsg("结论失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    setMsg("已标 " + verdict, "is-ok");
    await refreshWorkspace();
  };

  $("btn-load-next-demand").onclick = async function () {
    if (!state.workspaceId) return;
    setMsg("加载下一轮 demand…", "is-busy");
    var out = await api(
      "/api/selection/workspaces/" + encodeURIComponent(state.workspaceId) + "/demand-next"
    );
    if (!out.data.ok) {
      setMsg("加载失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    state.demandNext = out.data;
    setDemandPreview(
      "next-demand-preview",
      (out.data.markdown || "") +
        "\n\n--- JSON ---\n" +
        formatJson(out.data.json),
      true
    );
    $("btn-copy-next-md").disabled = false;
    $("btn-copy-next-json").disabled = false;
    setMsg("下一轮 demand 已加载", "is-ok");
  };
  $("btn-copy-next-md").onclick = async function () {
    if (!state.demandNext) return;
    await copyText(state.demandNext.markdown || "");
    setMsg("已复制下一轮 Markdown", "is-ok");
  };
  $("btn-copy-next-json").onclick = async function () {
    if (!state.demandNext) return;
    await copyText(JSON.stringify(state.demandNext.json || {}, null, 2));
    setMsg("已复制下一轮 JSON", "is-ok");
  };

  $("btn-finalize").onclick = async function () {
    if (!state.workspaceId) return;
    if (!window.confirm("确认定稿？此操作将工作台标为 finalized。")) return;
    setMsg("定稿中…", "is-busy");
    var out = await api(
      "/api/selection/workspaces/" + encodeURIComponent(state.workspaceId) + "/finalize",
      { method: "POST" }
    );
    if (!out.data.ok) {
      setMsg("定稿失败：" + (out.data.error || out.res.status), "is-err");
      return;
    }
    var pre = $("finalize-result");
    pre.hidden = false;
    pre.textContent = formatJson(out.data);
    setMsg(
      "已定稿 · reuse=" +
        out.data.reuse +
        " keep=" +
        out.data.keep +
        " / " +
        out.data.target,
      "is-ok"
    );
    await refreshWorkspace();
  };

  $("btn-import").onclick = async function () {
    $("import-result").textContent = "导入中…";
    var out = await api("/api/selection/imports/from-default", { method: "POST" });
    $("import-result").textContent = JSON.stringify(out.data, null, 2);
  };

  $("btn-change-json").onclick = function () {
    var latest = state.latestHandoff || {};
    var blob = new Blob(
      [
        JSON.stringify(
          {
            handoff_id: state.lastHandoffId || latest.handoff_id || null,
            product: state.product,
            new_lesson_uid: state.lessonUid,
            paper_url: latest.paper_url || null,
            change_notes: $("change-notes").value,
            question_ids: latest.question_ids || null,
            question_ids_list: latest.question_ids_list || null,
            kp: latest.kp || null,
            diff_summary: latest.diff_summary || null,
          },
          null,
          2
        ),
      ],
      { type: "application/json" }
    );
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "selection-change-request.json";
    a.click();
    URL.revokeObjectURL(a.href);
  };

  $("product-tabs").onclick = function (ev) {
    var btn = ev.target.closest("[data-product]");
    if (!btn) return;
    state.product = btn.getAttribute("data-product");
    Array.prototype.forEach.call(document.querySelectorAll(".sel-tab"), function (el) {
      el.classList.toggle("is-active", el === btn);
    });
    delete $("brief-notes").dataset.touched;
    delete $("brief-kp").dataset.touched;
    delete $("brief-plan").dataset.touched;
    state.demandRound1 = null;
    state.demandNext = null;
    state.cacheByQueId = {};
    refreshWorkspace();
  };

  $("lesson-tbody").onclick = function (ev) {
    var btn = ev.target.closest(".sel-open");
    if (!btn) return;
    state.lessonUid = btn.getAttribute("data-uid");
    state.lessonName = btn.getAttribute("data-name");
    state.lastHandoffId = null;
    state.latestHandoff = null;
    state.workspaceState = null;
    state.workspaceId = null;
    state.demandRound1 = null;
    state.demandNext = null;
    state.cacheByQueId = {};
    delete $("brief-notes").dataset.touched;
    delete $("brief-kp").dataset.touched;
    delete $("brief-plan").dataset.touched;
    $("ws-title").textContent = "工作台 · " + state.lessonName;
    $("review-box").hidden = true;
    $("review-readonly").hidden = true;
    $("old-disp-box").hidden = true;
    $("candidates-box").hidden = true;
    $("finalize-box").hidden = true;
    $("finalize-result").hidden = true;
    setDemandPreview("demand-preview", "", false);
    setDemandPreview("next-demand-preview", "", false);
    $("btn-copy-demand-md").disabled = true;
    $("btn-copy-demand-json").disabled = true;
    $("btn-dl-demand-json").disabled = true;
    $("btn-copy-next-md").disabled = true;
    $("btn-copy-next-json").disabled = true;
    showWorkspace();
    refreshWorkspace();
  };

  if (new URLSearchParams(location.search).get("tab") === "import") {
    $("panel-import").open = true;
    $("panel-import").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  showLessons();
  loadFilterOptions();
})();
