(function () {
  "use strict";

  const state = {
    lang: "zh-CN",
    appState: null,
    report: null,
    extensions: [],
    bridgeToken: "",
    activeBridgeJob: null,
    confirmCallback: null,
    pollTimer: null,
    busy: false,
  };

  function t(key) {
    const dict = (window.WIREDECK_I18N && window.WIREDECK_I18N[state.lang]) || window.WIREDECK_I18N["zh-CN"];
    return dict[key] || key;
  }

  function applyI18n() {
    document.documentElement.lang = state.lang;
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const k = el.getAttribute("data-i18n");
      if (k) el.textContent = t(k);
    });
  }

  function showToast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(showToast._tid);
    showToast._tid = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function sevIcon(sev) {
    if (sev === "ok") return "✓";
    if (sev === "warn") return "!";
    if (sev === "fail") return "✕";
    if (sev === "skipped") return "—";
    return "○";
  }

  function sevLabel(sev) {
    return `${sevIcon(sev)} ${t("sev_" + (sev || "idle"))}`;
  }

  function switchView(name) {
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    const target = document.getElementById("view" + name.charAt(0).toUpperCase() + name.slice(1));
    if (target) target.classList.add("active");
    document.getElementById("btnOpenAi").classList.toggle("active", name === "ai");
  }

  function getApi() {
    return window.pywebview && window.pywebview.api;
  }

  function severityRank(s) {
    return { skipped: 0, idle: 1, ok: 2, warn: 3, fail: 4 }[s] || 0;
  }

  function renderReport(report) {
    state.report = report;
    const layers = report.layers || [];
    const byId = {};
    layers.forEach((l) => { byId[l.layer] = l; });

    // Mode pill
    const modePill = document.getElementById("modePill");
    modePill.className = "mode-pill " + (report.mode || "normal");
    modePill.textContent = t("mode_" + (report.mode || "normal"));

    // Headline
    const dot = document.getElementById("headlineDot");
    const pf = report.primary_fault;
    dot.className = "pulse-dot " + (pf ? pf.severity : (report.mode === "direct" ? "ok" : "idle"));
    document.getElementById("headlineText").textContent = report.headline || "";

    const profileMap = { xbox: "Xbox Series X|S", ps5: "PS5 · 轻量模式", generic: "通用主机 · 轻量" };
    document.getElementById("profileChip").textContent = profileMap[report.console_profile] || report.console_profile;

    // Combine L5 + L6 into clean 5th node on Home screen
    const l5 = byId.L5 || { layer: "L5", status: "idle", summary: "待检测", faults: [] };
    const l6 = byId.L6 || { layer: "L6", status: "skipped", summary: "", faults: [] };
    const combinedStatus = severityRank(l6.status) > severityRank(l5.status) ? l6.status : l5.status;
    const combinedSummary = severityRank(l5.status) >= severityRank(l6.status) ? l5.summary : l6.summary;
    const combinedNode = {
      key: "node_console",
      layerObj: {
        layer: "L5/L6",
        name: t("node_console"),
        status: combinedStatus,
        summary: combinedSummary,
        faults: [...(l5.faults || []), ...(l6.faults || [])],
        metrics: Object.assign({}, l5.metrics, l6.metrics),
      },
    };

    const nodes = [
      { key: "node_nic", layerObj: byId.L1 },
      { key: "node_cable", layerObj: byId.L2 },
      { key: "node_gate", layerObj: byId.L3 },
      { key: "node_share", layerObj: byId.L4 },
      combinedNode,
    ];

    const grid = document.getElementById("chainGrid");
    grid.innerHTML = "";
    nodes.forEach((n, idx) => {
      const l = n.layerObj || { status: "idle", summary: "—", faults: [] };
      const card = document.createElement("div");
      card.className = `chain-node sev-${l.status}`;
      card.innerHTML = `
        <div class="node-name">${escapeHtml(t(n.key))}</div>
        <div class="sev-badge ${escapeHtml(l.status)}">${escapeHtml(sevLabel(l.status))}</div>
        <div class="node-summary">${escapeHtml(l.summary || "—")}</div>
      `;
      card.addEventListener("click", () => openNodeDetail(t(n.key), l));
      grid.appendChild(card);

      if (idx < nodes.length - 1) {
        const arr = document.createElement("div");
        const arrClass = l.status === "ok" ? "ok" : (l.status === "fail" ? "fail" : "");
        arr.className = `chain-arrow ${arrClass}`;
        arr.textContent = "→";
        grid.appendChild(arr);
      }
    });

    // Breakpoint card
    const bpCard = document.getElementById("breakpointCard");
    if (pf) {
      bpCard.classList.remove("hidden");
      bpCard.classList.toggle("warn", pf.severity === "warn");
      const badge = document.getElementById("bpBadge");
      badge.className = `sev-badge ${pf.severity}`;
      badge.textContent = sevLabel(pf.severity);
      document.getElementById("bpTitle").textContent = pf.title || pf.code;
      document.getElementById("bpMessage").textContent = pf.message || "";
      document.getElementById("bpExtPill").style.display = pf.source === "extension" ? "inline-flex" : "none";

      const fixBtn = document.getElementById("bpFixBtn");
      if (pf.fix_action) {
        fixBtn.style.display = "inline-flex";
        if (pf.fix_action === "clear-netnat") {
          fixBtn.textContent = t("btn_confirm_clear");
        } else if (pf.fix_action === "download-stream") {
          fixBtn.textContent = t("btn_download_stream");
        } else {
          fixBtn.textContent = t("btn_fix_now");
        }
        fixBtn.onclick = () => handleFixAction(pf);
      } else {
        fixBtn.style.display = "none";
      }
    } else {
      bpCard.classList.add("hidden");
    }

    // Main CTA buttons
    const btnEn = document.getElementById("btnEnableDirect");
    btnEn.disabled = state.busy || !report.can_enable_direct;
    btnEn.classList.toggle("is-active", report.mode === "direct");

    // Update Wizard step badges
    updateWizardState(byId, report);
    // Update NIC dropdown options in settings
    if (byId.L1 && byId.L1.metrics && byId.L1.metrics.physical_ethernets) {
      populateNicSelect(byId.L1.metrics.physical_ethernets);
    }
  }

  function updateWizardState(byId, report) {
    const l1 = byId.L1 || { status: "idle" };
    const l2 = byId.L2 || { status: "idle" };
    const l5 = byId.L5 || { status: "idle" };
    const l6 = byId.L6 || { status: "skipped", faults: [], metrics: {} };

    const s1Status = (l1.status === "ok" && l2.status === "ok") ? "ok" : (l1.status === "fail" ? "fail" : "warn");
    setBadge("wizS1Badge", s1Status);
    document.getElementById("wizStep1").classList.toggle("done", s1Status === "ok");

    const isXbox = report.console_profile === "xbox";
    document.getElementById("wizStep2").classList.toggle("skipped", !isXbox);
    document.getElementById("wizStep3").classList.toggle("skipped", !isXbox);
    if (!isXbox) {
      setBadge("wizS2Badge", "skipped");
      setBadge("wizS3Badge", "skipped");
    } else {
      const hasUwpMiss = (l6.faults || []).some((f) => f.code === "UWP_LOOPBACK_MISSING");
      const hasClientMiss = (l6.faults || []).some((f) => f.code === "STREAM_CLIENT_MISSING");
      setBadge("wizS2Badge", hasUwpMiss ? "warn" : "ok");
      document.getElementById("wizStep2").classList.toggle("done", !hasUwpMiss);
      setBadge("wizS3Badge", hasClientMiss ? "warn" : "ok");
      document.getElementById("wizStep3").classList.toggle("done", !hasClientMiss);
    }
    setBadge("wizS4Badge", l5.status === "ok" ? "ok" : "idle");
    document.getElementById("wizStep4").classList.toggle("done", l5.status === "ok");
  }

  function setBadge(id, sev) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `sev-badge ${sev}`;
    el.textContent = sevLabel(sev);
  }

  function openNodeDetail(title, layer) {
    document.getElementById("detailTitle").textContent = `${title} (${layer.layer})`;
    const faults = layer.faults || [];
    const lines = [];
    if (faults.length === 0) {
      lines.push(`状态：${sevLabel(layer.status)}（${layer.summary || "无异常"}）`);
    } else {
      faults.forEach((f) => {
        const tag = f.source === "extension" ? `[AI 扩展: ${f.ext_id}] ` : "";
        lines.push(`• ${tag}[${f.code}] ${f.title} — ${f.message}`);
      });
    }
    if (layer.metrics && Object.keys(layer.metrics).length > 0) {
      lines.push("");
      lines.push("Metrics: " + JSON.stringify(layer.metrics));
    }
    document.getElementById("detailBody").innerHTML = lines.map((x) => `<div>${escapeHtml(x)}</div>`).join("");
    document.getElementById("detailModal").classList.add("open");
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function handleFixAction(fault) {
    if (fault.fix_action === "download-stream") {
      switchView("wizard");
      return;
    }
    if (fault.fix_action === "clear-netnat") {
      const names = (fault.details && fault.details.nat_names) ? fault.details.nat_names.join(", ") : "";
      document.getElementById("confirmBody").textContent =
        `${t("confirm_netnat_body")}${names ? " (" + names + ")" : ""}`;
      state.confirmCallback = () => runAction("clear-netnat", { force: true });
      document.getElementById("confirmModal").classList.add("open");
      return;
    }
    if (fault.fix_action === "run-ext" && fault.ext_id) {
      await runAction("run-ext", { ext_id: fault.ext_id });
      return;
    }
    await runAction(fault.fix_action, {});
  }

  async function runAction(action, opts) {
    const api = getApi();
    if (!api) return;
    state.busy = true;
    showToast(t("toast_working"));
    try {
      const res = await api.run_action(action, opts || {});
      showToast(res.message || (res.ok ? "OK" : res.code));
      await refreshAll();
    } catch (err) {
      showToast(String(err));
    } finally {
      state.busy = false;
    }
  }

  function populateNicSelect(nics) {
    const sel = document.getElementById("setEth");
    const current = (state.appState && state.appState.config && state.appState.config.ethernet_interface) || "";
    sel.innerHTML = `<option value="">${t("set_eth_auto")}</option>`;
    nics.forEach((name) => {
      if (!name) return;
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === current) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function renderExtensions(entries) {
    state.extensions = entries || [];
    const list = document.getElementById("extList");
    list.innerHTML = "";
    if (!entries || entries.length === 0) {
      list.innerHTML = `<div class="step-desc">${t("ai_ext_empty")}</div>`;
      return;
    }
    entries.forEach((e) => {
      const m = e.manifest || {};
      const row = document.createElement("div");
      row.className = "ext-item";
      const statusBadge = e.valid
        ? `<span class="sev-badge ok">${m.layer || "L1"}</span>`
        : `<span class="sev-badge fail">INVALID</span>`;
      const desc = e.valid ? (m.description || `author: ${m.author || "User"}`) : (e.errors || []).join("; ");
      row.innerHTML = `
        <div class="ext-meta">
          <div class="ext-name">
            ${statusBadge}
            <span>${escapeHtml(m.name || m.id || "unknown")}</span>
            <span class="ext-pill">${t("badge_ai_ext")}</span>
          </div>
          <div class="ext-sub">${escapeHtml(desc)}</div>
        </div>
        <div style="display:flex;gap:6px;">
          ${e.valid && m.check_script ? `<button class="btn-sm" data-ext-check="${escapeHtml(m.id)}">${t("ai_run_check")}</button>` : ""}
          ${e.valid && m.fix_script ? `<button class="btn-sm primary" data-ext-fix="${escapeHtml(m.id)}">${t("ai_run_fix")}</button>` : ""}
        </div>
      `;
      list.appendChild(row);
    });

    list.querySelectorAll("[data-ext-check]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-ext-check");
        const api = getApi();
        if (!api) return;
        const r = await api.run_extension(id, "check");
        showToast(`[${r.status}] ${r.message || r.code}`);
        await refreshAll();
      });
    });
    list.querySelectorAll("[data-ext-fix]").forEach((btn) => {
      btn.addEventListener("click", () => runAction("run-ext", { ext_id: btn.getAttribute("data-ext-fix") }));
    });
  }

  function renderBridgeState(bridge) {
    const toggle = document.getElementById("bridgeToggle");
    const box = document.getElementById("bridgeDetails");
    toggle.checked = Boolean(bridge && bridge.running);
    if (bridge && bridge.running) {
      box.style.display = "flex";
      document.getElementById("bridgeUrl").textContent = bridge.url || "";
      state.bridgeToken = bridge.token || "";
      document.getElementById("bridgeTokenMask").textContent = state.bridgeToken
        ? state.bridgeToken.slice(0, 8) + "••••••••" + state.bridgeToken.slice(-6)
        : "";
      startBridgePolling();
    } else {
      box.style.display = "none";
      state.bridgeToken = "";
      stopBridgePolling();
    }
  }

  function startBridgePolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(async () => {
      const api = getApi();
      if (!api || state.activeBridgeJob) return;
      try {
        const st = await api.get_state();
        const pending = (st.bridge && st.bridge.pending_jobs) || [];
        if (pending.length > 0) {
          state.activeBridgeJob = pending[0];
          document.getElementById("bridgeReqBody").textContent =
            `Job: ${pending[0].job_id}\nAction: ${pending[0].action}\nOptions: ${JSON.stringify(pending[0].opts || {})}`;
          document.getElementById("bridgeModal").classList.add("open");
        }
      } catch (_) {}
    }, 1500);
  }

  function stopBridgePolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function syncConfigDisplay(cfg) {
    state.lang = cfg.language || "zh-CN";
    applyI18n();
    document.getElementById("setLanguage").value = cfg.language || "zh-CN";
    document.getElementById("setProfile").value = cfg.console_profile || "xbox";
    document.getElementById("setHostIp").value = cfg.host_ip || "192.168.50.2";
    document.getElementById("setPeerIp").value = cfg.peer_ip || "192.168.50.1";
    document.getElementById("subnetMeta").textContent = `${cfg.host_ip} ↔ ${cfg.peer_ip}`;
    document.getElementById("cardPeerIp").textContent = cfg.peer_ip;
    document.getElementById("cardMask").textContent = cfg.subnet_mask || "255.255.255.0";
    document.getElementById("cardHostIp").textContent = cfg.host_ip;
    document.getElementById("cardDns").textContent = (cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" / ");
  }

  async function refreshAll(silent) {
    const api = getApi();
    if (!api) return;
    const st = await api.get_state();
    state.appState = st;
    document.getElementById("versionPill").textContent = "v" + st.app_version;
    syncConfigDisplay(st.config);
    renderBridgeState(st.bridge);
    const [rep, exts] = await Promise.all([api.diagnose(), api.list_extensions()]);
    renderReport(rep);
    renderExtensions(exts);

    // Honor preview initial view for clean high-DPI screenshots
    if (st.preview_view && !refreshAll._previewApplied) {
      refreshAll._previewApplied = true;
      switchView(st.preview_view);
    }
    if (!silent) showToast(t("toast_refreshed"));
  }

  async function copyText(text) {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) {}
    const api = getApi();
    if (api && api.copy_to_clipboard) {
      await api.copy_to_clipboard(text);
      return true;
    }
    return false;
  }

  async function exportAndCopyAi() {
    const api = getApi();
    if (!api) return;
    const res = await api.export_for_ai();
    if (res.ok) {
      await copyText(res.prompt);
      showToast(t("toast_copied"));
    } else {
      showToast(res.message || "Export failed");
    }
  }

  function wireEvents() {
    document.getElementById("btnRefresh").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnOpenAi").addEventListener("click", () => switchView("ai"));
    document.getElementById("btnOpenWizard").addEventListener("click", () => switchView("wizard"));
    document.querySelectorAll("[data-go='home']").forEach((b) =>
      b.addEventListener("click", () => switchView("home"))
    );

    document.getElementById("btnEnableDirect").addEventListener("click", () => runAction("enable-direct", {}));
    document.getElementById("btnDisableDirect").addEventListener("click", () => runAction("disable-direct", {}));
    document.getElementById("btnCopyAiQuick").addEventListener("click", exportAndCopyAi);
    document.getElementById("btnAiExportCopy").addEventListener("click", exportAndCopyAi);

    const launchFn = async () => {
      const api = getApi();
      if (!api) return;
      const r = await api.launch_stream_client();
      showToast(r.message || "OK");
    };
    document.getElementById("btnLaunchStream").addEventListener("click", launchFn);
    document.getElementById("btnWizLaunch").addEventListener("click", launchFn);

    document.getElementById("btnWizFixNic").addEventListener("click", () => runAction("fix-nic", {}));
    document.getElementById("btnWizFixUwp").addEventListener("click", () => runAction("fix-uwp", {}));
    document.getElementById("btnWizTestPeer").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnWizCopyIp").addEventListener("click", async () => {
      const cfg = (state.appState && state.appState.config) || {};
      const txt = `IP: ${cfg.peer_ip || "192.168.50.1"}\nMask: ${cfg.subnet_mask || "255.255.255.0"}\nGateway: ${cfg.host_ip || "192.168.50.2"}\nDNS: ${(cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" / ")}`;
      await copyText(txt);
      showToast(t("toast_copied"));
    });
    document.getElementById("btnWizDownload").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      showToast(t("toast_working"));
      const r = await api.download_xstreaming();
      showToast(r.message || (r.ok ? "OK" : "Error"));
      await refreshAll(true);
    });

    document.getElementById("btnOpenExtFolder").addEventListener("click", () => getApi() && getApi().open_folder("extensions"));
    document.getElementById("btnOpenLogs").addEventListener("click", () => getApi() && getApi().open_folder("logs"));
    document.getElementById("btnOpenData").addEventListener("click", () => getApi() && getApi().open_folder("data"));
    document.getElementById("btnResetNic").addEventListener("click", async () => {
      document.getElementById("settingsModal").classList.remove("open");
      await runAction("reset-nic", {});
    });

    document.getElementById("bridgeToggle").addEventListener("change", async (e) => {
      const api = getApi();
      if (!api) return;
      const r = await api.toggle_bridge(Boolean(e.target.checked));
      renderBridgeState(r);
    });
    document.getElementById("btnCopyToken").addEventListener("click", async () => {
      if (state.bridgeToken) {
        await copyText(state.bridgeToken);
        showToast(t("toast_copied"));
      }
    });

    document.getElementById("btnOpenSettings").addEventListener("click", () => {
      document.getElementById("settingsModal").classList.add("open");
    });
    document.getElementById("btnCloseSettings").addEventListener("click", () => {
      document.getElementById("settingsModal").classList.remove("open");
    });
    document.getElementById("btnSaveSettings").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const patch = {
        language: document.getElementById("setLanguage").value,
        console_profile: document.getElementById("setProfile").value,
        ethernet_interface: document.getElementById("setEth").value,
        host_ip: document.getElementById("setHostIp").value.trim(),
        peer_ip: document.getElementById("setPeerIp").value.trim(),
      };
      await api.save_settings(patch);
      document.getElementById("settingsModal").classList.remove("open");
      showToast(t("toast_saved"));
      await refreshAll(true);
    });

    document.getElementById("btnConfirmCancel").addEventListener("click", () => {
      document.getElementById("confirmModal").classList.remove("open");
      state.confirmCallback = null;
    });
    document.getElementById("btnConfirmOk").addEventListener("click", async () => {
      document.getElementById("confirmModal").classList.remove("open");
      const cb = state.confirmCallback;
      state.confirmCallback = null;
      if (cb) await cb();
    });

    document.getElementById("btnCloseDetail").addEventListener("click", () => {
      document.getElementById("detailModal").classList.remove("open");
    });

    document.getElementById("btnBridgeDeny").addEventListener("click", async () => {
      document.getElementById("bridgeModal").classList.remove("open");
      const job = state.activeBridgeJob;
      state.activeBridgeJob = null;
      if (job && getApi()) await getApi().approve_bridge_job(job.job_id, false);
    });
    document.getElementById("btnBridgeAllow").addEventListener("click", async () => {
      document.getElementById("bridgeModal").classList.remove("open");
      const job = state.activeBridgeJob;
      state.activeBridgeJob = null;
      if (job && getApi()) {
        showToast(t("toast_working"));
        const res = await getApi().approve_bridge_job(job.job_id, true);
        const jr = res && res.job && res.job.result;
        if (jr && (jr.message || jr.code)) {
          showToast(jr.message || jr.code);
        }
        await refreshAll(true);
      }
    });
  }

  window.addEventListener("DOMContentLoaded", () => {
    wireEvents();
    applyI18n();
  });

  window.addEventListener("pywebviewready", () => {
    refreshAll(true);
  });
})();
