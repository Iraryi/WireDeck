(function () {
  "use strict";

  const PRESET_ORDER = ["xbox", "playstation", "switch", "handheld", "other"];
  const DEFAULT_WIZARD_STEPS = {
    xbox: ["nic", "uwp", "client", "peer"],
    playstation: ["nic", "client", "peer"],
    switch: ["nic", "peer"],
    handheld: ["nic", "client", "peer"],
    other: ["nic", "peer"],
  };

  const state = {
    lang: "zh-CN",
    theme: "light",
    appState: null,
    report: null,
    extensions: [],
    toolkits: [],
    bridgeToken: "",
    activeBridgeJob: null,
    confirmCallback: null,
    pollTimer: null,
    busy: false,
    onbSelectedLang: "zh-CN",
    onbSelectedPreset: "xbox",
    onboardingDismissed: false,
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
    const refreshBtn = document.getElementById("btnRefresh");
    if (refreshBtn) refreshBtn.title = t("btn_refresh");
    if (document.getElementById("onboardingModal").classList.contains("open")) {
      renderOnboardingModal();
    }
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

  function getActivePresetId(report) {
    if (state.appState && state.appState.active_preset && state.appState.active_preset.id) {
      return state.appState.active_preset.id;
    }
    if (report && report.console_profile) return report.console_profile;
    return "xbox";
  }

  function getActivePresetMeta(report) {
    const id = getActivePresetId(report);
    if (state.appState && state.appState.active_preset && state.appState.active_preset.id === id) {
      return state.appState.active_preset;
    }
    return {
      id,
      wizard_steps: DEFAULT_WIZARD_STEPS[id] || DEFAULT_WIZARD_STEPS.other,
      uses_uwp_loopback: id === "xbox",
      uses_stream_client: id === "xbox" || id === "playstation" || id === "handheld",
      official_client_name: id === "playstation" ? "Chiaki-ng" : (id === "handheld" ? "Moonlight" : "XStreamingDesktop"),
    };
  }

  const FAULT_I18N = {
    "zh-TW": {
      NIC_NOT_FOUND: ["未偵測到有線網卡", "請檢查本機有線網卡或插好 USB 轉接器"],
      NIC_PCIE_PARENT_DISABLED: ["PCIe 上級通道停用", "網卡父級 PCIe 根連接埠被停用，點「一鍵修復」喚醒"],
      NIC_DISABLED: ["有線網卡已停用", "有線介面卡目前停用中，點「一鍵修復」立即啟用"],
      NIC_POWERSAVE_ENABLED: ["網卡節能已開啟", "EEE/綠色節能可能導致串流微卡頓，點「一鍵修復」關閉"],
      CABLE_UNPLUGGED: ["網路線未插好", "請確認網路線已插緊電腦與開機後的目標裝置"],
      LINK_DEGRADED_100M: ["協商速率僅 100M", "建議更換 Cat5e/Cat6 千兆網路線以確保穩定頻寬"],
      NIC_HW_ERRORS: ["線材硬體錯包偏高", "偵測到大量傳輸錯包，建議重插或更換網路線"],
      VPN_TUN_ACTIVE: ["VPN 接管預設路由", "請先暫時中斷 VPN 連線，再點「開啟遊戲直連」"],
      ICS_ACTIVE: ["系統 ICS 衝突中", "Windows ICS 會佔用 TUN/WinNAT 連接埠，點「一鍵修復」清理"],
      PORT_53_67_OCCUPIED: ["UDP 53/67 埠被佔用", "系統 DHCP/DNS 連接埠衝突，請清理 ICS 或釋放佔用服務"],
      PORT_53_NON_ICS_WARNING: ["第三方本機 DNS 佔用 53 埠", "不影響有線直連；已略過以免誤關您的本機 DNS 程式"],
      FOREIGN_NETNAT_EXISTS: ["偵測到其他 NetNat", "系統存在非 WireDeck 建立的 NetNat，請先確認清理"],
      ETH_IP_MISSING: ["有線靜態 IP 遺失", "請點「一鍵修復」重新寫入本機有線網段參數"],
      ETH_HAS_UNEXPECTED_GATEWAY: ["有線網卡誤設預設閘道", "直連網卡不應保留預設閘道，請重新套用直連修復"],
      FORWARDING_DISABLED: ["IPv4 轉發未開啟", "點「一鍵修復」啟用網路介面轉發以恢復上網共用"],
      FW_RULE_MISSING: ["子網路防火牆規則遺失", "點「一鍵修復」補回 WireDeck 專屬防火牆放行規則"],
      PEER_UNREACHABLE: ["目標裝置尚未連通", "請確認裝置開機並已在網路設定填入「首次精靈」靜態 IP"],
      HOST_INTERNET_DOWN: ["電腦本機無上網出口", "請檢查 Wi-Fi 或手機熱點是否已連網"],
      XBOX_LIVE_UNREACHABLE: ["Xbox Live 認證無法連線", "Xbox 伺服器未回應，請重試或檢查上游網路連線"],
      PSN_UNREACHABLE: ["PSN 認證端點無法連線", "PlayStation 認證未回應，請重試或檢查上游網路連線"],
      NINTENDO_UNREACHABLE: ["任天堂連線檢測未通過", "ctest.cdn.nintendo.net 未回應，請檢查上游網路或 DNS"],
      UWP_LOOPBACK_MISSING: ["UWP 迴圈代理攔截", "請套用 UWP 豁免，避免系統代理攔截微軟商店與 Xbox"],
      STREAM_CLIENT_MISSING: ["未偵測到 Xbox 串流端", "建議於「首次精靈」下載已驗證的 XStreaming 或安裝 Xbox 應用"],
      PS_CLIENT_MISSING: ["未偵測到 PS 串流端", "建議於「首次精靈」取得 Chiaki-ng 或安裝 PS Remote Play"],
      HANDHELD_CLIENT_MISSING: ["未偵測到掌機串流端", "建議於「首次精靈」取得 Moonlight 或安裝 Steam / Sunshine"],
    },
    "en": {
      NIC_NOT_FOUND: ["Wired NIC Not Found", "Check Ethernet hardware or plug in a USB wired adapter"],
      NIC_PCIE_PARENT_DISABLED: ["PCIe Port Disabled", "Parent PCIe root port is disabled; click Fix Now to wake it"],
      NIC_DISABLED: ["Wired NIC Disabled", "Wired adapter is disabled; click Fix Now to enable it"],
      NIC_POWERSAVE_ENABLED: ["NIC Power-Save On", "EEE/Green Ethernet can cause stream stutter; click Fix Now"],
      CABLE_UNPLUGGED: ["Cable Unplugged", "Plug Ethernet cable firmly into laptop and powered-on device"],
      LINK_DEGRADED_100M: ["100 Mbps Link", "Negotiated 100 Mbps; use a Cat5e/Cat6 Gigabit cable"],
      NIC_HW_ERRORS: ["Cable Packet Errors", "High hardware packet errors; reseat or replace Ethernet cable"],
      VPN_TUN_ACTIVE: ["VPN Taking Over Route", "Disconnect your VPN first, then click Enable Direct"],
      ICS_ACTIVE: ["Windows ICS Active", "Windows ICS conflicts with TUN/WinNAT; click Fix Now to clear"],
      PORT_53_67_OCCUPIED: ["UDP 53/67 In Use", "ICS/DHCP port conflict; clear ICS or stop conflicting service"],
      PORT_53_NON_ICS_WARNING: ["Local DNS on UDP 53", "Third-party local DNS uses port 53 (does not block Direct)"],
      FOREIGN_NETNAT_EXISTS: ["Other NetNat Found", "Another NetNat exists; confirm clearing before enabling Direct"],
      ETH_IP_MISSING: ["Wired Static IP Missing", "Static host IP missing; click Fix Now to reapply"],
      ETH_HAS_UNEXPECTED_GATEWAY: ["Unexpected Gateway", "Wired NIC should not hold a default gateway; reapply Direct"],
      FORWARDING_DISABLED: ["IP Forwarding Off", "IPv4 forwarding is disabled; click Fix Now to reapply"],
      FW_RULE_MISSING: ["Firewall Rule Missing", "WireDeck subnet firewall rule missing; click Fix Now"],
      PEER_UNREACHABLE: ["Device Unreachable", "Power on device and apply the static IP in First Setup"],
      HOST_INTERNET_DOWN: ["No Internet on Laptop", "Check your Wi-Fi or hotspot internet connection"],
      XBOX_LIVE_UNREACHABLE: ["Xbox Live Unreachable", "Xbox auth endpoint did not respond; retry or check network"],
      PSN_UNREACHABLE: ["PSN Unreachable", "PlayStation auth endpoint did not respond; check upstream link"],
      NINTENDO_UNREACHABLE: ["Nintendo Check Failed", "ctest.cdn.nintendo.net did not respond; check upstream DNS"],
      UWP_LOOPBACK_MISSING: ["UWP Loopback Blocked", "Exempt Store & Xbox apps so proxies do not block login"],
      STREAM_CLIENT_MISSING: ["No Xbox Stream Client", "Download verified XStreaming or install Xbox App in First Setup"],
      PS_CLIENT_MISSING: ["No PS Stream Client", "Get open-source Chiaki-ng or PS Remote Play in First Setup"],
      HANDHELD_CLIENT_MISSING: ["No Handheld Streamer", "Get Moonlight / Sunshine or use Steam Remote Play in First Setup"],
    },
    "ja": {
      NIC_NOT_FOUND: ["有線 LAN が見つかりません", "内蔵 LAN または USB 有線アダプターを確認してください"],
      NIC_PCIE_PARENT_DISABLED: ["PCIe 上位ポートが無効", "親 PCIe ルートポートが停止中です。「ワンクリック修復」で復帰します"],
      NIC_DISABLED: ["有線 LAN が無効", "有線アダプターが無効です。「ワンクリック修復」で有効化できます"],
      NIC_POWERSAVE_ENABLED: ["NIC 省電力機能が有効", "EEE 省電力は配信カクつきの原因になります。「ワンクリック修復」で解除"],
      CABLE_UNPLUGGED: ["LAN ケーブル未接続", "ケーブルが PC と電源オンの機器にしっかり挿さっているか確認してください"],
      LINK_DEGRADED_100M: ["100 Mbps リンクに低下", "Cat5e / Cat6 以上のギガビット対応ケーブルをご使用ください"],
      NIC_HW_ERRORS: ["ケーブル通信エラー多数", "ハードウェアエラーが増加しています。ケーブルを交換または差し直してください"],
      VPN_TUN_ACTIVE: ["VPN がルートを専有中", "一度 VPN を切断してから「直結を有効化」を押してください"],
      ICS_ACTIVE: ["Windows ICS が競合中", "Windows の ICS が WinNAT と競合します。「ワンクリック修復」で解除します"],
      PORT_53_67_OCCUPIED: ["UDP 53/67 ポート使用中", "ICS や DHCP 競合サービスを停止してください"],
      PORT_53_NON_ICS_WARNING: ["ローカル DNS が 53 番使用中", "他社ローカル DNS の利用であり直結はブロックされません"],
      FOREIGN_NETNAT_EXISTS: ["他社製の NetNat を検出", "WireDeck 以外の NetNat が存在します。削除を確認してください"],
      ETH_IP_MISSING: ["有線固定 IP が未設定", "ホスト側固定 IP がありません。「ワンクリック修復」で再適用します"],
      ETH_HAS_UNEXPECTED_GATEWAY: ["予期しないゲートウェイ", "有線直結側にはゲートウェイを設定しません。再適用して修正します"],
      FORWARDING_DISABLED: ["IPv4 転送が無効", "IP 転送がオフです。「ワンクリック修復」で有効化します"],
      FW_RULE_MISSING: ["ファイアウォール規則なし", "WireDeck サブネット規則がありません。「ワンクリック修復」で作成します"],
      PEER_UNREACHABLE: ["接続先機器に応答なし", "機器の電源を入れ、「初期設定」の固定 IP を設定してください"],
      HOST_INTERNET_DOWN: ["PC 側のインターネット未接続", "Wi-Fi またはテザリング回線を確認してください"],
      XBOX_LIVE_UNREACHABLE: ["Xbox Live 接続不可", "Xbox 認証サーバーが応答しません。上流回線を確認してください"],
      PSN_UNREACHABLE: ["PSN 認証サーバー接続不可", "PlayStation 認証エンドポイントが応答しません。回線を確認してください"],
      NINTENDO_UNREACHABLE: ["任天堂接続チェック失敗", "ctest.cdn.nintendo.net が応答しません。DNS や上流回線を確認してください"],
      UWP_LOOPBACK_MISSING: ["UWP ループバック遮断", "Store / Xbox アプリをプロキシ例外に追加してください"],
      STREAM_CLIENT_MISSING: ["Xbox 用アプリ未検出", "「初期設定」から検証済み XStreaming または Xbox アプリを導入してください"],
      PS_CLIENT_MISSING: ["PS 用配信アプリ未検出", "「初期設定」から Chiaki-ng または PS Remote Play を入手してください"],
      HANDHELD_CLIENT_MISSING: ["携帯機用配信アプリ未検出", "「初期設定」から Moonlight / Sunshine または Steam を導入してください"],
    },
  };

  function localizeFault(f) {
    if (!f) return null;
    if (f.source === "extension" || state.lang === "zh-CN") {
      return { title: f.title || f.code, message: f.message || "" };
    }
    const table = FAULT_I18N[state.lang];
    const pair = table && table[f.code];
    if (pair) return { title: pair[0], message: pair[1] };
    return { title: f.title || f.code, message: f.message || "" };
  }

  function localizeHeadline(report) {
    if (report.primary_fault) {
      if (state.lang === "zh-CN" && report.headline) return report.headline;
      const lf = localizeFault(report.primary_fault);
      return `${t("headline_issue_prefix")}${lf.title}`;
    }
    if (report.mode === "direct") return t("headline_direct");
    return t("headline_normal");
  }

  function setBusy(busy) {
    state.busy = busy;
    const prog = document.getElementById("homeProgress");
    if (prog) prog.classList.toggle("active", busy);
    const btnRef = document.getElementById("btnRefresh");
    const btnEn = document.getElementById("btnEnableDirect");
    const btnDis = document.getElementById("btnDisableDirect");
    if (btnRef) btnRef.disabled = busy;
    if (btnDis) btnDis.disabled = busy;
    if (btnEn) {
      const canEn = state.report ? Boolean(state.report.can_enable_direct) : false;
      btnEn.disabled = busy || !canEn;
    }
  }

  function renderLoadingSkeleton() {
    const table = document.getElementById("diagTable");
    if (!table || table.children.length > 0) return;
    const keys = ["node_nic", "node_cable", "node_gate", "node_share", "node_console"];
    keys.forEach((k) => {
      const row = document.createElement("div");
      row.className = "diag-row sev-idle";
      row.innerHTML = `
        <div class="diag-name">${escapeHtml(t(k))}</div>
        <div class="diag-detail">${escapeHtml(t("loading_row"))}</div>
        <div class="diag-right"><span class="sev-text idle">${escapeHtml(sevLabel("idle"))}</span></div>
      `;
      table.appendChild(row);
    });
  }

  function describeLayerDetail(layer, combinedL6) {
    const faults = layer.faults || [];
    const primary = faults.find((f) => f.severity === "fail") || faults.find((f) => f.severity === "warn");
    if (primary) {
      const lf = localizeFault(primary);
      return lf.message || lf.title;
    }
    const m = layer.metrics || {};
    if (layer.layer === "L1") {
      return m.selected_ethernet
        ? `${m.selected_ethernet} · ${t("detail_ps_off")}`
        : (layer.summary || "—");
    }
    if (layer.layer === "L2") {
      if (layer.status === "idle") return t("detail_wait_nic");
      return layer.summary ? `${layer.summary} ${t("detail_full_duplex")}` : "—";
    }
    if (layer.layer === "L3") {
      return t("detail_no_conflict");
    }
    if (layer.layer === "L4") {
      return m.nat_active ? t("detail_nat_on") : t("detail_nat_standby");
    }
    if (layer.layer === "L5/L6") {
      if (layer.status === "idle") {
        const appTail = combinedL6 && combinedL6.status === "ok" ? ` · ${t("detail_client_ready")}` : "";
        return t("detail_wait_cable") + appTail;
      }
      const parts = [];
      if (m.peer_ip) parts.push(m.peer_ip);
      if (typeof m.peer_rtt_ms === "number") parts.push(`${m.peer_rtt_ms.toFixed(1)} ms`);
      if (combinedL6 && combinedL6.status === "ok") parts.push(t("detail_client_ready"));
      if (combinedL6 && combinedL6.status === "skipped") parts.push(t("detail_no_client_preset"));
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
    document.getElementById("headlineText").textContent = localizeHeadline(report);

    const ethName = (byId.L1 && byId.L1.metrics && byId.L1.metrics.selected_ethernet) || "";
    const speedTxt = (byId.L2 && byId.L2.summary) || "";
    const shareActive = Boolean(byId.L4 && byId.L4.metrics && byId.L4.metrics.nat_active);
    const metaParts = [];
    if (ethName) metaParts.push(ethName);
    if (speedTxt && speedTxt !== "待检测" && speedTxt !== "未插线") metaParts.push(speedTxt);
    metaParts.push(shareActive ? t("meta_nat_on") : t("meta_nat_off"));
    document.getElementById("ctrlMeta").textContent = metaParts.join(" · ") || "—";

    const presetId = getActivePresetId(report);
    const presetMeta = getActivePresetMeta(report);
    document.getElementById("sbProfile").textContent = t(`preset_${presetId}_name`);

    const launchBtn = document.getElementById("btnLaunchStream");
    const launchSep = document.getElementById("sbLaunchSep");
    const showLaunch = Boolean(presetMeta.uses_stream_client);
    if (launchBtn) launchBtn.style.display = showLaunch ? "inline-flex" : "none";
    if (launchSep) launchSep.style.display = showLaunch ? "inline" : "none";

    // Combine L5 + L6 cleanly into Row 5 (never show OK when L5 itself is still idle/unplugged)
    const l5 = byId.L5 || { layer: "L5", status: "idle", summary: "待检测", faults: [], metrics: {} };
    const l6 = byId.L6 || { layer: "L6", status: "skipped", summary: "", faults: [], metrics: {} };
    let combinedStatus = severityRank(l6.status) > severityRank(l5.status) ? l6.status : l5.status;
    if (l5.status === "idle" && l6.status === "ok") {
      combinedStatus = "idle";
    }
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

    const bpCard = document.getElementById("breakpointCard");
    if (pf) {
      const allFails = layers.flatMap((x) => (x.faults || []).filter((f) => f.severity === "fail"));
      const extraFails = Math.max(0, allFails.length - (pf.severity === "fail" ? 1 : 0));
      const lf = localizeFault(pf);
      bpCard.classList.remove("hidden");
      bpCard.classList.toggle("warn", pf.severity === "warn");
      document.getElementById("bpIcon").textContent = pf.severity === "warn" ? "!" : "✕";
      document.getElementById("bpTitle").textContent = lf.title;
      const suffix = extraFails > 0 ? ` (${t("more_blockers").replace("{n}", String(extraFails))})` : "";
      document.getElementById("bpMessage").textContent = lf.message + suffix;
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

    const btnEn = document.getElementById("btnEnableDirect");
    btnEn.disabled = state.busy || !report.can_enable_direct;
    btnEn.title = report.can_enable_direct ? "" : t("disabled_hint");
    document.getElementById("btnDisableDirect").disabled = state.busy;

    updateWizardState(byId, report);
    if (byId.L1 && byId.L1.metrics && byId.L1.metrics.physical_ethernets) {
      populateNicSelect(byId.L1.metrics.physical_ethernets);
    }
  }

  function updateWizardState(byId, report) {
    const l1 = byId.L1 || { status: "idle" };
    const l2 = byId.L2 || { status: "idle" };
    const l5 = byId.L5 || { status: "idle" };
    const l6 = byId.L6 || { status: "skipped", faults: [], metrics: {} };

    const presetId = getActivePresetId(report);
    const presetMeta = getActivePresetMeta(report);
    const steps = presetMeta.wizard_steps || DEFAULT_WIZARD_STEPS[presetId] || ["nic", "peer"];

    const hasNic = steps.includes("nic");
    const hasUwp = steps.includes("uwp");
    const hasClient = steps.includes("client");
    const hasPeer = steps.includes("peer");

    const elS1 = document.getElementById("wizStep1");
    const elS2 = document.getElementById("wizStep2");
    const elS3 = document.getElementById("wizStep3");
    const elS4 = document.getElementById("wizStep4");

    elS1.style.display = hasNic ? "flex" : "none";
    elS2.style.display = hasUwp ? "flex" : "none";
    elS3.style.display = hasClient ? "flex" : "none";
    elS4.style.display = hasPeer ? "block" : "none";

    // Dynamic sequential numbering (e.g. PS has 1,2,3; Switch has 1,2; Xbox has 1,2,3,4)
    let stepNum = 1;
    if (hasNic) document.getElementById("wizS1Title").textContent = `${stepNum++}. ${t("wiz_nic_title")}`;
    if (hasUwp) document.getElementById("wizS2Title").textContent = `${stepNum++}. ${t("wiz_uwp_title")}`;
    if (hasClient) document.getElementById("wizS3Title").textContent = `${stepNum++}. ${t("wiz_client_title")}`;
    if (hasPeer) document.getElementById("wizS4Title").textContent = `${stepNum++}. ${t("wiz_peer_title")}`;

    const s1Status = (l1.status === "ok" && l2.status === "ok") ? "ok" : (l1.status === "fail" ? "fail" : "warn");
    setBadge("wizS1Badge", s1Status);
    let hasWizTodo = (s1Status !== "ok");

    if (hasUwp) {
      const hasUwpMiss = (l6.faults || []).some((f) => f.code === "UWP_LOOPBACK_MISSING");
      setBadge("wizS2Badge", hasUwpMiss ? "warn" : "ok");
      if (hasUwpMiss) hasWizTodo = true;
    }

    if (hasClient) {
      const hasClientMiss = (l6.faults || []).some((f) =>
        f.code === "STREAM_CLIENT_MISSING" || f.code === "PS_CLIENT_MISSING" || f.code === "HANDHELD_CLIENT_MISSING"
      );
      setBadge("wizS3Badge", hasClientMiss ? "warn" : "ok");
      if (hasClientMiss) hasWizTodo = true;

      const s3Desc = document.getElementById("wizS3Desc");
      const m6 = l6.metrics || {};
      const btnDl = document.getElementById("btnWizDownload");
      if (btnDl) {
        btnDl.textContent = presetId === "xbox" ? t("wiz_s3_btn") : t("wiz_btn_open_release");
      }
      if (s3Desc) {
        if (presetId === "xbox") {
          if (m6.xstreaming_path) s3Desc.textContent = t("wiz_client_xstreaming");
          else if (m6.xbox_app_installed) s3Desc.textContent = t("wiz_client_xbox");
          else s3Desc.textContent = t("wiz_client_none");
        } else if (presetId === "playstation") {
          if (m6.chiaki_path) s3Desc.textContent = t("wiz_client_ps_chiaki");
          else if (m6.ps_remote_path) s3Desc.textContent = t("wiz_client_ps_remote");
          else s3Desc.textContent = t("wiz_client_ps_none");
        } else if (presetId === "handheld") {
          if (m6.moonlight_path) s3Desc.textContent = t("wiz_client_hh_moonlight");
          else if (m6.steam_path || m6.sunshine_path) s3Desc.textContent = t("wiz_client_hh_steam");
          else s3Desc.textContent = t("wiz_client_hh_none");
        }
      }
    }

    setBadge("wizS4Badge", l5.status === "ok" ? "ok" : "idle");

    const nmcliBtn = document.getElementById("btnWizCopyNmcli");
    if (nmcliBtn) {
      nmcliBtn.style.display = (presetId === "handheld" || presetId === "other") ? "inline-flex" : "none";
    }
    const tipLine = document.getElementById("wizTipLine");
    if (tipLine) {
      tipLine.textContent = t(`wiz_tip_${presetId}`);
    }

    const wizDot = document.getElementById("wizDot");
    if (wizDot) wizDot.classList.toggle("show", hasWizTodo);
  }

  function setBadge(id, sev) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `sev-text ${sev}`;
    el.textContent = sevLabel(sev);
  }

  let _lastDetailText = "";
  function openNodeDetail(title, layer) {
    document.getElementById("detailTitle").textContent = `${title} (${layer.layer})`;
    const faults = layer.faults || [];
    const lines = [];
    if (faults.length === 0) {
      lines.push(`${sevLabel(layer.status)} — ${describeLayerDetail(layer, null)}`);
    } else {
      faults.forEach((f) => {
        const lf = localizeFault(f);
        const tag = f.source === "extension" ? `[AI: ${f.ext_id}] ` : "";
        lines.push(`• ${tag}[${f.code}] ${lf.title} — ${lf.message}`);
      });
    }
    const m = layer.metrics || {};
    const metricEntries = Object.entries(m).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && v.length === 0));
    if (metricEntries.length > 0) {
      lines.push("");
      metricEntries.forEach(([k, v]) => {
        lines.push(`${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
      });
    }
    _lastDetailText = lines.join("\n");
    document.getElementById("detailBody").innerHTML = lines.map((x) => `<div>${escapeHtml(x)}</div>`).join("");
    document.getElementById("detailModal").classList.add("open");
  }

  function openTextModal(title, rawText) {
    document.getElementById("detailTitle").textContent = title;
    _lastDetailText = rawText;
    document.getElementById("detailBody").innerHTML = `<pre class="mono" style="margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;">${escapeHtml(rawText)}</pre>`;
    document.getElementById("detailModal").classList.add("open");
  }

  async function handleFixAction(fault) {
    if (fault.fix_action === "download-stream") {
      switchView("wizard");
      return;
    }
    if (fault.fix_action === "clear-netnat") {
      const names = (fault.details && fault.details.nat_names) ? fault.details.nat_names.join(", ") : "";
      document.getElementById("confirmTitle").textContent = t("confirm_netnat_title");
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
    if (!api || state.busy) return;
    setBusy(true);
    showToast(t("toast_working"));
    try {
      const res = await api.run_action(action, opts || {});
      showToast(res.message || (res.ok ? "OK" : res.code));
      await refreshAll(true);
    } catch (err) {
      showToast(String(err));
    } finally {
      setBusy(false);
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

  function renderToolkits(items) {
    state.toolkits = items || [];
    const list = document.getElementById("toolkitList");
    const countTag = document.getElementById("toolkitCountTag");
    if (countTag) countTag.textContent = String(state.toolkits.length);
    if (!list) return;
    list.innerHTML = "";
    const isZh = state.lang === "zh-CN" || state.lang === "zh-TW";
    state.toolkits.forEach((tool) => {
      const row = document.createElement("div");
      row.className = "list-row";
      const scopeLabel = tool.scope === "common" ? t("ai_tool_common") : tool.scope.toUpperCase();
      const title = isZh ? (tool.name_zh || tool.name_en || tool.id) : (tool.name_en || tool.name_zh || tool.id);
      const desc = isZh ? (tool.description_zh || tool.description_en || "") : (tool.description_en || tool.description_zh || "");
      row.innerHTML = `
        <div class="list-row-col">
          <div class="row-head">
            <span class="ext-tag">${escapeHtml(scopeLabel)}</span>
            <span>${escapeHtml(title)}</span>
          </div>
          <div class="row-desc">${escapeHtml(desc)}</div>
        </div>
        <div class="row-actions">
          <button class="btn btn-sm" data-tool-run="${escapeHtml(tool.id)}">${escapeHtml(t("ai_tool_run"))}</button>
        </div>
      `;
      list.appendChild(row);
    });

    list.querySelectorAll("[data-tool-run]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-tool-run");
        const api = getApi();
        if (!api || !api.run_toolkit) return;
        btn.disabled = true;
        showToast(t("ai_tool_running"));
        try {
          const res = await api.run_toolkit(id);
          openTextModal(`${t("ai_tool_result_title")} — ${id}`, JSON.stringify(res, null, 2));
        } catch (err) {
          showToast(String(err));
        } finally {
          btn.disabled = false;
        }
      });
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

  function renderOnboardingModal() {
    document.querySelectorAll("[data-onb-lang]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-onb-lang") === state.onbSelectedLang);
    });
    const grid = document.getElementById("onbPresetGrid");
    if (!grid) return;
    grid.innerHTML = "";
    PRESET_ORDER.forEach((pid) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "preset-card" + (pid === "other" ? " span-2" : "") + (state.onbSelectedPreset === pid ? " active" : "");
      card.setAttribute("data-onb-preset", pid);
      card.innerHTML = `
        <div class="preset-card-title">${escapeHtml(t(`preset_${pid}_name`))}</div>
        <div class="preset-card-desc">${escapeHtml(t(`preset_${pid}_desc`))}</div>
      `;
      card.addEventListener("click", () => {
        state.onbSelectedPreset = pid;
        renderOnboardingModal();
      });
      grid.appendChild(card);
    });
  }

  function openOnboardingModal() {
    const cfg = (state.appState && state.appState.config) || {};
    state.onbSelectedLang = state.lang || cfg.language || "zh-CN";
    state.onbSelectedPreset = getActivePresetId(state.report) || cfg.active_preset || "xbox";
    renderOnboardingModal();
    document.getElementById("onboardingModal").classList.add("open");
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
    const activeId = (state.appState && state.appState.active_preset && state.appState.active_preset.id)
      || cfg.active_preset
      || cfg.console_profile
      || "xbox";
    document.getElementById("setTheme").value = state.theme;
    document.getElementById("setLanguage").value = state.lang;
    document.getElementById("setProfile").value = activeId;
    document.getElementById("setHostIp").value = cfg.host_ip || "192.168.50.2";
    document.getElementById("setPeerIp").value = cfg.peer_ip || "192.168.50.1";
    document.getElementById("setDns").value = (cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(", ");
    document.getElementById("sbSubnet").textContent = `${cfg.host_ip} → ${cfg.peer_ip}`;
    document.getElementById("cardPeerIp").textContent = cfg.peer_ip;
    document.getElementById("cardMask").textContent = cfg.subnet_mask || "255.255.255.0";
    document.getElementById("cardHostIp").textContent = cfg.host_ip;
    document.getElementById("cardDns").textContent = (cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" / ");
  }

  async function refreshAll(silent) {
    const api = getApi();
    if (!api) return;
    setBusy(true);
    try {
      const st = await api.get_state();
      state.appState = st;
      document.getElementById("versionLabel").textContent = "v" + st.app_version;
      syncConfigDisplay(st.config);
      renderBridgeState(st.bridge);
      const [rep, exts, tools] = await Promise.all([
        api.diagnose(),
        api.list_extensions(),
        api.list_toolkits ? api.list_toolkits() : Promise.resolve([]),
      ]);
      renderReport(rep);
      renderExtensions(exts);
      renderToolkits(tools);

      if (st.show_onboarding && !state.onboardingDismissed) {
        openOnboardingModal();
      }
      if (st.preview_view && !refreshAll._previewApplied) {
        refreshAll._previewApplied = true;
        switchView(st.preview_view);
      }
      if (!silent) showToast(t("toast_refreshed"));
    } finally {
      setBusy(false);
    }
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

    document.getElementById("sbProfile").addEventListener("click", () => switchView("settings"));

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
    document.getElementById("btnWizTestPeer").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const box = document.getElementById("peerPingResult");
      box.className = "peer-ping-result show";
      box.textContent = t("ping_testing");
      const res = await api.ping_peer_now();
      if (res.ok) {
        const rttTxt = typeof res.rtt_ms === "number" ? ` (${res.rtt_ms.toFixed(1)} ms)` : "";
        box.className = "peer-ping-result show ok";
        box.textContent = `● ${res.peer_ip} ${t("ping_ok")}${rttTxt}`;
        setBadge("wizS4Badge", "ok");
      } else {
        box.className = "peer-ping-result show fail";
        box.textContent = `✕ ${res.peer_ip} ${t("ping_fail")}`;
      }
    });
    document.getElementById("btnWizCopyIp").addEventListener("click", async () => {
      const cfg = (state.appState && state.appState.config) || {};
      const txt = `IP: ${cfg.peer_ip || "192.168.50.1"}\nMask: ${cfg.subnet_mask || "255.255.255.0"}\nGateway: ${cfg.host_ip || "192.168.50.2"}\nDNS: ${(cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" / ")}`;
      await copyText(txt);
      showToast(t("toast_copied"));
    });
    document.getElementById("btnWizCopyNmcli").addEventListener("click", async () => {
      const api = getApi();
      const cfg = (state.appState && state.appState.config) || {};
      let cmd = `sudo nmcli con mod "Wired connection 1" ipv4.method manual ipv4.addresses ${cfg.peer_ip || "192.168.50.30"}/${cfg.prefix_length || 24} ipv4.gateway ${cfg.host_ip || "192.168.50.2"} ipv4.dns "${(cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" ")}" && sudo nmcli con up "Wired connection 1"`;
      if (api && api.run_toolkit) {
        try {
          const r = await api.run_toolkit("handheld/emit-linux-nmcli-script");
          if (r && r.ok && r.data && r.data.nmcli_setup) cmd = r.data.nmcli_setup;
        } catch (_) {}
      }
      await copyText(cmd);
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
    document.getElementById("btnRerunOnboarding").addEventListener("click", () => {
      state.onboardingDismissed = false;
      openOnboardingModal();
    });
    document.getElementById("btnResetNic").addEventListener("click", () => {
      document.getElementById("confirmTitle").textContent = t("confirm_reset_title");
      document.getElementById("confirmBody").textContent = t("confirm_reset_body");
      state.confirmCallback = () => runAction("reset-nic", {});
      document.getElementById("confirmModal").classList.add("open");
    });

    // Startup Onboarding modal language pills + confirm
    document.querySelectorAll("[data-onb-lang]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const lang = btn.getAttribute("data-onb-lang");
        state.onbSelectedLang = lang;
        state.lang = lang;
        applyI18n();
        renderOnboardingModal();
        if (state.report) renderReport(state.report);
      });
    });
    document.getElementById("btnOnbStart").addEventListener("click", async () => {
      const api = getApi();
      state.onboardingDismissed = true;
      document.getElementById("onboardingModal").classList.remove("open");
      if (api && api.complete_onboarding) {
        await api.complete_onboarding(state.onbSelectedLang, state.onbSelectedPreset);
        await refreshAll(true);
      }
    });

    // Live instant preview + auto-save on Theme / Language / Preset switch
    document.getElementById("setTheme").addEventListener("change", async (e) => {
      applyTheme(e.target.value);
      const api = getApi();
      if (api) await api.save_settings({ theme: e.target.value });
    });
    document.getElementById("setLanguage").addEventListener("change", async (e) => {
      state.lang = e.target.value;
      applyI18n();
      const api = getApi();
      if (api) {
        await api.save_settings({ language: e.target.value });
        if (state.report) renderReport(state.report);
        if (state.toolkits.length) renderToolkits(state.toolkits);
      }
    });
    document.getElementById("setProfile").addEventListener("change", async (e) => {
      const api = getApi();
      if (!api || !api.switch_preset) return;
      const res = await api.switch_preset(e.target.value);
      if (res && res.ok) {
        await refreshAll(true);
      }
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

    document.getElementById("btnSaveSettings").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const patch = {
        theme: document.getElementById("setTheme").value,
        language: document.getElementById("setLanguage").value,
        active_preset: document.getElementById("setProfile").value,
        ethernet_interface: document.getElementById("setEth").value,
        host_ip: document.getElementById("setHostIp").value.trim(),
        peer_ip: document.getElementById("setPeerIp").value.trim(),
        dns_servers: document.getElementById("setDns").value.trim(),
      };
      const res = await api.save_settings(patch);
      if (!res.ok) {
        showToast(res.message || res.code);
        return;
      }
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
    document.getElementById("btnCopyDetail").addEventListener("click", async () => {
      if (_lastDetailText) {
        await copyText(_lastDetailText);
        showToast(t("toast_copied"));
      }
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
    renderLoadingSkeleton();
  });

  window.addEventListener("pywebviewready", () => {
    refreshAll(true);
  });
})();
