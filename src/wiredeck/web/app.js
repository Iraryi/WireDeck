(function () {
  "use strict";

  const state = {
    lang: "zh-CN",
    theme: "light",
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

  function applyTheme(theme) {
    state.theme = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", state.theme);
  }

  function showToast(msg) {
    const el = document.getElementById("toast");
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(showToast._tid);
    showToast._tid = setTimeout(() => el.classList.remove("show"), 2600);
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function sevIcon(sev) {
    if (sev === "ok") return "●";
    if (sev === "warn") return "▲";
    if (sev === "fail") return "✕";
    if (sev === "skipped") return "—";
    return "○";
  }

  function sevLabel(sev) {
    return `${sevIcon(sev)} ${t("sev_" + (sev || "idle"))}`;
  }

  function severityRank(s) {
    return { skipped: 0, idle: 1, ok: 2, warn: 3, fail: 4 }[s] || 0;
  }

  function switchView(name) {
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    const target = document.getElementById("view" + name.charAt(0).toUpperCase() + name.slice(1));
    if (target) target.classList.add("active");
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-nav") === name);
    });
  }

  function getApi() {
    return window.pywebview && window.pywebview.api;
  }

  function describeLayerDetail(layer, combinedL6) {
    const faults = layer.faults || [];
    const primary = faults.find((f) => f.severity === "fail") || faults.find((f) => f.severity === "warn");
    if (primary) {
      return primary.message || primary.title;
    }
    const m = layer.metrics || {};
    if (layer.layer === "L1") {
      return m.selected_ethernet ? `${m.selected_ethernet} · 节能已关` : (layer.summary || "—");
    }
    if (layer.layer === "L2") {
      return layer.summary ? `${layer.summary} 全双工` : "—";
    }
    if (layer.layer === "L3") {
      return "无 VPN TUN / ICS / 端口冲突";
    }
    if (layer.layer === "L4") {
      return m.nat_active ? "WireDeckNat 运行中 · IP Forwarding 已开" : "未开启（日常上网模式）";
    }
    if (layer.layer === "L5/L6") {
      const parts = [];
      if (m.peer_ip) parts.push(m.peer_ip);
      if (layer.summary) parts.push(layer.summary);
      if (combinedL6 && combinedL6.status === "ok") parts.push("串流端已就绪");
      if (combinedL6 && combinedL6.status === "skipped") parts.push("轻量模式");
      return parts.join(" · ") || "—";
    }
    return layer.summary || "—";
  }

  function renderReport(report) {
    state.report = report;
    const layers = report.layers || [];
    const byId = {};
    layers.forEach((l) => { byId[l.layer] = l; });

    document.getElementById("modeLabel").textContent = t("mode_" + (report.mode || "normal"));

    const dot = document.getElementById("headlineDot");
    const pf = report.primary_fault;
    dot.className = "status-led " + (pf ? pf.severity : (report.mode === "direct" ? "ok" : "idle"));
    document.getElementById("headlineText").textContent = report.headline || "";

    const ethName = (byId.L1 && byId.L1.metrics && byId.L1.metrics.selected_ethernet) || "";
    const speedTxt = (byId.L2 && byId.L2.summary) || "";
    const shareTxt = (byId.L4 && byId.L4.summary) || "";
    const metaParts = [];
    if (ethName) metaParts.push(ethName);
    if (speedTxt && speedTxt !== "待检测") metaParts.push(speedTxt);
    if (shareTxt) metaParts.push(`WinNAT ${shareTxt}`);
    document.getElementById("ctrlMeta").textContent = metaParts.join(" · ") || "—";

    const profileMap = { xbox: "Xbox Series X|S", ps5: "PS5（轻量）", generic: "通用主机" };
    document.getElementById("sbProfile").textContent = profileMap[report.console_profile] || report.console_profile;

    // Combine L5 + L6 cleanly into Row 5
    const l5 = byId.L5 || { layer: "L5", status: "idle", summary: "待检测", faults: [], metrics: {} };
    const l6 = byId.L6 || { layer: "L6", status: "skipped", summary: "", faults: [], metrics: {} };
    const combinedStatus = severityRank(l6.status) > severityRank(l5.status) ? l6.status : l5.status;
    const combinedSummary = severityRank(l5.status) >= severityRank(l6.status) ? l5.summary : l6.summary;
    const combinedNode = {
      key: "node_console",
      l6Ref: l6,
      layerObj: {
        layer: "L5/L6",
        name: t("node_console"),
        status: combinedStatus,
        summary: combinedSummary,
        faults: [...(l5.faults || []), ...(l6.faults || [])],
        metrics: Object.assign({}, l5.metrics, l6.metrics),
      },
    };

    const rows = [
      { key: "node_nic", layerObj: byId.L1 },
      { key: "node_cable", layerObj: byId.L2 },
      { key: "node_gate", layerObj: byId.L3 },
      { key: "node_share", layerObj: byId.L4 },
      combinedNode,
    ];

    const table = document.getElementById("diagTable");
    table.innerHTML = "";
    rows.forEach((r) => {
      const l = r.layerObj || { layer: "", status: "idle", summary: "—", faults: [] };
      const rowEl = document.createElement("div");
      rowEl.className = `diag-row sev-${l.status}`;
      const detailTxt = describeLayerDetail(l, r.l6Ref);
      const hasExt = (l.faults || []).some((f) => f.source === "extension");
      const fixableFault = (l.faults || []).find((f) => f.fix_action && (f.severity === "fail" || f.severity === "warn"));

      rowEl.innerHTML = `
        <div class="diag-name">${escapeHtml(t(r.key))}</div>
        <div class="diag-detail">${escapeHtml(detailTxt)}</div>
        <div class="diag-right">
          ${hasExt ? `<span class="ext-tag">${escapeHtml(t("badge_ai_ext"))}</span>` : ""}
          <span class="sev-text ${escapeHtml(l.status)}">${escapeHtml(sevLabel(l.status))}</span>
        </div>
      `;

      if (fixableFault) {
        const rightBox = rowEl.querySelector(".diag-right");
        const fixBtn = document.createElement("button");
        fixBtn.className = "btn btn-sm";
        fixBtn.type = "button";
        fixBtn.textContent = fixableFault.fix_action === "clear-netnat" ? t("btn_confirm_clear") : t("btn_fix_now");
        fixBtn.addEventListener("click", (ev) => {
          ev.stopPropagation();
          handleFixAction(fixableFault);
        });
        rightBox.appendChild(fixBtn);
      }

      rowEl.addEventListener("click", () => openNodeDetail(t(r.key), l));
      table.appendChild(rowEl);
    });

    // Native InfoBar (breakpoint / warning)
    const bpCard = document.getElementById("breakpointCard");
    if (pf) {
      bpCard.classList.remove("hidden");
      bpCard.classList.toggle("warn", pf.severity === "warn");
      document.getElementById("bpIcon").textContent = pf.severity === "warn" ? "!" : "✕";
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

    // Action buttons
    const btnEn = document.getElementById("btnEnableDirect");
    btnEn.disabled = state.busy || !report.can_enable_direct;

    updateWizardState(byId, report);
    if (byId.L1 && byId.L1.metrics && byId.L1.metrics.physical_ethernets) {
      populateNicSelect(byId.L1.metrics.physical_ethernets);
    }
  }

  function updateWizardState(byId, report) {
    const l1 = byId.L1 || { status: "idle" };
    const l2 = byId.L2 || { status: "idle" };
    const l5 = byId.L5 || { status: "idle" };
    const l6 = byId.L6 || { status: "skipped", faults: [] };

    const s1Status = (l1.status === "ok" && l2.status === "ok") ? "ok" : (l1.status === "fail" ? "fail" : "warn");
    setBadge("wizS1Badge", s1Status);

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
      setBadge("wizS3Badge", hasClientMiss ? "warn" : "ok");
    }
    setBadge("wizS4Badge", l5.status === "ok" ? "ok" : "idle");
  }

  function setBadge(id, sev) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `sev-text ${sev}`;
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
      await refreshAll(true);
    } catch (err) {
      showToast(String(err));
    } finally {
      state.busy = false;
    }
  }

  function populateNicSelect(nics) {
    const sel = document.getElementById("setEth");
    const current = (state.appState && state.appState.config && state.appState.config.ethernet_interface) || "";
    sel.innerHTML = `<option value="">${escapeHtml(t("set_eth_auto"))}</option>`;
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
      list.innerHTML = `<div class="list-row"><div class="row-desc">${escapeHtml(t("ai_ext_empty"))}</div></div>`;
      return;
    }
    entries.forEach((e) => {
      const m = e.manifest || {};
      const row = document.createElement("div");
      row.className = "list-row";
      const badge = e.valid
        ? `<span class="ext-tag">${escapeHtml(m.layer || "L1")}</span>`
        : `<span class="sev-text fail">INVALID</span>`;
      const desc = e.valid ? (m.description || `author: ${m.author || "User"}`) : (e.errors || []).join("; ");
      row.innerHTML = `
        <div class="list-row-col">
          <div class="row-head">
            ${badge}
            <span>${escapeHtml(m.name || m.id || "unknown")}</span>
            <span class="ext-tag">${escapeHtml(t("badge_ai_ext"))}</span>
          </div>
          <div class="row-desc">${escapeHtml(desc)}</div>
        </div>
        <div class="row-actions">
          ${e.valid && m.check_script ? `<button class="btn btn-sm" data-ext-check="${escapeHtml(m.id)}">${escapeHtml(t("ai_run_check"))}</button>` : ""}
          ${e.valid && m.fix_script ? `<button class="btn btn-sm btn-primary" data-ext-fix="${escapeHtml(m.id)}">${escapeHtml(t("ai_run_fix"))}</button>` : ""}
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
        await refreshAll(true);
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
    applyTheme(cfg.theme || "light");
    applyI18n();
    document.getElementById("setTheme").value = state.theme;
    document.getElementById("setLanguage").value = cfg.language || "zh-CN";
    document.getElementById("setProfile").value = cfg.console_profile || "xbox";
    document.getElementById("setHostIp").value = cfg.host_ip || "192.168.50.2";
    document.getElementById("setPeerIp").value = cfg.peer_ip || "192.168.50.1";
    document.getElementById("sbSubnet").textContent = `${cfg.host_ip} → ${cfg.peer_ip}`;
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
    document.getElementById("versionLabel").textContent = "v" + st.app_version;
    syncConfigDisplay(st.config);
    renderBridgeState(st.bridge);
    const [rep, exts] = await Promise.all([api.diagnose(), api.list_extensions()]);
    renderReport(rep);
    renderExtensions(exts);

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

  async function exportAndCopyAi(showPreview) {
    const api = getApi();
    if (!api) return;
    const res = await api.export_for_ai();
    if (res.ok) {
      await copyText(res.prompt);
      const box = document.getElementById("aiPromptPreview");
      if (box) {
        box.textContent = res.prompt;
        if (showPreview) {
          box.style.display = "block";
        }
      }
      showToast(t("ai_copied_hint"));
    } else {
      showToast(res.message || "Export failed");
    }
  }

  function wireEvents() {
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.getAttribute("data-nav")));
    });

    document.getElementById("btnRefresh").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnWizRefresh").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnAiRefresh").addEventListener("click", () => refreshAll(false));

    document.getElementById("btnEnableDirect").addEventListener("click", () => runAction("enable-direct", {}));
    document.getElementById("btnDisableDirect").addEventListener("click", () => runAction("disable-direct", {}));
    document.getElementById("btnCopyAiQuick").addEventListener("click", () => exportAndCopyAi(false));
    document.getElementById("btnAiExportCopy").addEventListener("click", () => exportAndCopyAi(true));
    document.getElementById("btnAiPreviewPrompt").addEventListener("click", async () => {
      const box = document.getElementById("aiPromptPreview");
      if (box.style.display === "block") {
        box.style.display = "none";
        return;
      }
      if (!box.textContent.trim()) {
        const api = getApi();
        if (api) {
          const res = await api.export_for_ai();
          if (res.ok) box.textContent = res.prompt;
        }
      }
      box.style.display = "block";
    });
    document.getElementById("btnCreateSampleExt").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const res = await api.create_sample_extension();
      showToast(res.message || "OK");
      await refreshAll(true);
    });
    document.getElementById("btnCopyCliCmd").addEventListener("click", async () => {
      await copyText("wiredeck-cli.exe diagnose --json");
      showToast(t("toast_copied"));
    });

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
    document.getElementById("btnResetNic").addEventListener("click", () => runAction("reset-nic", {}));

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

    document.getElementById("btnSaveSettings").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const patch = {
        theme: document.getElementById("setTheme").value,
        language: document.getElementById("setLanguage").value,
        console_profile: document.getElementById("setProfile").value,
        ethernet_interface: document.getElementById("setEth").value,
        host_ip: document.getElementById("setHostIp").value.trim(),
        peer_ip: document.getElementById("setPeerIp").value.trim(),
      };
      await api.save_settings(patch);
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
