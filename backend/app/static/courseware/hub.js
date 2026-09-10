(function () {
  var lessonUid = null;
  var assetId = null;

  function $(id) {
    return document.getElementById(id);
  }

  function setStatus(msg, kind) {
    var el = $("status");
    if (!el) return;
    el.textContent = msg || "";
    el.className = "cw-status" + (kind ? " is-" + kind : "");
  }

  async function jfetch(url, opts) {
    var r = await fetch(url, opts);
    var d = null;
    try {
      d = await r.json();
    } catch (e) {
      d = { ok: false, error: "invalid json" };
    }
    return { r: r, d: d };
  }

  function showAsset(asset) {
    if (!asset) return;
    assetId = asset.id;
    $("btn-ocr").disabled = false;
    var st = asset.ocr_status || "";
    var res = asset.ocr_result_json || {};
    if (st === "ready" && res.kps && res.kps.length) {
      setStatus("OCR 成功（ready），已识别 " + res.kps.length + " 个 KP。可回选题页刷新齐套。", "ok");
    } else if (st === "failed") {
      setStatus(
        "OCR 失败：" +
          (res.error || "unknown") +
          (res.hint ? "。" + res.hint : "") +
          "。可再点「跑 OCR」重试。",
        "err"
      );
    } else if (st === "pending" || st === "running") {
      setStatus("已上传，状态 " + st + "。请点「跑 OCR」。", "busy");
    } else {
      setStatus("已选中资产 " + asset.id.slice(0, 8) + "… 状态=" + st, "");
    }
    $("out").textContent = JSON.stringify({ ok: true, asset: asset }, null, 2);
  }

  async function loadExistingAssets() {
    if (!lessonUid) return;
    var out = await jfetch(
      "/api/courseware/lessons/" + encodeURIComponent(lessonUid) + "/assets"
    );
    var assets = (out.d && out.d.assets) || [];
    if (!assets.length) {
      setStatus("尚未上传课件。请选择 PDF / 课件页 ZIP / 图片后点「上传」。", "");
      $("btn-ocr").disabled = true;
      return;
    }
    showAsset(assets[0]);
  }

  $("btn-load").onclick = async function () {
    var q = new URLSearchParams();
    if ($("ed").value.trim()) q.set("edition", $("ed").value.trim());
    if ($("gr").value.trim()) q.set("grade", $("gr").value.trim());
    if ($("sem").value.trim()) q.set("semester", $("sem").value.trim());
    var out = await jfetch("/api/selection/new-lessons?" + q.toString());
    $("tb").innerHTML =
      (out.d.items || [])
        .map(function (it) {
          return (
            "<tr><td>" +
            it.edition +
            " " +
            it.grade +
            it.semester +
            " · " +
            it.lesson_name +
            '</td><td><button data-uid="' +
            it.lesson_uid +
            '" data-name="' +
            it.lesson_name +
            '">选择</button></td></tr>'
          );
        })
        .join("") || "<tr><td colspan=2>无数据</td></tr>";
  };

  $("tb").onclick = function (ev) {
    var b = ev.target.closest("button[data-uid]");
    if (!b) return;
    lessonUid = b.getAttribute("data-uid");
    assetId = null;
    $("detail").hidden = false;
    $("d-title").textContent = b.getAttribute("data-name");
    $("btn-ocr").disabled = true;
    $("out").textContent = "";
    loadExistingAssets();
  };

  $("btn-up").onclick = async function () {
    if (!lessonUid) {
      setStatus("未选定新课，请从选题页「去补」进入，或先加载并选择课时。", "err");
      return;
    }
    if (!$("file").files[0]) {
      setStatus("请先选择 PDF、课件页 ZIP 或图片。", "err");
      return;
    }
    setStatus("上传中…", "busy");
    $("btn-up").disabled = true;
    try {
      var fd = new FormData();
      fd.append("file", $("file").files[0]);
      var r = await fetch(
        "/api/courseware/lessons/" + encodeURIComponent(lessonUid) + "/assets",
        { method: "POST", body: fd }
      );
      var d = await r.json();
      $("out").textContent = JSON.stringify(d, null, 2);
      if (d.ok && d.asset) {
        showAsset(d.asset);
        setStatus("上传成功。正在自动 OCR（复用旧课件/教材比对能力，可能要几十秒）…", "busy");
        await runOcr();
      } else {
        setStatus("上传失败：" + (d.error || r.status), "err");
      }
    } catch (e) {
      setStatus("上传异常：" + (e.message || e), "err");
    } finally {
      $("btn-up").disabled = false;
    }
  };

  async function runOcr() {
    if (!assetId) {
      setStatus("没有可识别的文件。请先上传成功（上传后会自动启用「跑 OCR」）。", "err");
      return;
    }
    setStatus("OCR 进行中，扫描版可能较慢，请稍候…", "busy");
    $("btn-ocr").disabled = true;
    try {
      var out = await jfetch(
        "/api/courseware/assets/" + encodeURIComponent(assetId) + "/ocr",
        { method: "POST" }
      );
      $("out").textContent = JSON.stringify(out.d, null, 2);
      if (out.d.ok && out.d.asset) {
        showAsset(out.d.asset);
      } else {
        setStatus("OCR 请求失败：" + (out.d.error || out.r.status), "err");
      }
    } catch (e) {
      setStatus("OCR 异常：" + (e.message || e), "err");
    } finally {
      $("btn-ocr").disabled = !assetId;
    }
  }

  $("btn-ocr").onclick = function () {
    runOcr();
  };

  var params = new URLSearchParams(location.search);
  if (params.get("lesson_uid")) {
    lessonUid = params.get("lesson_uid");
    $("detail").hidden = false;
    $("d-title").textContent = "加载课名中…";
    $("btn-ocr").disabled = true;
    $("out").textContent = "";
    (async function () {
      var out = await jfetch("/api/selection/new-lessons");
      var hit = (out.d.items || []).find(function (it) {
        return it.lesson_uid === lessonUid;
      });
      if (hit) {
        $("d-title").textContent =
          hit.edition +
          " " +
          hit.grade +
          hit.semester +
          " · " +
          hit.lesson_name +
          (hit.book_type === "diff_new" ? "（比对册）" : "");
        $("ed").value = hit.edition || "";
        $("gr").value = hit.grade != null ? String(hit.grade) : "";
        $("sem").value = hit.semester || "";
      } else {
        $("d-title").textContent = "已选定新课（系统 id，无需手填）";
      }
      await loadExistingAssets();
    })();
  }
})();
