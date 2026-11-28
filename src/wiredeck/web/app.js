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
    transport: "wired",
    wirelessSubmode: "hotspot",
    appState: null,
    report: null,
    wirelessReport: null,
    hotspotCreds: null,
    showHotspotPass: false,
    extensions: [],
    toolkits: [],
    bridgeToken: "",
    activeBridgeJob: null,
    confirmCallback: null,
    pollTimer: null,
    busy: false,
    wiredStale: true,
    wirelessStale: true,
    onbSelectedLang: "zh-CN",
    onbSelectedTransport: "wired",
    onbSelectedPreset: "xbox",
    onboardingDismissed: false,
    guideStepIdx: 0,
    guideManualChecks: {},
    guideLastFlowKey: "",
    guideUserPicked: false,
    guidePingResult: null,
    guideShortcutCreatedKey: "",
    guideRestoreShortcutCreated: false,
    pluginsInstalled: [],
    pluginsCatalog: null,
    pluginsTab: "catalog",
    pluginsCategory: "all",
    activePluginConfig: null,
    pluginBadges: [],
    recorderState: null,
    recorderTab: "control",
    recorderPollTimer: null,
    activeRenameClip: null,
    systemHealth: null,
    updateReport: null,
    uninstallScan: null,
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
    document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const k = el.getAttribute("data-i18n-placeholder");
      if (k) el.setAttribute("placeholder", t(k));
    });
    document.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const k = el.getAttribute("data-i18n-title");
      if (k) el.setAttribute("title", t(k));
    });
    const refreshBtn = document.getElementById("btnRefresh");
    if (refreshBtn) refreshBtn.title = t("btn_refresh");
    const wRefreshBtn = document.getElementById("btnWRefresh");
    if (wRefreshBtn) wRefreshBtn.title = t("btn_refresh");
    if (document.getElementById("onboardingModal").classList.contains("open")) {
      renderOnboardingModal();
    }
    if (state.guideSteps && state.guideSteps.length) {
      const activeRep = state.transport === "wireless" ? state.wirelessReport : state.report;
      if (activeRep) updateWizardState(layerMapOf(activeRep), activeRep);
    }
    if (document.getElementById("viewPlugins") && document.getElementById("viewPlugins").classList.contains("active")) {
      renderPluginsView();
    }
    if (document.getElementById("updateModal") && document.getElementById("updateModal").classList.contains("open") && state.updateReport) {
      renderUpdateModal(state.updateReport);
    }
    if (document.getElementById("uninstallModal") && document.getElementById("uninstallModal").classList.contains("open") && state.uninstallScan) {
      renderUninstallModal(state.uninstallScan);
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

  async function switchView(name, skipTransportPersist) {
    document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
    const target = document.getElementById("view" + name.charAt(0).toUpperCase() + name.slice(1));
    if (target) target.classList.add("active");
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-nav") === name);
    });

    const api = getApi();
    if (!skipTransportPersist && (name === "home" || name === "wireless")) {
      const nextT = name === "wireless" ? "wireless" : "wired";
      if (state.transport !== nextT) {
        state.transport = nextT;
        if (api && api.set_transport) {
          await api.set_transport(nextT);
        }
        if (state.appState && state.appState.config) {
          updateStatusBar(state.appState.config);
        }
        if (nextT === "wireless" && state.wirelessReport) {
          renderWirelessReport(state.wirelessReport);
        } else if (nextT === "wired" && state.report) {
          renderReport(state.report);
        }
        const rep = nextT === "wireless" ? state.wirelessReport : state.report;
        if (rep) updateWizardState(layerMapOf(rep), rep);
        if ((nextT === "wireless" && (!state.wirelessReport || state.wirelessStale))
          || (nextT === "wired" && (!state.report || state.wiredStale))) {
          await refreshAll(true);
        }
      }
    }

    if (name === "plugins") {
      renderPluginsView();
      if (!state.pluginsCatalog && api && api.fetch_plugin_catalog) {
        await loadPluginsData(true);
      }
    }

    if (name === "recorder") {
      renderRecorderView();
      if (!state.recorderState && api && api.get_recorder_state) {
        await loadRecorderData();
      }
      syncRecorderTelemetryPolling();
    } else {
      syncRecorderTelemetryPolling();
    }
  }

  function getApi() {
    return window.pywebview && window.pywebview.api;
  }

  function layerMapOf(report) {
    const byId = {};
    ((report && report.layers) || []).forEach((l) => { byId[l.layer] = l; });
    return byId;
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
      VPN_TUN_ACTIVE: ["VPN 接管預設路由", "請先暫時中斷 VPN 連線再進行直連或無線串流"],
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
      WIFI_NOT_FOUND: ["未偵測到無線網卡", "請確認無線網卡未被停用或插好 USB Wi-Fi 轉接器"],
      WIFI_DISABLED: ["無線網卡已停用", "請在系統設定啟用無線介面卡或開啟 Wi-Fi 開關"],
      WIFI_POWERSAVE_ENABLED: ["無線節能/封包合併開啟中", "偵測到 MIMO 節能或封包合併，建議開啟「無線防抖」關閉"],
      WIFI_DISCONNECTED: ["Wi-Fi 尚未連線", "同 Wi-Fi 模式需要電腦與裝置連入同一無線路由器"],
      WIFI_BAND_24GHZ: ["目前連在 2.4 GHz 頻段", "2.4 GHz 亦可正常串流；建議碼率 10–15 Mbps 並優先有線手把避開藍牙干擾"],
      WIFI_SIGNAL_WEAK: ["無線訊號強度偏弱", "建議靠近路由器或開啟電腦熱點直連"],
      WIFI_LINK_SLOW: ["無線協商速率偏低", "高碼率串流建議使用 5 GHz，或於 2.4 GHz 適當降低碼率"],
      WIRED_NAT_CONFLICT: ["有線 WinNAT 運作中", "有線分享與行動熱點互斥，請先關閉有線直連"],
      HOTSPOT_UNSUPPORTED: ["目前無法開啟行動熱點", "請確認電腦已連網且系統原則允許開啟熱點"],
      HOTSPOT_BAND_24GHZ: ["熱點正運作於 2.4 GHz", "本機支援 5 GHz 熱點，點右側切換或將熱點頻段設為 2.4G"],
      LAN_AP_ISOLATION_SUSPECTED: ["疑似路由器/校園網 AP 隔離", "閘道正常但同網段裝置無法直通，建議改用上方「5GHz 電腦熱點」"],
      WIFI_BG_SCAN_ACTIVE: ["系統週期背景掃描開啟中", "Windows 定期掃描 Wi-Fi 易引發頓挫，點一鍵開啟防抖"],
      WIFI_JITTER_HIGH: ["無線鏈路抖動偏高", "建議改用 5 GHz 熱點直連或更換較乾淨的頻道"],
      WIFI_PEER_UNREACHABLE: ["無線裝置暫未回應", "請確認目標裝置已連入目前無線並已開機"],
    },
    "en": {
      NIC_NOT_FOUND: ["Wired NIC Not Found", "Check Ethernet hardware or plug in a USB wired adapter"],
      NIC_PCIE_PARENT_DISABLED: ["PCIe Port Disabled", "Parent PCIe root port is disabled; click Fix Now to wake it"],
      NIC_DISABLED: ["Wired NIC Disabled", "Wired adapter is disabled; click Fix Now to enable it"],
      NIC_POWERSAVE_ENABLED: ["NIC Power-Save On", "EEE/Green Ethernet can cause stream stutter; click Fix Now"],
      CABLE_UNPLUGGED: ["Cable Unplugged", "Plug Ethernet cable firmly into laptop and powered-on device"],
      LINK_DEGRADED_100M: ["100 Mbps Link", "Negotiated 100 Mbps; use a Cat5e/Cat6 Gigabit cable"],
      NIC_HW_ERRORS: ["Cable Packet Errors", "High hardware packet errors; reseat or replace Ethernet cable"],
      VPN_TUN_ACTIVE: ["VPN Taking Over Route", "Disconnect VPN first before wired direct or wireless streaming"],
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
      WIFI_NOT_FOUND: ["Wi-Fi Adapter Not Found", "Check laptop Wi-Fi hardware or plug in a USB Wi-Fi dongle"],
      WIFI_DISABLED: ["Wi-Fi Adapter Disabled", "Enable Wi-Fi adapter in Windows settings or toggle Wi-Fi ON"],
      WIFI_POWERSAVE_ENABLED: ["Wi-Fi Power-Save On", "MIMO SMPS / Packet Coalescing can stutter; click Fix Now to tune"],
      WIFI_DISCONNECTED: ["Wi-Fi Not Connected", "Connect both PC and device to the same Wi-Fi network"],
      WIFI_BAND_24GHZ: ["Connected on 2.4 GHz", "2.4 GHz streaming works too: keep bitrate ~10-15 Mbps and wire Bluetooth pads"],
      WIFI_SIGNAL_WEAK: ["Weak Wi-Fi Signal", "Move closer to router or start PC Hotspot direct link"],
      WIFI_LINK_SLOW: ["Low Wi-Fi PHY Rate", "Use 5 GHz for high bitrate, or lower bitrate on 2.4 GHz"],
      WIRED_NAT_CONFLICT: ["Wired WinNAT Active", "Wired WireDeckNat conflicts with Mobile Hotspot; stop Wired first"],
      HOTSPOT_UNSUPPORTED: ["Mobile Hotspot Unavailable", "Ensure internet connection exists and hotspot policy allows sharing"],
      HOTSPOT_BAND_24GHZ: ["Hotspot Running 2.4 GHz", "5 GHz supported; click Fix Now or switch band selector to 2.4G"],
      LAN_AP_ISOLATION_SUSPECTED: ["AP Isolation Suspected", "Gateway responds but peer is blocked; use 5 GHz PC Hotspot instead"],
      WIFI_BG_SCAN_ACTIVE: ["Background Wi-Fi Scan On", "Periodic Windows scans cause lag spikes; click Anti-Stutter to pause"],
      WIFI_JITTER_HIGH: ["High Wi-Fi Jitter", "High RTT variance or packet loss; switch to 5 GHz Hotspot or cleaner channel"],
      WIFI_PEER_UNREACHABLE: ["Wireless Device Unreachable", "Verify device is powered on and connected to current Wi-Fi/Hotspot"],
    },
    "ja": {
      NIC_NOT_FOUND: ["有線 LAN が見つかりません", "内蔵 LAN または USB 有線アダプターを確認してください"],
      NIC_PCIE_PARENT_DISABLED: ["PCIe 上位ポートが無効", "親 PCIe ルートポートが停止中です。「ワンクリック修復」で復帰します"],
      NIC_DISABLED: ["有線 LAN が無効", "有線アダプターが無効です。「ワンクリック修復」で有効化できます"],
      NIC_POWERSAVE_ENABLED: ["NIC 省電力機能が有効", "EEE 省電力は配信カクつきの原因になります。「ワンクリック修復」で解除"],
      CABLE_UNPLUGGED: ["LAN ケーブル未接続", "ケーブルが PC と電源オンの機器にしっかり挿さっているか確認してください"],
      LINK_DEGRADED_100M: ["100 Mbps リンクに低下", "Cat5e / Cat6 以上のギガビット対応ケーブルをご使用ください"],
      NIC_HW_ERRORS: ["ケーブル通信エラー多数", "ハードウェアエラーが増加しています。ケーブルを交換または差し直してください"],
      VPN_TUN_ACTIVE: ["VPN がルートを専有中", "直結や無線配信の前に一度 VPN を切断してください"],
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
      WIFI_NOT_FOUND: ["無線 LAN が見つかりません", "Wi-Fi アダプターが有効か USB 無線子機が接続されているか確認してください"],
      WIFI_DISABLED: ["無線 LAN が無効です", "Windows 設定から Wi-Fi をオンにしてください"],
      WIFI_POWERSAVE_ENABLED: ["無線省電力 / 結合が有効", "MIMO 省電力やパケット結合が遅延の原因になります。「無線安定化」で解除できます"],
      WIFI_DISCONNECTED: ["Wi-Fi 未接続", "PC と端末を同じ Wi-Fi に接続してください"],
      WIFI_BAND_24GHZ: ["2.4 GHz 帯で接続中", "2.4 GHz でも配信可能です。ビットレート 10〜15 Mbps 目安＋有線コントローラ推奨"],
      WIFI_SIGNAL_WEAK: ["Wi-Fi 電波が弱い", "ルーターに近づくか、PC ホットスポット直結をご利用ください"],
      WIFI_LINK_SLOW: ["リンク速度が低下中", "高画質は 5 GHz 推奨、2.4 GHz の場合はビットレートを控えめにしてください"],
      WIRED_NAT_CONFLICT: ["有線 WinNAT が稼働中", "ホットスポットと競合するため、先に有線直結を停止してください"],
      HOTSPOT_UNSUPPORTED: ["モバイルホットスポット不可", "PC のネット接続と共有ポリシーを確認してください"],
      HOTSPOT_BAND_24GHZ: ["2.4 GHz ホットスポット稼働中", "5 GHz に対応しています。修復で 5 GHz 化、または帯域設定を 2.4G へ変更"],
      LAN_AP_ISOLATION_SUSPECTED: ["AP 分離（プライバシー保護）疑い", "ルーター側で端末間通信が遮断されています。5 GHz PC ホットスポットを推奨します"],
      WIFI_BG_SCAN_ACTIVE: ["バックグラウンドスキャン有効", "Windows の定期 Wi-Fi スキャンがラグの原因になります。無線安定化で一時停止できます"],
      WIFI_JITTER_HIGH: ["無線のジッターが高い", "遅延ばらつきが大きいため、5 GHz ホットスポットまたは空いたチャンネルをご使用ください"],
      WIFI_PEER_UNREACHABLE: ["無線端末から応答なし", "機器の電源と Wi-Fi/ホットスポット接続を確認してください"],
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
    if (report.transport === "wireless") {
      if (report.mode === "hotspot") return t("w_headline_hotspot");
      if (report.mode === "optimized") return t("w_headline_optimized");
      return t("w_headline_normal");
    }
    if (report.mode === "direct") return t("headline_direct");
    return t("headline_normal");
  }

  function setBusy(busy) {
    state.busy = busy;
    ["homeProgress", "wProgress"].forEach((id) => {
      const p = document.getElementById(id);
      if (p) p.classList.toggle("active", busy);
    });
    const btnRef = document.getElementById("btnRefresh");
    const btnEn = document.getElementById("btnEnableDirect");
    const btnDis = document.getElementById("btnDisableDirect");
    if (btnRef) btnRef.disabled = busy;
    if (btnDis) btnDis.disabled = busy;
    if (btnEn) btnEn.disabled = busy || !(state.report && state.report.can_enable_direct);

    const wRef = document.getElementById("btnWRefresh");
    const wP = document.getElementById("btnWPrimary");
    const wS = document.getElementById("btnWSecondary");
    if (wRef) wRef.disabled = busy;
    if (wP) wP.disabled = busy;
    if (wS) wS.disabled = busy;

    document.querySelectorAll("#guideActions button").forEach((btn) => {
      btn.disabled = busy || btn.dataset.stepDisabled === "1";
    });
  }

  function renderLoadingSkeleton(tableId, keys) {
    const table = document.getElementById(tableId);
    if (!table || table.children.length > 0) return;
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
    // Wired layers
    if (layer.layer === "L1") {
      return m.selected_ethernet ? `${m.selected_ethernet} · ${t("detail_ps_off")}` : (layer.summary || "—");
    }
    if (layer.layer === "L2") {
      if (layer.status === "idle") return t("detail_wait_nic");
      return layer.summary ? `${layer.summary} ${t("detail_full_duplex")}` : "—";
    }
    if (layer.layer === "L3") return t("detail_no_conflict");
    if (layer.layer === "L4") return m.nat_active ? t("detail_nat_on") : t("detail_nat_standby");

    // Wireless layers
    if (layer.layer === "W1") {
      const adapterName = m.description || m.selected_wifi || layer.summary || "—";
      const optSuffix = (m.powersave_keywords || []).length === 0 ? t("w_detail_opt_on") : t("w_detail_opt_off");
      return `${adapterName} · ${optSuffix}`;
    }
    if (layer.layer === "W2") {
      if (layer.status === "idle") return layer.summary || t("detail_wait_nic");
      const parts = [];
      if (m.band) parts.push(m.band);
      if (m.channel) parts.push(`Ch${m.channel}`);
      if (m.rssi_dbm !== null && m.rssi_dbm !== undefined) parts.push(`${m.rssi_dbm} dBm`);
      if (m.rx_rate_mbps) parts.push(`${Math.round(m.rx_rate_mbps)} Mbps`);
      return parts.join(" · ") || layer.summary || "—";
    }
    if (layer.layer === "W3") {
      if (m.submode === "hotspot") {
        if (m.hotspot_state === "On") {
          return `${t("w_detail_hotspot_on")} (${m.hotspot_band}) · ${m.hotspot_client_count || 0} ${t("w_clients")}`;
        }
        return t("w_detail_hotspot_off");
      }
      return t("w_detail_lan_ok");
    }
    if (layer.layer === "W4") {
      return m.autoconfig_enabled === false ? t("w_detail_scan_paused") : t("w_detail_scan_active");
    }
    if (layer.layer === "L5/L6" || layer.layer === "W5/W6") {
      const isWireless = layer.layer === "W5/W6";
      if (layer.status === "idle") {
        const appTail = combinedL6 && combinedL6.status === "ok" ? ` · ${t("detail_client_ready")}` : "";
        return (isWireless ? t("w_detail_no_peer") : t("detail_wait_cable")) + appTail;
      }
      const parts = [];
      const pip = m.peer_ip || m.target_peer_ip;
      if (pip) parts.push(pip);
      if (typeof m.peer_rtt_ms === "number") parts.push(`${m.peer_rtt_ms.toFixed(1)} ms`);
      if (isWireless && typeof m.jitter_ms === "number") parts.push(`jitter ${m.jitter_ms.toFixed(1)} ms`);
      if (combinedL6 && combinedL6.status === "ok") parts.push(t("detail_client_ready"));
      if (combinedL6 && combinedL6.status === "skipped") parts.push(t("detail_no_client_preset"));
      return parts.join(" · ") || "—";
    }
    return layer.summary || "—";
  }

  function renderDiagnosticRows(tableId, rows, emptyHandler) {
    const table = document.getElementById(tableId);
    if (!table) return;
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

      rowEl.addEventListener("click", () => emptyHandler(t(r.key), l));
      table.appendChild(rowEl);
    });
  }

  function renderInfoBar(cardId, iconId, titleId, msgId, pillId, btnId, report, layers) {
    const bpCard = document.getElementById(cardId);
    const pf = report.primary_fault;
    if (!bpCard) return;
    if (pf) {
      const allFails = layers.flatMap((x) => (x.faults || []).filter((f) => f.severity === "fail"));
      const extraFails = Math.max(0, allFails.length - (pf.severity === "fail" ? 1 : 0));
      const lf = localizeFault(pf);
      bpCard.classList.remove("hidden");
      bpCard.classList.toggle("warn", pf.severity === "warn");
      document.getElementById(iconId).textContent = pf.severity === "warn" ? "!" : "✕";
      document.getElementById(titleId).textContent = lf.title;
      const suffix = extraFails > 0 ? ` (${t("more_blockers").replace("{n}", String(extraFails))})` : "";
      document.getElementById(msgId).textContent = lf.message + suffix;
      document.getElementById(pillId).style.display = pf.source === "extension" ? "inline-flex" : "none";

      const fixBtn = document.getElementById(btnId);
      if (pf.fix_action) {
        fixBtn.style.display = "inline-flex";
        if (pf.fix_action === "clear-netnat") fixBtn.textContent = t("btn_confirm_clear");
        else if (pf.fix_action === "download-stream") fixBtn.textContent = t("btn_download_stream");
        else fixBtn.textContent = t("btn_fix_now");
        fixBtn.onclick = () => handleFixAction(pf);
      } else {
        fixBtn.style.display = "none";
      }
    } else {
      bpCard.classList.add("hidden");
    }
  }

  function combinePeerAndApp(baseLayer, appLayer, combinedId, titleKey) {
    let combinedStatus = severityRank(appLayer.status) > severityRank(baseLayer.status) ? appLayer.status : baseLayer.status;
    if (baseLayer.status === "idle" && appLayer.status === "ok") {
      combinedStatus = "idle";
    }
    const combinedSummary = severityRank(baseLayer.status) >= severityRank(appLayer.status) ? baseLayer.summary : appLayer.summary;
    return {
      key: titleKey,
      l6Ref: appLayer,
      layerObj: {
        layer: combinedId,
        name: t(titleKey),
        status: combinedStatus,
        summary: combinedSummary,
        faults: [...(baseLayer.faults || []), ...(appLayer.faults || [])],
        metrics: Object.assign({}, baseLayer.metrics, appLayer.metrics),
      },
    };
  }

  function updateStatusBar(cfg) {
    const presetId = getActivePresetId(state.report);
    const presetMeta = getActivePresetMeta(state.report);
    document.getElementById("sbProfile").textContent = t(`preset_${presetId}_name`);

    const sbSub = document.getElementById("sbSubnet");
    if (state.transport === "wireless") {
      const subTxt = state.wirelessSubmode === "lan" ? t("sb_lan") : t("sb_hotspot");
      const lanSsid = state.wirelessReport && state.wirelessReport.layers && (state.wirelessReport.layers.find((x) => x.layer === "W2")?.metrics?.ssid);
      const ssid = state.wirelessSubmode === "lan"
        ? (lanSsid || "")
        : ((state.hotspotCreds && state.hotspotCreds.ssid) || lanSsid || "");
      sbSub.textContent = ssid ? `${subTxt} · ${ssid}` : subTxt;
    } else {
      sbSub.textContent = `${cfg.host_ip || "192.168.50.2"} → ${cfg.peer_ip || "192.168.50.1"}`;
    }

    const launchBtn = document.getElementById("btnLaunchStream");
    const launchSep = document.getElementById("sbLaunchSep");
    const showLaunch = Boolean(presetMeta.uses_stream_client);
    if (launchBtn) launchBtn.style.display = showLaunch ? "inline-flex" : "none";
    if (launchSep) launchSep.style.display = showLaunch ? "inline" : "none";
  }

  function renderReport(report) {
    state.report = report;
    const layers = report.layers || [];
    const byId = layerMapOf(report);

    if (state.transport === "wired") {
      document.getElementById("modeLabel").textContent = t("mode_" + (report.mode || "normal"));
    }

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

    const l5 = byId.L5 || { layer: "L5", status: "idle", summary: "", faults: [], metrics: {} };
    const l6 = byId.L6 || { layer: "L6", status: "skipped", summary: "", faults: [], metrics: {} };
    const rows = [
      { key: "node_nic", layerObj: byId.L1 },
      { key: "node_cable", layerObj: byId.L2 },
      { key: "node_gate", layerObj: byId.L3 },
      { key: "node_share", layerObj: byId.L4 },
      combinePeerAndApp(l5, l6, "L5/L6", "node_console"),
    ];
    renderDiagnosticRows("diagTable", rows, openNodeDetail);
    renderInfoBar("breakpointCard", "bpIcon", "bpTitle", "bpMessage", "bpExtPill", "bpFixBtn", report, layers);

    const btnEn = document.getElementById("btnEnableDirect");
    btnEn.disabled = state.busy || !report.can_enable_direct;
    btnEn.title = report.can_enable_direct ? "" : t("disabled_hint");
    document.getElementById("btnDisableDirect").disabled = state.busy;

    if (byId.L1 && byId.L1.metrics && byId.L1.metrics.physical_ethernets) {
      populateSelect("setEth", byId.L1.metrics.physical_ethernets, (state.appState && state.appState.config && state.appState.config.ethernet_interface) || "");
    }
    if (state.transport === "wired") {
      updateWizardState(byId, report);
      updateStatusBar((state.appState && state.appState.config) || {});
    }
  }

  function renderWirelessReport(report) {
    state.wirelessReport = report;
    const layers = report.layers || [];
    const byId = layerMapOf(report);
    const w1 = byId.W1 || { status: "idle", metrics: {} };
    const w2 = byId.W2 || { status: "idle", metrics: {} };
    const w3 = byId.W3 || { status: "idle", metrics: {} };
    const w4 = byId.W4 || { status: "idle", metrics: {} };
    const w5 = byId.W5 || { status: "idle", faults: [], metrics: {} };
    const w6 = byId.W6 || { status: "skipped", faults: [], metrics: {} };

    const hsOn = Boolean(w3.metrics && w3.metrics.hotspot_state === "On");
    const antiOn = Boolean(w4.metrics && w4.metrics.anti_stutter_active);

    if (state.transport === "wireless") {
      const mKey = report.mode === "hotspot" ? "w_mode_hotspot" : (report.mode === "optimized" ? "w_mode_optimized" : ("mode_" + (report.mode === "blocked" ? "blocked" : "normal")));
      document.getElementById("modeLabel").textContent = t(mKey);
    }

    const dot = document.getElementById("wHeadlineDot");
    const pf = report.primary_fault;
    dot.className = "status-led " + (pf ? pf.severity : (hsOn || antiOn ? "ok" : "idle"));
    document.getElementById("wHeadlineText").textContent = localizeHeadline(report);

    const metaParts = [];
    const m1 = w1.metrics || {};
    const m2 = w2.metrics || {};
    if (m1.description || m1.selected_wifi) metaParts.push(m1.description || m1.selected_wifi);
    if (m2.connected) {
      if (m2.band) metaParts.push(m2.band + (m2.channel ? ` Ch${m2.channel}` : ""));
      if (m2.rssi_dbm !== null && m2.rssi_dbm !== undefined) metaParts.push(`${m2.rssi_dbm} dBm`);
    }
    if (hsOn) {
      const hb = String((w3.metrics && w3.metrics.hotspot_band) || "");
      const hbLbl = /five/i.test(hb) ? "5 GHz" : ((/two|2\.4/i.test(hb)) ? "2.4 GHz" : hb);
      metaParts.push(hbLbl ? `${t("w_detail_hotspot_on")} (${hbLbl})` : t("w_detail_hotspot_on"));
    } else if (antiOn) {
      metaParts.push(t("w_detail_opt_on"));
    }
    document.getElementById("wCtrlMeta").textContent = metaParts.join(" · ") || "—";

    // Submode toggle state
    document.getElementById("btnSubHotspot").classList.toggle("active", state.wirelessSubmode === "hotspot");
    document.getElementById("btnSubLan").classList.toggle("active", state.wirelessSubmode === "lan");
    document.getElementById("wHotspotStrip").style.display = state.wirelessSubmode === "hotspot" ? "flex" : "none";
    document.getElementById("wLanStrip").style.display = state.wirelessSubmode === "lan" ? "flex" : "none";

    // Primary / Secondary control bar buttons
    const btnP = document.getElementById("btnWPrimary");
    const btnS = document.getElementById("btnWSecondary");
    if (state.wirelessSubmode === "hotspot") {
      if (hsOn) {
        btnP.textContent = t("w_btn_stop_hotspot");
        btnP.className = "btn";
        btnP.onclick = () => runAction("disable-hotspot", {});
      } else {
        btnP.textContent = t("w_btn_start_hotspot");
        btnP.className = "btn btn-primary";
        btnP.onclick = () => startHotspotWithCheck();
      }
      if (antiOn) {
        btnS.textContent = t("w_btn_restore");
        btnS.onclick = () => runAction("restore-wifi", {});
      } else {
        btnS.textContent = t("w_btn_optimize");
        btnS.onclick = () => runAction("optimize-wifi", {});
      }
    } else {
      if (antiOn) {
        btnP.textContent = t("w_btn_restore");
        btnP.className = "btn";
        btnP.onclick = () => runAction("restore-wifi", {});
      } else {
        btnP.textContent = t("w_btn_optimize");
        btnP.className = "btn btn-primary";
        btnP.onclick = () => runAction("optimize-wifi", {});
      }
      if (hsOn) {
        btnS.textContent = t("w_btn_stop_hotspot");
        btnS.onclick = () => runAction("disable-hotspot", {});
      } else {
        btnS.textContent = t("w_btn_start_hotspot");
        btnS.onclick = () => startHotspotWithCheck();
      }
    }

    // Populate hotspot strip
    const ssidVal = (state.hotspotCreds && state.hotspotCreds.ssid) || (w3.metrics && w3.metrics.hotspot_ssid) || "—";
    document.getElementById("wHotspotSsid").textContent = ssidVal;
    const passVal = (state.hotspotCreds && state.hotspotCreds.passphrase) || "";
    document.getElementById("wHotspotPass").textContent = state.showHotspotPass && passVal ? passVal : "••••••••";
    document.getElementById("btnWShowPass").textContent = t(state.showHotspotPass ? "w_hide" : "w_show");

    const clients = (w3.metrics && w3.metrics.hotspot_clients) || [];
    const clientPill = document.getElementById("wClientPill");
    if (clients.length > 0) {
      clientPill.textContent = `${t("w_clients")}: ${clients[0].ip}`;
    } else {
      clientPill.textContent = t("w_no_client");
    }

    // 5-row wireless table
    const rows = [
      { key: "w_node_nic", layerObj: w1 },
      { key: "w_node_rf", layerObj: w2 },
      { key: "w_node_net", layerObj: w3 },
      { key: "w_node_gate", layerObj: w4 },
      combinePeerAndApp(w5, w6, "W5/W6", "w_node_device"),
    ];
    renderDiagnosticRows("wDiagTable", rows, openNodeDetail);
    renderInfoBar("wBreakpointCard", "wBpIcon", "wBpTitle", "wBpMessage", "wBpExtPill", "wBpFixBtn", report, layers);

    if (m1.wifi_adapters) {
      populateSelect("setWifi", m1.wifi_adapters, (state.appState && state.appState.config && state.appState.config.wifi_interface) || "");
    }
    if (state.transport === "wireless") {
      updateWizardState(byId, report);
      updateStatusBar((state.appState && state.appState.config) || {});
    }
  }

  function startHotspotWithCheck() {
    const wiredActive = state.wirelessReport && state.wirelessReport.layers
      && state.wirelessReport.layers.some((l) => l.layer === "W3" && l.metrics && l.metrics.wired_nat_active);
    if (wiredActive) {
      document.getElementById("confirmTitle").textContent = t("confirm_switch_from_wired_title");
      document.getElementById("confirmBody").textContent = t("confirm_switch_from_wired_body");
      state.confirmCallback = () => runAction("enable-hotspot", { force: true });
      document.getElementById("confirmModal").classList.add("open");
      return;
    }
    runAction("enable-hotspot", {});
  }

  function _findLayerFault(layerObj, codes) {
    const faults = (layerObj && layerObj.faults) || [];
    return faults.find((f) => codes.includes(f.code)) || null;
  }

  function buildGuideSteps(byId, report, cfg) {
    const isWireless = Boolean(report && report.transport === "wireless");
    const submode = state.wirelessSubmode === "lan" ? "lan" : "hotspot";
    const presetId = getActivePresetId(report);
    const presetMeta = getActivePresetMeta(report);
    const steps = [];

    const downloadClientFn = async () => {
      const api = getApi();
      if (!api) return;
      showToast(t("toast_working"));
      const r = await api.download_xstreaming();
      showToast(r.message || (r.ok ? "OK" : "Error"));
      await refreshAll(true);
    };
    const launchClientFn = async () => {
      const api = getApi();
      if (!api) return;
      const r = await api.launch_stream_client();
      showToast(r.message || "OK");
    };
    const pickClientFn = async () => {
      const api = getApi();
      if (!api || !api.pick_stream_client) return;
      const r = await api.pick_stream_client();
      if (r && r.ok) {
        showToast(r.message || r.filename || "OK");
        state.wiredStale = true;
        state.wirelessStale = true;
        state.guideUserPicked = false;
        await refreshAll(true);
      } else if (r && r.message) {
        showToast(r.message);
      }
    };
    const runPcAction = (act, opts) => {
      state.guideUserPicked = false;
      return runAction(act, opts || {});
    };

    if (!isWireless) {
      const l1 = byId.L1 || { status: "idle", faults: [], metrics: {} };
      const l2 = byId.L2 || { status: "idle", faults: [], metrics: {} };
      const l3 = byId.L3 || { status: "idle", faults: [], metrics: {} };
      const l4 = byId.L4 || { status: "idle", faults: [], metrics: {} };

      const fVpn = _findLayerFault(l3, ["VPN_TUN_ACTIVE"]);
      const fNicHard = _findLayerFault(l1, ["NIC_NOT_FOUND", "NIC_PCIE_PARENT_DISABLED", "NIC_DISABLED"]);
      const fCable = _findLayerFault(l2, ["CABLE_UNPLUGGED"]);
      const fIcs = _findLayerFault(l3, ["ICS_ACTIVE"]);
      const fPs = _findLayerFault(l1, ["NIC_POWERSAVE_ENABLED"]);
      const f100 = _findLayerFault(l2, ["LINK_DEGRADED_100M"]);

      let s1Status = "ok";
      let s1Live = t("guide_live_cable_ok");
      const s1Actions = [];
      if (fVpn) {
        s1Status = "fail";
        s1Live = t("guide_live_cable_vpn");
      } else if (fNicHard) {
        s1Status = "fail";
        s1Live = localizeFault(fNicHard).message;
        if (fNicHard.fix_action) {
          s1Actions.push({ label: t("guide_btn_fix_nic"), primary: true, onClick: () => runPcAction(fNicHard.fix_action, {}) });
        }
      } else if (fCable) {
        s1Status = "fail";
        s1Live = t("guide_live_cable_unplugged");
      } else if (fIcs) {
        s1Status = "fail";
        s1Live = t("guide_live_ics");
        s1Actions.push({ label: t("guide_btn_clear_ics"), primary: true, onClick: () => runPcAction("clear-ics", {}) });
      } else if (fPs) {
        s1Status = "warn";
        s1Live = t("guide_live_cable_ps");
        s1Actions.push({ label: t("guide_btn_fix_nic"), primary: true, onClick: () => runPcAction("fix-nic", {}) });
      } else if (f100) {
        s1Status = "warn";
        s1Live = t("guide_live_cable_100m");
      }

      steps.push({
        id: "cable",
        actor: "pc",
        railTitle: t("guide_r_wired_1"),
        title: t("guide_t_wired_1"),
        goal: t("guide_g_wired_1"),
        status: s1Status,
        liveText: s1Live,
        actions: s1Actions,
        checklist: [],
        paramKind: null,
      });

      const fForeign = _findLayerFault(l4, ["FOREIGN_NETNAT_EXISTS"]);
      const natOn = Boolean(l4.metrics && l4.metrics.nat_active);
      let s2Status = "idle";
      let s2Live = t("guide_live_nat_off");
      const s2Actions = [];
      if (fForeign) {
        s2Status = "fail";
        s2Live = t("guide_live_nat_foreign");
        s2Actions.push({ label: t("btn_confirm_clear"), primary: true, onClick: () => handleFixAction(fForeign) });
      } else if (natOn) {
        s2Status = "ok";
        s2Live = t("guide_live_nat_on");
      } else if (!report.can_enable_direct) {
        s2Status = "warn";
        s2Live = t("guide_live_nat_blocked");
        s2Actions.push({ label: t("btn_enable_direct"), primary: true, disabled: true, onClick: () => {} });
      } else {
        s2Actions.push({ label: t("btn_enable_direct"), primary: true, onClick: () => runPcAction("enable-direct", {}) });
      }

      steps.push({
        id: "share",
        actor: "pc",
        railTitle: t("guide_r_wired_2"),
        title: t("guide_t_wired_2"),
        goal: t("guide_g_wired_2"),
        status: s2Status,
        liveText: s2Live,
        actions: s2Actions,
        checklist: [],
        paramKind: null,
      });
    } else if (submode === "hotspot") {
      const w1 = byId.W1 || { status: "idle", metrics: {} };
      const w3 = byId.W3 || { status: "idle", metrics: {} };
      const w4 = byId.W4 || { status: "idle", metrics: {} };

      let w1Status = "ok";
      let w1Live = t("guide_live_wifi_ok");
      const w1Actions = [];
      if (w1.status === "fail") {
        w1Status = "fail";
        w1Live = t("guide_live_wifi_fail");
      } else if (w1.status === "warn" || w4.status === "warn") {
        w1Status = "warn";
        w1Live = t("guide_live_wifi_warn");
        w1Actions.push({ label: t("w_btn_optimize"), primary: true, onClick: () => runPcAction("optimize-wifi", {}) });
      }
      steps.push({
        id: "wifi_opt",
        actor: "pc",
        railTitle: t("guide_r_hs_1"),
        title: t("guide_t_hs_1"),
        goal: t("guide_g_hs_1"),
        status: w1Status,
        liveText: w1Live,
        actions: w1Actions,
        checklist: [],
        paramKind: null,
      });

      const hsOn = Boolean(w3.metrics && w3.metrics.hotspot_state === "On");
      const wiredConflict = Boolean(w3.metrics && w3.metrics.wired_nat_active);
      let hsStatus = "idle";
      let hsLive = t("guide_live_hs_off");
      const hsActions = [];
      if (wiredConflict) {
        hsStatus = "warn";
        hsLive = t("guide_live_hs_conflict");
        hsActions.push({ label: t("w_btn_start_hotspot"), primary: true, onClick: () => { state.guideUserPicked = false; startHotspotWithCheck(); } });
      } else if (hsOn) {
        hsStatus = "ok";
        hsLive = t("guide_live_hs_on");
      } else {
        hsActions.push({ label: t("w_btn_start_hotspot"), primary: true, onClick: () => { state.guideUserPicked = false; startHotspotWithCheck(); } });
      }
      steps.push({
        id: "hotspot",
        actor: "pc",
        railTitle: t("guide_r_hs_2"),
        title: t("guide_t_hs_2"),
        goal: t("guide_g_hs_2"),
        status: hsStatus,
        liveText: hsLive,
        actions: hsActions,
        checklist: [],
        paramKind: null,
      });
    } else {
      const w1 = byId.W1 || { status: "idle", metrics: {} };
      const w2 = byId.W2 || { status: "idle", metrics: {} };
      const w4 = byId.W4 || { status: "idle", metrics: {} };
      const connected = Boolean(w2.metrics && w2.metrics.connected);
      let lanStatus = "ok";
      let lanLive = t("guide_live_lan_ok");
      const lanActions = [];
      if (w1.status === "fail") {
        lanStatus = "fail";
        lanLive = t("guide_live_wifi_fail");
      } else if (!connected) {
        lanStatus = "fail";
        lanLive = t("guide_live_lan_disc");
      } else if (w1.status === "warn" || w2.status === "warn" || w4.status === "warn") {
        lanStatus = "warn";
        lanLive = t("guide_live_lan_24g");
        lanActions.push({ label: t("w_btn_optimize"), primary: true, onClick: () => runPcAction("optimize-wifi", {}) });
      }
      steps.push({
        id: "lan_opt",
        actor: "pc",
        railTitle: t("guide_r_lan_1"),
        title: t("guide_t_lan_1"),
        goal: t("guide_g_lan_1"),
        status: lanStatus,
        liveText: lanLive,
        actions: lanActions,
        checklist: [],
        paramKind: null,
      });
    }

    // Shared optional PC streaming software step (skipped cleanly for Switch & Other)
    if (presetMeta.uses_stream_client || presetMeta.uses_uwp_loopback) {
      const appLayer = isWireless ? (byId.W6 || { faults: [], metrics: {} }) : (byId.L6 || { faults: [], metrics: {} });
      const uwpMiss = _findLayerFault(appLayer, ["UWP_LOOPBACK_MISSING"]);
      const clientMiss = _findLayerFault(appLayer, ["STREAM_CLIENT_MISSING", "PS_CLIENT_MISSING", "HANDHELD_CLIENT_MISSING"]);
      let cStatus = "ok";
      let cLive = t("guide_live_client_ok");
      const cActions = [];
      if (uwpMiss) {
        cStatus = "warn";
        cLive = t("guide_live_client_uwp");
        cActions.push({ label: t("guide_btn_fix_uwp"), primary: true, onClick: () => runPcAction("fix-uwp", {}) });
      } else if (clientMiss) {
        cStatus = "warn";
        cLive = t("guide_live_client_miss");
      }
      if (clientMiss) {
        cActions.push({
          label: presetId === "xbox" ? t("wiz_s3_btn") : t("wiz_btn_open_release"),
          primary: !uwpMiss,
          onClick: downloadClientFn,
        });
      } else if (presetMeta.uses_stream_client) {
        cActions.push({ label: t("wiz_s3_launch"), primary: false, onClick: launchClientFn });
      }
      if (presetMeta.uses_stream_client) {
        cActions.push({ label: t("guide_btn_pick_client"), primary: false, onClick: pickClientFn });
      }
      steps.push({
        id: "client",
        actor: "pc",
        railTitle: t("guide_r_client"),
        title: t("guide_t_client"),
        goal: t("guide_g_client"),
        status: cStatus,
        liveText: cLive,
        actions: cActions,
        checklist: [],
        paramKind: null,
      });
    }

    // Console / Handheld Step with interactive menu path checklist & live parameter box
    const shortPreset = { xbox: "xb", playstation: "ps", switch: "sw", handheld: "hh", other: "ot" }[presetId] || "ot";
    const shortMode = !isWireless ? "w" : (submode === "hotspot" ? "h" : "l");
    const checkCount = (
      shortPreset === "xb"
      || shortPreset === "ps"
      || (shortPreset === "sw" && shortMode === "w")
      || (shortPreset === "hh" && shortMode === "w")
    ) ? 3 : 2;
    const checklist = [];
    for (let idx = 1; idx <= checkCount; idx++) {
      const k = `guide_c_${shortPreset}_${shortMode}_${idx}`;
      checklist.push({ id: k, text: t(k) });
    }

    const peerLayer = isWireless ? (byId.W5 || { status: "idle", metrics: {} }) : (byId.L5 || { status: "idle", metrics: {} });
    const w3Metrics = (byId.W3 && byId.W3.metrics) || {};
    const hsClients = w3Metrics.hotspot_clients || [];
    const peerReachable = !isWireless
      ? Boolean(peerLayer.status === "ok" || (peerLayer.metrics && peerLayer.metrics.peer_reachable))
      : (submode === "hotspot"
        ? (hsClients.length > 0 || peerLayer.status === "ok")
        : (Boolean((cfg.wireless_peer_ip || "").trim()) && peerLayer.status === "ok"));
    const allTicked = checklist.every((c) => Boolean(state.guideManualChecks[c.id]));

    const conGoal = !isWireless
      ? t("guide_g_console_wired")
      : (submode === "hotspot" ? t("guide_g_console_hs") : t("guide_g_console_lan"));
    const conLive = !isWireless
      ? t(peerReachable ? "guide_live_console_wired_ok" : "guide_live_console_wired_wait")
      : (submode === "hotspot"
        ? t(peerReachable ? "guide_live_console_hs_ok" : "guide_live_console_hs_wait")
        : t(peerReachable ? "guide_live_console_lan_ok" : "guide_live_console_lan_wait"));

    const conActions = [];
    if (!isWireless) {
      const dnsTxt = (cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" / ");
      conActions.push({
        label: t("wiz_copy_params"),
        primary: false,
        onClick: async () => {
          const txt = `IP: ${cfg.peer_ip || "192.168.50.1"}\nMask: ${cfg.subnet_mask || "255.255.255.0"}\nGateway: ${cfg.host_ip || "192.168.50.2"}\nDNS: ${dnsTxt}`;
          await copyText(txt);
          showToast(t("toast_copied"));
        },
      });
      if (presetId === "handheld" || presetId === "other") {
        conActions.push({
          label: t("wiz_copy_nmcli"),
          primary: false,
          onClick: async () => {
            const api = getApi();
            let cmd = `sudo nmcli con add type ethernet ifname eth0 con-name wiredeck-direct ipv4.method manual ipv4.addresses ${cfg.peer_ip || "192.168.50.30"}/${cfg.prefix_length || 24} ipv4.gateway ${cfg.host_ip || "192.168.50.2"} ipv4.dns "${(cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" ")}" && sudo nmcli con up wiredeck-direct`;
            if (api && api.run_toolkit) {
              try {
                const r = await api.run_toolkit("handheld/emit-linux-nmcli-script");
                if (r && r.ok && r.data && r.data.nmcli_setup) cmd = r.data.nmcli_setup;
              } catch (_) {}
            }
            await copyText(cmd);
            showToast(t("toast_copied"));
          },
        });
      }
    }

    steps.push({
      id: "console",
      actor: "console",
      railTitle: t("guide_r_console"),
      title: t("guide_t_console"),
      goal: conGoal,
      status: (peerReachable || allTicked) ? "ok" : "idle",
      liveText: conLive,
      actions: conActions,
      checklist,
      paramKind: !isWireless ? "wired_ip" : (submode === "hotspot" ? "hotspot" : "lan_ip"),
    });

    // Final Verification & Play Entry Step
    const pingOk = Boolean(state.guidePingResult && state.guidePingResult.ok);
    const vOk = peerReachable || pingOk;
    const vActions = [
      {
        label: t("guide_btn_test_now"),
        primary: !vOk,
        onClick: async () => {
          const api = getApi();
          if (!api) return;
          state.guidePingResult = { testing: true };
          renderCurrentGuide();
          let res;
          if (isWireless && api.ping_wireless_peer_now) {
            res = await api.ping_wireless_peer_now("");
          } else {
            res = await api.ping_peer_now();
          }
          state.guidePingResult = res;
          renderCurrentGuide();
        },
      },
    ];
    steps.push({
      id: "verify",
      actor: "verify",
      railTitle: t("guide_r_verify"),
      title: t("guide_t_verify"),
      goal: t("guide_g_verify"),
      status: vOk ? "ok" : "idle",
      liveText: t(vOk ? "guide_live_verify_ok" : "guide_live_verify_wait"),
      actions: vActions,
      checklist: [],
      paramKind: "verify",
      flowKey: `${isWireless ? "wireless" : "wired"}:${submode}:${presetId}`,
      dailyTip: t(!isWireless ? "guide_daily_tip_wired" : (submode === "hotspot" ? "guide_daily_tip_hs" : "guide_daily_tip_lan")),
    });

    return steps;
  }

  function detectGuideClientLabel(byId, presetId, presetMeta) {
    const m = (byId.L6 && byId.L6.metrics) || (byId.W6 && byId.W6.metrics) || {};
    if (m.custom_client_path) {
      const stem = String(m.custom_client_path).split(/[\\/]/).pop().replace(/\.exe$/i, "");
      return { found: true, label: stem || (presetMeta && presetMeta.official_client_name) || "Client" };
    }
    if (presetId === "xbox") {
      if (m.xstreaming_path) return { found: true, label: "XStreaming" };
      if (m.xbox_app_installed) return { found: true, label: "Xbox" };
      return { found: false, label: "XStreaming" };
    }
    if (presetId === "playstation") {
      if (m.chiaki_path) return { found: true, label: "Chiaki-ng" };
      if (m.ps_remote_path) return { found: true, label: "PS Remote Play" };
      return { found: false, label: "Chiaki-ng" };
    }
    if (presetId === "handheld") {
      if (m.moonlight_path) return { found: true, label: "Moonlight" };
      if (m.steam_path) return { found: true, label: "Steam" };
      if (m.sunshine_path) return { found: true, label: "Sunshine" };
      return { found: false, label: "Moonlight" };
    }
    return { found: false, label: (presetMeta && presetMeta.official_client_name) || "" };
  }

  function explainGuidePingFailure(byId, report, cfg) {
    const isWireless = Boolean(report && report.transport === "wireless");
    if (!isWireless) {
      const l2 = byId.L2 || {};
      const l4 = byId.L4 || { metrics: {} };
      if (l2.status === "fail") return { msg: t("guide_ping_fail_cable"), switchHotspot: false };
      if (!(l4.metrics && l4.metrics.nat_active)) return { msg: t("guide_ping_fail_nat"), switchHotspot: false };
      return { msg: t("guide_ping_fail_ip"), switchHotspot: false };
    }
    const w3 = byId.W3 || { faults: [], metrics: {} };
    if (state.wirelessSubmode !== "lan") {
      if ((w3.metrics && w3.metrics.hotspot_state) !== "On") return { msg: t("guide_ping_fail_hs_off"), switchHotspot: false };
      if (!((w3.metrics && w3.metrics.hotspot_clients) || []).length) return { msg: t("guide_ping_fail_hs_noclient"), switchHotspot: false };
      return { msg: t("ping_fail"), switchHotspot: false };
    }
    if (!(cfg.wireless_peer_ip || "").trim()) return { msg: t("guide_ping_fail_lan_empty"), switchHotspot: false };
    const apIso = (w3.faults || []).some((f) => f.code === "LAN_AP_ISOLATION_SUSPECTED");
    if (apIso) return { msg: t("guide_ping_fail_ap_iso"), switchHotspot: true };
    return { msg: t("ping_fail"), switchHotspot: false };
  }

  function renderGuideParams(step, byId, report, cfg) {
    const host = document.getElementById("guideParams");
    if (!host) return;
    host.innerHTML = "";
    if (!step.paramKind) {
      host.style.display = "none";
      return;
    }
    host.style.display = "flex";

    if (step.paramKind === "wired_ip") {
      const dnsTxt = (cfg.dns_servers || ["223.5.5.5", "8.8.8.8"]).join(" / ");
      const ipGrid = document.createElement("div");
      ipGrid.className = "ip-box";
      ipGrid.innerHTML = `
        <div><div class="ip-k">${escapeHtml(t("ip_label_addr"))}</div><div class="ip-v mono">${escapeHtml(cfg.peer_ip || "192.168.50.1")}</div></div>
        <div><div class="ip-k">${escapeHtml(t("ip_label_mask"))}</div><div class="ip-v mono">${escapeHtml(cfg.subnet_mask || "255.255.255.0")}</div></div>
        <div><div class="ip-k">${escapeHtml(t("ip_label_gw"))}</div><div class="ip-v mono">${escapeHtml(cfg.host_ip || "192.168.50.2")}</div></div>
        <div><div class="ip-k">${escapeHtml(t("ip_label_dns"))}</div><div class="ip-v mono">${escapeHtml(dnsTxt)}</div></div>
      `;
      host.appendChild(ipGrid);
      return;
    }

    if (step.paramKind === "hotspot") {
      const w3 = byId.W3 || { metrics: {} };
      const ssidVal = (state.hotspotCreds && state.hotspotCreds.ssid) || (w3.metrics && w3.metrics.hotspot_ssid) || "—";
      const passVal = (state.hotspotCreds && state.hotspotCreds.passphrase) || "";
      const clients = (w3.metrics && w3.metrics.hotspot_clients) || [];

      const bar = document.createElement("div");
      bar.className = "guide-param-bar";
      bar.innerHTML = `
        <div class="w-strip">
          <span class="w-kv"><span class="w-k">${escapeHtml(t("w_ssid"))}</span> <span class="mono w-v">${escapeHtml(ssidVal)}</span></span>
          <span class="w-kv"><span class="w-k">${escapeHtml(t("w_pass"))}</span> <span class="mono w-v">${escapeHtml(state.showHotspotPass && passVal ? passVal : "••••••••")}</span></span>
        </div>
      `;
      const acts = document.createElement("div");
      acts.className = "row-actions";
      const showBtn = document.createElement("button");
      showBtn.type = "button";
      showBtn.className = "btn btn-sm";
      showBtn.textContent = t(state.showHotspotPass ? "w_hide" : "w_show");
      showBtn.addEventListener("click", () => {
        state.showHotspotPass = !state.showHotspotPass;
        renderCurrentGuide();
      });
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "btn btn-sm";
      copyBtn.textContent = t("w_copy_pass");
      copyBtn.addEventListener("click", async () => {
        const api = getApi();
        let pass = passVal;
        if (!pass && api && api.get_hotspot_credentials) {
          const c = await api.get_hotspot_credentials();
          if (c && c.ok) { state.hotspotCreds = c; pass = c.passphrase; }
        }
        if (pass) { await copyText(pass); showToast(t("toast_copied")); }
      });
      const pill = document.createElement("span");
      pill.className = "ext-tag";
      pill.textContent = clients.length > 0 ? `${t("w_clients")}: ${clients[0].ip}` : t("w_no_client");
      acts.appendChild(showBtn);
      acts.appendChild(copyBtn);
      acts.appendChild(pill);
      bar.appendChild(acts);
      host.appendChild(bar);
      return;
    }

    if (step.paramKind === "lan_ip") {
      const w2 = byId.W2 || { metrics: {} };
      const ssidVal = (w2.metrics && w2.metrics.ssid) || "5 GHz Wi-Fi";
      const bar = document.createElement("div");
      bar.className = "guide-param-bar";
      bar.innerHTML = `
        <span class="w-kv"><span class="w-k">Wi-Fi</span> <span class="mono w-v">${escapeHtml(ssidVal)}</span></span>
        <div class="ip-pair">
          <span class="w-k">${escapeHtml(t("w_lan_peer_label"))}</span>
          <input id="guideLanPeerInput" class="mono w-ip-in" placeholder="${escapeHtml(t("w_lan_peer_placeholder"))}" value="${escapeHtml(cfg.wireless_peer_ip || "")}" />
          <button type="button" class="btn btn-sm btn-primary" id="btnGuideSaveLanIp">${escapeHtml(t("guide_btn_save_ip"))}</button>
        </div>
      `;
      host.appendChild(bar);
      const saveBtn = bar.querySelector("#btnGuideSaveLanIp");
      const inEl = bar.querySelector("#guideLanPeerInput");
      const doSave = async () => {
        if (await saveLanPeerIp(inEl.value)) {
          showToast(t("toast_saved"));
          state.wirelessStale = true;
          await refreshAll(true);
        }
      };
      saveBtn.addEventListener("click", doSave);
      inEl.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); doSave(); }
      });
      return;
    }

    if (step.paramKind === "verify") {
      const ping = state.guidePingResult;
      if (ping) {
        const resBox = document.createElement("div");
        if (ping.testing) {
          resBox.className = "peer-ping-result show";
          resBox.textContent = t("ping_testing");
        } else if (ping.ok) {
          resBox.className = "peer-ping-result show ok";
          const ipTxt = ping.target_ip || ping.peer_ip || "";
          const metricTxt = typeof ping.jitter_ms === "number"
            ? ` (${ping.avg_rtt_ms} ms / jitter ${ping.jitter_ms} ms)`
            : (typeof ping.rtt_ms === "number" ? ` (${ping.rtt_ms.toFixed(1)} ms)` : "");
          resBox.textContent = `● ${ipTxt} ${t("ping_ok")}${metricTxt}`;
        } else {
          const diagErr = explainGuidePingFailure(byId, report, cfg);
          resBox.className = "peer-ping-result show fail";
          resBox.textContent = `✕ ${diagErr.msg}`;
          if (diagErr.switchHotspot) {
            const swBtn = document.createElement("button");
            swBtn.type = "button";
            swBtn.className = "btn-link-subtle";
            swBtn.style.marginLeft = "8px";
            swBtn.textContent = t("guide_switch_to_hotspot");
            swBtn.addEventListener("click", async () => {
              const api = getApi();
              state.transport = "wireless";
              state.wirelessSubmode = "hotspot";
              state.guideUserPicked = false;
              if (api && api.set_transport) await api.set_transport("wireless");
              if (api && api.set_wireless_submode) await api.set_wireless_submode("hotspot");
              await refreshAll(true);
            });
            resBox.appendChild(swBtn);
          }
        }
        host.appendChild(resBox);
      }

      const presetId = getActivePresetId(report);
      const presetMeta = getActivePresetMeta(report);
      const clientInfo = detectGuideClientLabel(byId, presetId, presetMeta);

      const entryCard = document.createElement("div");
      entryCard.className = "guide-entry-card";
      const descText = presetMeta.uses_stream_client
        ? t("guide_entry_desc_client").replace(/\{client\}/g, clientInfo.label)
        : t("guide_entry_desc_noclient");
      entryCard.innerHTML = `
        <div class="guide-entry-col">
          <div class="guide-entry-title">${escapeHtml(t("guide_entry_title"))}</div>
          <div class="guide-entry-desc">${escapeHtml(descText)}</div>
        </div>
      `;

      const entryActs = document.createElement("div");
      entryActs.className = "row-actions";

      const scDone = Boolean(step.flowKey && state.guideShortcutCreatedKey === step.flowKey);
      const scBtn = document.createElement("button");
      scBtn.type = "button";
      scBtn.className = !presetMeta.uses_stream_client ? "btn btn-sm btn-primary" : "btn btn-sm";
      scBtn.textContent = t(scDone ? "guide_btn_shortcut_done" : "guide_btn_create_shortcut");
      scBtn.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.create_play_shortcut) return;
        scBtn.disabled = true;
        try {
          const r = await api.create_play_shortcut();
          showToast(r.message || (r.ok ? "OK" : r.code));
          if (r.ok && step.flowKey) {
            state.guideShortcutCreatedKey = step.flowKey;
            scBtn.textContent = t("guide_btn_shortcut_done");
          }
        } catch (err) {
          showToast(String(err));
        } finally {
          scBtn.disabled = false;
        }
      });
      entryActs.appendChild(scBtn);

      if (presetMeta.uses_stream_client) {
        const enterBtn = document.createElement("button");
        enterBtn.type = "button";
        enterBtn.className = "btn btn-sm btn-primary";
        enterBtn.textContent = t(clientInfo.found ? "guide_btn_enter_client" : "guide_btn_get_client").replace("{client}", clientInfo.label);
        enterBtn.addEventListener("click", async () => {
          const api = getApi();
          if (!api) return;
          if (clientInfo.found) {
            if (api.quick_play_now) {
              enterBtn.disabled = true;
              try {
                const r = await api.quick_play_now();
                if (r && r.ok) {
                  showToast(r.message || `${t("wiz_s3_launch")} ${clientInfo.label}`);
                } else {
                  showToast((r && r.message) || t("guide_quickplay_blocked"));
                  state.wiredStale = true;
                  state.wirelessStale = true;
                  state.guideUserPicked = false;
                }
                await refreshAll(true);
              } finally {
                enterBtn.disabled = false;
              }
            } else {
              const r = await api.launch_stream_client();
              showToast(r.message || "OK");
            }
          } else {
            showToast(t("toast_working"));
            const r = await api.download_xstreaming();
            showToast(r.message || (r.ok ? "OK" : "Error"));
            await refreshAll(true);
          }
        });
        entryActs.appendChild(enterBtn);
      }

      entryCard.appendChild(entryActs);
      host.appendChild(entryCard);

      const plgStrip = document.createElement("div");
      plgStrip.className = "guide-plugins-strip";
      const badges = state.pluginBadges || [];
      if (badges.length) {
        const leftBox = document.createElement("div");
        leftBox.className = "guide-plugins-pills";
        const lbl = document.createElement("span");
        lbl.textContent = t("guide_plugins_active");
        leftBox.appendChild(lbl);
        badges.forEach((b) => {
          const tag = document.createElement("span");
          tag.className = "ext-tag";
          tag.textContent = b.name;
          leftBox.appendChild(tag);
        });
        plgStrip.appendChild(leftBox);
      } else {
        const lbl = document.createElement("span");
        lbl.textContent = t("guide_plugins_none");
        plgStrip.appendChild(lbl);
      }
      const goPlg = document.createElement("button");
      goPlg.type = "button";
      goPlg.className = "btn-link-subtle";
      goPlg.textContent = t("guide_plugins_goto");
      goPlg.addEventListener("click", () => switchView("plugins", true));
      plgStrip.appendChild(goPlg);
      host.appendChild(plgStrip);

      if (step.dailyTip) {
        const tipRow = document.createElement("div");
        tipRow.className = "guide-tip-row";
        const tip = document.createElement("div");
        tip.className = "guide-tip";
        tip.textContent = step.dailyTip;
        tipRow.appendChild(tip);

        const rsBtn = document.createElement("button");
        rsBtn.type = "button";
        rsBtn.className = "btn-link-subtle";
        rsBtn.textContent = t(state.guideRestoreShortcutCreated ? "guide_btn_restore_done" : "guide_btn_restore_shortcut");
        rsBtn.addEventListener("click", async () => {
          const api = getApi();
          if (!api || !api.create_restore_shortcut) return;
          const r = await api.create_restore_shortcut();
          showToast(r.message || (r.ok ? "OK" : r.code));
          if (r && r.ok) {
            state.guideRestoreShortcutCreated = true;
            rsBtn.textContent = t("guide_btn_restore_done");
          }
        });
        tipRow.appendChild(rsBtn);
        host.appendChild(tipRow);
      }
    }
  }

  function renderCurrentGuide() {
    const steps = state.guideSteps || [];
    const byId = state.guideById || {};
    const report = state.guideReport;
    const cfg = (state.appState && state.appState.config) || {};
    if (!steps.length) return;

    const idx = Math.max(0, Math.min(state.guideStepIdx, steps.length - 1));
    state.guideStepIdx = idx;
    const step = steps[idx];

    const isWireless = Boolean(report && report.transport === "wireless");
    const bW = document.getElementById("btnGuideWired");
    const bH = document.getElementById("btnGuideHotspot");
    const bL = document.getElementById("btnGuideLan");
    if (bW) bW.classList.toggle("active", !isWireless);
    if (bH) bH.classList.toggle("active", isWireless && state.wirelessSubmode !== "lan");
    if (bL) bL.classList.toggle("active", isWireless && state.wirelessSubmode === "lan");

    const presetSel = document.getElementById("guidePresetSelect");
    const activePid = getActivePresetId(report);
    if (presetSel) presetSel.value = activePid;
    const presetName = t(`preset_${activePid}_name`);
    const subEl = document.getElementById("guideSub");
    if (subEl) subEl.textContent = `${presetName} · ${t("wizard_sub")}`;

    const rail = document.getElementById("guideRail");
    if (rail) {
      rail.innerHTML = "";
      steps.forEach((s, i) => {
        const pill = document.createElement("button");
        pill.type = "button";
        const isDone = s.status === "ok";
        const sevCls = s.status === "fail" ? "fail" : (s.status === "warn" ? "warn" : "");
        pill.className = "guide-pill" + (i === idx ? " active" : "") + (isDone ? " done" : (sevCls ? " " + sevCls : ""));
        pill.innerHTML = `
          <span class="guide-pill-num">${isDone ? "✓" : String(i + 1)}</span>
          <span class="guide-pill-label">${escapeHtml(s.railTitle)}</span>
        `;
        pill.addEventListener("click", () => {
          state.guideUserPicked = true;
          state.guideStepIdx = i;
          renderCurrentGuide();
        });
        rail.appendChild(pill);
      });
    }

    const actorPill = document.getElementById("guideActorPill");
    if (actorPill) {
      actorPill.className = "actor-pill actor-" + step.actor;
      actorPill.textContent = t("guide_actor_" + step.actor);
    }
    const titleEl = document.getElementById("guideStepTitle");
    if (titleEl) titleEl.textContent = `${idx + 1}. ${step.title}`;
    setBadge("guideStepBadge", step.status);
    const goalEl = document.getElementById("guideGoal");
    if (goalEl) goalEl.textContent = step.goal;

    const liveBox = document.getElementById("guideLiveBox");
    if (liveBox) {
      liveBox.className = "guide-live" + (step.status === "ok" ? " sev-ok" : (step.status === "fail" ? " sev-fail" : (step.status === "warn" ? " sev-warn" : "")));
    }
    const liveTextEl = document.getElementById("guideLiveText");
    if (liveTextEl) liveTextEl.textContent = step.liveText;

    const actHost = document.getElementById("guideActions");
    if (actHost) {
      actHost.innerHTML = "";
      (step.actions || []).forEach((a) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm" + (a.primary ? " btn-primary" : "");
        btn.textContent = a.label;
        btn.dataset.stepDisabled = a.disabled ? "1" : "0";
        btn.disabled = Boolean(state.busy || a.disabled);
        btn.addEventListener("click", a.onClick);
        actHost.appendChild(btn);
      });
    }

    const checkHost = document.getElementById("guideChecklist");
    if (checkHost) {
      if (step.checklist && step.checklist.length > 0) {
        checkHost.style.display = "block";
        checkHost.innerHTML = "";
        step.checklist.forEach((item, cIdx) => {
          const checked = Boolean(state.guideManualChecks[item.id]);
          const row = document.createElement("label");
          row.className = "guide-check-item" + (checked ? " checked" : "");
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = checked;
          cb.addEventListener("change", () => {
            state.guideManualChecks[item.id] = cb.checked;
            renderCurrentGuide();
          });
          const num = document.createElement("span");
          num.className = "guide-check-num";
          num.textContent = `${cIdx + 1})`;
          const txt = document.createElement("span");
          txt.textContent = item.text;
          row.appendChild(cb);
          row.appendChild(num);
          row.appendChild(txt);
          checkHost.appendChild(row);
        });
      } else {
        checkHost.style.display = "none";
        checkHost.innerHTML = "";
      }
    }

    renderGuideParams(step, byId, report, cfg);

    const prevBtn = document.getElementById("btnGuidePrev");
    const nextBtn = document.getElementById("btnGuideNext");
    const ctrEl = document.getElementById("guideStepCounter");
    if (prevBtn) prevBtn.disabled = idx === 0;
    if (ctrEl) ctrEl.textContent = t("guide_step_of").replace("{i}", String(idx + 1)).replace("{n}", String(steps.length));
    if (nextBtn) {
      const isLast = idx >= steps.length - 1;
      nextBtn.textContent = t(isLast ? "guide_finish" : "guide_next");
    }
  }

  function updateWizardState(byId, report) {
    const cfg = (state.appState && state.appState.config) || {};
    const isWireless = Boolean(report && report.transport === "wireless");
    const presetId = getActivePresetId(report);
    const flowKey = `${isWireless ? "wireless" : "wired"}:${state.wirelessSubmode}:${presetId}`;
    if (state.guideLastFlowKey !== flowKey) {
      state.guideLastFlowKey = flowKey;
      state.guideUserPicked = false;
      state.guidePingResult = null;
    }

    const steps = buildGuideSteps(byId, report, cfg);
    state.guideSteps = steps;
    state.guideById = byId;
    state.guideReport = report;

    const previewStep = state.appState && state.appState.preview_wizard_step;
    if (typeof previewStep === "number" && previewStep >= 1) {
      state.guideStepIdx = Math.max(0, Math.min(previewStep - 1, steps.length - 1));
    } else if (!state.guideUserPicked) {
      const firstTodo = steps.findIndex((s) => s.status !== "ok");
      state.guideStepIdx = firstTodo >= 0 ? firstTodo : (steps.length - 1);
    }

    renderCurrentGuide();

    const wizDot = document.getElementById("wizDot");
    const hasWizTodo = steps.some((s) => s.id !== "verify" && s.status !== "ok");
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
      switchView("wizard", true);
      return;
    }
    if (fault.fix_action === "clear-netnat") {
      const names = (fault.details && fault.details.nat_names) ? fault.details.nat_names.join(", ") : "";
      document.getElementById("confirmTitle").textContent = t("confirm_netnat_title");
      document.getElementById("confirmBody").textContent = `${t("confirm_netnat_body")}${names ? " (" + names + ")" : ""}`;
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
      if (!res.ok && res.code === "WIRED_NAT_CONFLICT" && action === "enable-hotspot") {
        setBusy(false);
        startHotspotWithCheck();
        return;
      }
      showToast(res.message || (res.ok ? "OK" : res.code));
      state.wiredStale = true;
      state.wirelessStale = true;
      await refreshAll(true);
    } catch (err) {
      showToast(String(err));
    } finally {
      setBusy(false);
    }
  }

  function populateSelect(selId, items, currentVal) {
    const sel = document.getElementById(selId);
    if (!sel) return;
    sel.innerHTML = `<option value="">${escapeHtml(t("set_eth_auto"))}</option>`;
    items.forEach((name) => {
      if (!name) return;
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === currentVal) opt.selected = true;
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
      const badge = e.valid ? `<span class="ext-tag">${escapeHtml(m.layer || "L1")}</span>` : `<span class="sev-text fail">INVALID</span>`;
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
    document.querySelectorAll("[data-onb-transport]").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-onb-transport") === state.onbSelectedTransport);
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
    state.onbSelectedTransport = state.transport || cfg.active_transport || "wired";
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
    state.transport = cfg.active_transport || "wired";
    state.wirelessSubmode = cfg.wireless_submode || "hotspot";
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
    document.getElementById("setWirelessPeerIp").value = cfg.wireless_peer_ip || "";
    const hbVal = cfg.hotspot_band || (cfg.hotspot_prefer_5ghz === false ? "auto" : "5ghz");
    const setHbEl = document.getElementById("setHotspotBand");
    if (setHbEl) setHbEl.value = hbVal;
    const wHbEl = document.getElementById("wHotspotBandSel");
    if (wHbEl) wHbEl.value = hbVal;
    const kaEl = document.getElementById("setKeepAwake");
    if (kaEl) kaEl.checked = cfg.keep_awake_during_stream !== false;
    const arEl = document.getElementById("setAutoRestoreExit");
    if (arEl) arEl.checked = Boolean(cfg.auto_restore_on_exit);
    document.getElementById("wLanPeerInput").value = cfg.wireless_peer_ip || "";
    updateStatusBar(cfg);
  }

  function renderSystemHealthBadge(health) {
    const btn = document.getElementById("sbHealthBtn");
    if (!btn) return;
    state.systemHealth = health || null;
    if (!health || !health.ok) {
      btn.className = "sb-link";
      btn.textContent = t("sb_health_ok");
      return;
    }
    const wc = Number(health.warning_count || 0);
    if (wc > 0) {
      btn.className = "sb-link sb-health-warn";
      btn.textContent = t("sb_health_warn").replace("{n}", String(wc));
    } else {
      btn.className = "sb-link sb-health-ok";
      btn.textContent = t("sb_health_ok");
    }
  }

  function openSystemHealthModal() {
    const h = state.systemHealth;
    if (!h) return;
    const lines = [];
    (h.items || []).forEach((it) => {
      const icon = it.ok ? "✓" : "▲";
      lines.push(`${icon} ${it.label}: ${it.value}`);
    });
    const warns = h.warnings || [];
    if (warns.length) {
      lines.push("");
      warns.forEach((w) => {
        lines.push(`! [${w.code}] ${w.title}`);
        if (w.detail) lines.push(`  ${w.detail}`);
      });
    } else {
      lines.push("");
      lines.push(t("health_all_clear"));
    }
    openTextModal(t("health_modal_title"), lines.join("\n"));
  }

  function compareSemver(a, b) {
    const parse = (s) => String(s || "0").replace(/^[vV]/, "").split(/[.\-+]/).map((x) => parseInt(x, 10) || 0);
    const pa = parse(a);
    const pb = parse(b);
    const len = Math.max(pa.length, pb.length);
    for (let i = 0; i < len; i += 1) {
      const va = pa[i] || 0;
      const vb = pb[i] || 0;
      if (va < vb) return -1;
      if (va > vb) return 1;
    }
    return 0;
  }

  function collectPluginFormSettings(manifest) {
    const form = document.getElementById("pluginConfigForm");
    const out = {};
    (manifest.settings_schema || []).forEach((field) => {
      if (field.type === "bool") {
        const el = form.querySelector(`[data-plg-key="${field.key}"]`);
        if (el) out[field.key] = Boolean(el.checked);
      } else if (field.type === "number") {
        const el = form.querySelector(`[data-plg-key="${field.key}"]`);
        if (el) {
          const val = parseFloat(el.value);
          out[field.key] = Number.isNaN(val) ? (field.default || 0) : val;
        }
      } else if (field.type === "multi_preset") {
        const checks = form.querySelectorAll(`[data-plg-preset-key="${field.key}"]`);
        const picked = [];
        checks.forEach((chk) => {
          if (chk.checked) picked.push(chk.value);
        });
        out[field.key] = picked;
      } else {
        const el = form.querySelector(`[data-plg-key="${field.key}"]`);
        if (el) out[field.key] = String(el.value || "").trim();
      }
    });
    return out;
  }

  function openPluginConfigModal(entry) {
    const m = (entry && entry.manifest) || {};
    state.activePluginConfig = entry;
    document.getElementById("pluginConfigTitle").textContent = `${m.name || m.id} (v${m.version || "1.0.0"})`;

    const actsHost = document.getElementById("pluginConfigActions");
    actsHost.innerHTML = "";
    (m.actions || []).forEach((act) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = act.style === "primary" ? "btn btn-sm btn-primary" : "btn btn-sm";
      btn.textContent = act.label || act.id;
      if (act.description) btn.title = act.description;
      btn.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.run_plugin_action) return;
        btn.disabled = true;
        try {
          const res = await api.run_plugin_action(m.id, act.id);
          showToast((res && res.message) || "OK");
          if (res && res.saved_settings && Object.keys(res.saved_settings).length) {
            await loadPluginsData(false);
            const refreshed = (state.pluginsInstalled || []).find((x) => x.manifest && x.manifest.id === m.id);
            if (refreshed) openPluginConfigModal(refreshed);
          }
        } finally {
          btn.disabled = false;
        }
      });
      actsHost.appendChild(btn);
    });

    const form = document.getElementById("pluginConfigForm");
    form.innerHTML = "";
    const banner = document.getElementById("pluginConfigBanner");
    if (banner) {
      banner.style.display = "none";
      banner.className = "plugin-cfg-banner";
      banner.textContent = "";
    }
    const curSettings = (entry && entry.settings) || {};
    const fields = m.settings_schema || [];
    const dynamicSelects = [];
    if (!fields.length) {
      const empty = document.createElement("div");
      empty.className = "guide-tip";
      empty.textContent = t("plugins_no_settings");
      form.appendChild(empty);
    } else {
      fields.forEach((field) => {
        const row = document.createElement("div");
        row.className = "plugin-form-row";
        const info = document.createElement("div");
        info.className = "plugin-form-info";
        info.innerHTML = `
          <div class="plugin-form-lbl">${escapeHtml(field.label || field.key)}</div>
          ${field.description ? `<div class="plugin-form-desc">${escapeHtml(field.description)}</div>` : ""}
        `;
        row.appendChild(info);

        const ctrl = document.createElement("div");
        ctrl.className = "plugin-form-ctrl";
        const curVal = Object.prototype.hasOwnProperty.call(curSettings, field.key) ? curSettings[field.key] : field.default;

        if (field.type === "bool") {
          const sw = document.createElement("label");
          sw.className = "win-switch";
          const inp = document.createElement("input");
          inp.type = "checkbox";
          inp.setAttribute("data-plg-key", field.key);
          inp.checked = Boolean(curVal);
          const slider = document.createElement("span");
          slider.className = "win-slider";
          sw.appendChild(inp);
          sw.appendChild(slider);
          ctrl.appendChild(sw);
        } else if (field.type === "select" || field.type === "dynamic_select") {
          const sel = document.createElement("select");
          sel.setAttribute("data-plg-key", field.key);
          const baseOpts = field.options || [];
          baseOpts.forEach((opt) => {
            const o = document.createElement("option");
            o.value = opt.value;
            o.textContent = opt.label || opt.value;
            if (String(curVal) === String(opt.value)) o.selected = true;
            sel.appendChild(o);
          });
          if (curVal && !baseOpts.some((opt) => String(opt.value) === String(curVal))) {
            const curOpt = document.createElement("option");
            curOpt.value = String(curVal);
            curOpt.textContent = String(curVal);
            curOpt.selected = true;
            sel.appendChild(curOpt);
          }
          if (field.type === "dynamic_select") {
            dynamicSelects.push({ key: field.key, sel, curVal: String(curVal || "") });
          }
          ctrl.appendChild(sel);
        } else if (field.type === "number") {
          const inp = document.createElement("input");
          inp.type = "number";
          inp.className = "mono";
          inp.setAttribute("data-plg-key", field.key);
          if (field.min !== null && field.min !== undefined) inp.min = String(field.min);
          if (field.max !== null && field.max !== undefined) inp.max = String(field.max);
          if (field.step !== null && field.step !== undefined) inp.step = String(field.step);
          inp.value = String(curVal !== undefined ? curVal : 1);
          ctrl.appendChild(inp);
        } else if (field.type === "file_path") {
          const inp = document.createElement("input");
          inp.type = "text";
          inp.className = "mono";
          inp.setAttribute("data-plg-key", field.key);
          inp.value = String(curVal || "");
          if (field.placeholder) inp.placeholder = field.placeholder;
          ctrl.appendChild(inp);
          const br = document.createElement("button");
          br.type = "button";
          br.className = "btn btn-sm";
          br.textContent = t("plugins_browse");
          br.addEventListener("click", async () => {
            const api = getApi();
            if (!api || !api.pick_plugin_file) return;
            const picked = await api.pick_plugin_file(field.file_filter || "");
            if (picked && picked.ok && picked.path) inp.value = picked.path;
          });
          ctrl.appendChild(br);
        } else if (field.type === "multi_preset") {
          const box = document.createElement("div");
          box.className = "plugin-preset-checks";
          const pickedList = Array.isArray(curVal) ? curVal : [];
          PRESET_ORDER.forEach((pid) => {
            const lbl = document.createElement("label");
            lbl.className = "inline-check";
            const chk = document.createElement("input");
            chk.type = "checkbox";
            chk.value = pid;
            chk.setAttribute("data-plg-preset-key", field.key);
            chk.checked = pickedList.includes(pid) || pickedList.includes("all");
            const txt = document.createElement("span");
            txt.textContent = pid === "playstation" ? "PS" : (pid === "handheld" ? "Deck" : pid.toUpperCase());
            lbl.appendChild(chk);
            lbl.appendChild(txt);
            box.appendChild(lbl);
          });
          ctrl.appendChild(box);
        } else {
          const inp = document.createElement("input");
          inp.type = "text";
          inp.className = "mono";
          inp.setAttribute("data-plg-key", field.key);
          inp.value = String(curVal || "");
          if (field.placeholder) inp.placeholder = field.placeholder;
          ctrl.appendChild(inp);
        }

        row.appendChild(ctrl);
        form.appendChild(row);
      });
    }

    document.getElementById("pluginConfigModal").classList.add("open");

    const api = getApi();
    if (api && m.id) {
      Promise.all([
        dynamicSelects.length && api.get_plugin_dynamic_options ? api.get_plugin_dynamic_options(m.id) : Promise.resolve(null),
        m.diagnostic_check && api.run_plugin_check ? api.run_plugin_check(m.id) : Promise.resolve(null),
      ]).then(([dynRes, chkRes]) => {
        if (!state.activePluginConfig || !state.activePluginConfig.manifest || state.activePluginConfig.manifest.id !== m.id) return;
        if (dynRes && dynRes.ok && dynRes.options_by_key) {
          dynamicSelects.forEach(({ key, sel, curVal }) => {
            const opts = dynRes.options_by_key[key];
            if (Array.isArray(opts) && opts.length) {
              const chosen = sel.value || curVal;
              sel.innerHTML = "";
              opts.forEach((opt) => {
                const o = document.createElement("option");
                o.value = opt.value;
                o.textContent = opt.label || opt.value;
                if (String(chosen) === String(opt.value)) o.selected = true;
                sel.appendChild(o);
              });
              if (chosen && !opts.some((opt) => String(opt.value) === String(chosen))) {
                const keepOpt = document.createElement("option");
                keepOpt.value = chosen;
                keepOpt.textContent = chosen;
                keepOpt.selected = true;
                sel.appendChild(keepOpt);
              }
            }
          });
        }
        if (banner && chkRes && chkRes.message) {
          banner.textContent = chkRes.message;
          banner.className = "plugin-cfg-banner " + (chkRes.status === "ok" ? "ok" : (chkRes.status === "warn" ? "warn" : ""));
          banner.style.display = "block";
        }
      }).catch(() => {});
    }
  }

  function renderPluginsView() {
    const installed = state.pluginsInstalled || [];
    const byId = {};
    installed.forEach((entry) => {
      if (entry.manifest && entry.manifest.id) byId[entry.manifest.id] = entry;
    });

    const activeCount = installed.filter((e) => e.valid && e.enabled).length;
    const dot = document.getElementById("pluginsDot");
    if (dot) dot.classList.toggle("show", activeCount > 0);

    const tabCatBtn = document.getElementById("btnPluginTabCatalog");
    const tabInstBtn = document.getElementById("btnPluginTabInstalled");
    const catPane = document.getElementById("pluginsCatalogPane");
    const instPane = document.getElementById("pluginsInstalledPane");
    if (tabCatBtn && tabInstBtn && catPane && instPane) {
      tabCatBtn.classList.toggle("active", state.pluginsTab === "catalog");
      tabInstBtn.classList.toggle("active", state.pluginsTab === "installed");
      catPane.style.display = state.pluginsTab === "catalog" ? "flex" : "none";
      instPane.style.display = state.pluginsTab === "installed" ? "flex" : "none";
    }

    document.querySelectorAll("#pluginCatBar .cat-chip").forEach((chip) => {
      chip.classList.toggle("active", chip.getAttribute("data-cat") === state.pluginsCategory);
    });

    // Render GitHub Catalog List
    const catHost = document.getElementById("pluginCatalogList");
    if (catHost) {
      catHost.innerHTML = "";
      const catalog = state.pluginsCatalog;
      if (!catalog) {
        catHost.innerHTML = `<div class="list-row"><span class="row-desc">${escapeHtml(t("loading_headline"))}</span></div>`;
      } else if (!catalog.ok) {
        catHost.innerHTML = `<div class="list-row"><span class="row-desc">${escapeHtml(catalog.error || "GitHub error")}</span></div>`;
      } else {
        const items = (catalog.plugins || []).filter((p) => {
          if (state.pluginsCategory === "all") return true;
          const caps = p.capabilities || [];
          return p.category === state.pluginsCategory || caps.includes(state.pluginsCategory);
        });
        if (!items.length) {
          catHost.innerHTML = `<div class="list-row"><span class="row-desc">${escapeHtml(t("plugins_empty_catalog"))}</span></div>`;
        } else {
          items.forEach((p) => {
            const existing = byId[p.id];
            const hasUpdate = Boolean(existing && existing.manifest && compareSemver(existing.manifest.version, p.version) < 0);
            const card = document.createElement("div");
            card.className = "plugin-card";

            const main = document.createElement("div");
            main.className = "plugin-main";
            const capsHtml = (p.capabilities || []).map((c) => `<span class="ext-tag">${escapeHtml(c)}</span>`).join("");
            const reqText = (p.requirements && p.requirements.summary) ? `<div class="plugin-req">${escapeHtml(p.requirements.summary)}</div>` : "";
            main.innerHTML = `
              <div class="plugin-head">
                <span class="plugin-name">${escapeHtml(p.name || p.id)}</span>
                <span class="plugin-ver mono">v${escapeHtml(p.version || "1.0.0")}</span>
                ${p.author ? `<span class="plugin-ver">by ${escapeHtml(p.author)}</span>` : ""}
                <div class="plugin-pills">${capsHtml}</div>
              </div>
              <div class="plugin-desc">${escapeHtml(p.description || "")}</div>
              ${reqText}
            `;
            card.appendChild(main);

            const side = document.createElement("div");
            side.className = "plugin-side";
            if (p.coming_soon) {
              const tag = document.createElement("span");
              tag.className = "ext-tag";
              tag.textContent = t("plugins_coming_soon");
              side.appendChild(tag);
            } else if (existing && !hasUpdate) {
              const cfgBtn = document.createElement("button");
              cfgBtn.type = "button";
              cfgBtn.className = "btn btn-sm";
              cfgBtn.textContent = t("plugins_cfg");
              cfgBtn.addEventListener("click", () => openPluginConfigModal(existing));
              side.appendChild(cfgBtn);

              const badge = document.createElement("span");
              badge.className = "sev-text ok";
              badge.textContent = `● ${t("plugins_installed_badge")}`;
              side.appendChild(badge);
            } else {
              const instBtn = document.createElement("button");
              instBtn.type = "button";
              instBtn.className = "btn btn-sm btn-primary";
              instBtn.textContent = t(hasUpdate ? "plugins_update" : "plugins_install");
              instBtn.addEventListener("click", async () => {
                const api = getApi();
                if (!api || !api.install_plugin) return;
                instBtn.disabled = true;
                showToast(t("toast_working"));
                try {
                  const res = await api.install_plugin(p.id, "");
                  showToast((res && res.message) || "OK");
                  await loadPluginsData(false);
                } finally {
                  instBtn.disabled = false;
                }
              });
              side.appendChild(instBtn);
            }
            card.appendChild(side);
            catHost.appendChild(card);
          });
        }
      }
    }

    // Render Installed Plugins List
    const instHost = document.getElementById("pluginInstalledList");
    if (instHost) {
      instHost.innerHTML = "";
      if (!installed.length) {
        instHost.innerHTML = `<div class="list-row"><span class="row-desc">${escapeHtml(t("plugins_empty_installed"))}</span></div>`;
      } else {
        installed.forEach((entry) => {
          const m = entry.manifest || {};
          const card = document.createElement("div");
          card.className = "plugin-card";
          const main = document.createElement("div");
          main.className = "plugin-main";
          const hooksCount = Object.keys(m.hooks || {}).length;
          const desc = entry.valid ? (m.description || "") : (entry.errors || []).join("; ");
          main.innerHTML = `
            <div class="plugin-head">
              <span class="plugin-name">${escapeHtml(m.name || m.id)}</span>
              ${m.version ? `<span class="plugin-ver mono">v${escapeHtml(m.version)}</span>` : ""}
              ${hooksCount ? `<span class="ext-tag">${hooksCount} hooks</span>` : ""}
            </div>
            <div class="plugin-desc">${escapeHtml(desc)}</div>
          `;
          card.appendChild(main);

          const side = document.createElement("div");
          side.className = "plugin-side";
          if (entry.valid) {
            const toggleBtn = document.createElement("button");
            toggleBtn.type = "button";
            toggleBtn.className = entry.enabled ? "btn btn-sm" : "btn btn-sm btn-primary";
            toggleBtn.textContent = t(entry.enabled ? "plugins_disable" : "plugins_enable");
            toggleBtn.addEventListener("click", async () => {
              const api = getApi();
              if (!api || !api.set_plugin_enabled) return;
              const res = await api.set_plugin_enabled(m.id, !entry.enabled);
              showToast((res && res.message) || "OK");
              await loadPluginsData(false);
            });
            side.appendChild(toggleBtn);

            const cfgBtn = document.createElement("button");
            cfgBtn.type = "button";
            cfgBtn.className = "btn btn-sm";
            cfgBtn.textContent = t("plugins_cfg");
            cfgBtn.addEventListener("click", () => openPluginConfigModal(entry));
            side.appendChild(cfgBtn);

            if (m.diagnostic_check) {
              const chkBtn = document.createElement("button");
              chkBtn.type = "button";
              chkBtn.className = "btn btn-sm";
              chkBtn.textContent = t("plugins_check");
              chkBtn.addEventListener("click", async () => {
                const api = getApi();
                if (!api || !api.run_plugin_check) return;
                const res = await api.run_plugin_check(m.id);
                showToast((res && res.message) || "OK");
              });
              side.appendChild(chkBtn);
            }
          }

          const rmBtn = document.createElement("button");
          rmBtn.type = "button";
          rmBtn.className = "btn btn-sm btn-danger";
          rmBtn.textContent = t("plugins_uninstall");
          rmBtn.addEventListener("click", async () => {
            const api = getApi();
            if (!api || !api.uninstall_plugin) return;
            const res = await api.uninstall_plugin(m.id, false);
            showToast((res && res.message) || "OK");
            await loadPluginsData(false);
          });
          side.appendChild(rmBtn);
          card.appendChild(side);
          instHost.appendChild(card);
        });
      }
    }
  }

  async function loadPluginsData(withCatalog) {
    const api = getApi();
    if (!api) return;
    const jobs = [
      api.list_plugins ? api.list_plugins() : Promise.resolve([]),
      api.get_plugin_badges ? api.get_plugin_badges() : Promise.resolve([]),
      withCatalog && api.fetch_plugin_catalog ? api.fetch_plugin_catalog("") : Promise.resolve(state.pluginsCatalog),
    ];
    const [inst, badges, cat] = await Promise.all(jobs);
    state.pluginsInstalled = Array.isArray(inst) ? inst : [];
    state.pluginBadges = Array.isArray(badges) ? badges : [];
    if (cat !== undefined && cat !== null) state.pluginsCatalog = cat;
    renderPluginsView();
    if (state.guideSteps && state.guideSteps.length) {
      const activeRep = state.transport === "wireless" ? state.wirelessReport : state.report;
      if (activeRep) updateWizardState(layerMapOf(activeRep), activeRep);
    }
  }

  async function showPluginHooksOverview() {
    const api = getApi();
    if (!api || !api.get_plugin_sdk_info) return;
    const info = await api.get_plugin_sdk_info();
    const lines = [
      `Schema: ${info.schema}`,
      `Registry: ${info.active_registry_url}`,
      "",
      "Lifecycle Hooks (13):",
      ...(info.hooks || []).map((h) => `  - ${h}`),
      "",
      "Supported Capabilities:",
      "  " + (info.capabilities || []).join(", "),
      "",
      "Declarative Host Commands:",
      "  " + (info.host_commands || []).join(", "),
    ];
    showDetailModal(t("plugins_hooks_title"), lines.join("\n"));
  }

  function toggleRecBackendRows(bk) {
    const obsRow = document.getElementById("recObsRow");
    const ffRow = document.getElementById("recFfmpegRow");
    const cuRow = document.getElementById("recCustomRow");
    const det = (state.recorderState && state.recorderState.detected) || {};
    const eff = bk === "auto" ? (det.effective_backend || "obs") : bk;
    if (obsRow) obsRow.style.display = (eff === "obs" || bk === "obs") ? "flex" : "none";
    if (ffRow) ffRow.style.display = (eff === "ffmpeg" || bk === "ffmpeg") ? "flex" : "none";
    if (cuRow) cuRow.style.display = (eff === "custom" || bk === "custom") ? "flex" : "none";
    updateSplitRowVisibility();
    updateLocalFilenamePreview();
  }

  function updateSplitRowVisibility() {
    const modeSel = document.getElementById("recSplitMode");
    const minWrap = document.getElementById("recSplitMinWrap");
    const sizeWrap = document.getElementById("recSplitSizeWrap");
    const fatBadge = document.getElementById("recSplitFat32Badge");
    if (!modeSel || !minWrap || !sizeWrap) return;
    const m = modeSel.value || "off";
    minWrap.style.display = (m === "time" || m === "both") ? "inline-flex" : "none";
    sizeWrap.style.display = (m === "size" || m === "both") ? "inline-flex" : "none";
    const rs = state.recorderState || {};
    const tgt = rs.target_status || {};
    const eff = rs.effective_split || {};
    if (fatBadge) {
      const isFat = String(tgt.filesystem || "").toUpperCase() === "FAT32" || Boolean(eff.fat32_guarded);
      fatBadge.style.display = isFat ? "inline-flex" : "none";
    }
  }

  function sanitizeClientBody(raw) {
    return String(raw || "")
      .replace(/\{seq\}_*/gi, "")
      .replace(/^\d+_+/, "")
      .replace(/[<>:"/\\|?*\x00-\x1f]+/g, "_")
      .replace(/\s+/g, "_")
      .replace(/_+/g, "_")
      .replace(/^[._\-\s]+|[._\-\s]+$/g, "")
      .slice(0, 96);
  }

  function updateLocalFilenamePreview() {
    const prevEl = document.getElementById("recFilenamePreview");
    const tplIn = document.getElementById("recFilenameTemplate");
    if (!prevEl || !tplIn) return;
    const rs = state.recorderState || {};
    const det = rs.detected || {};
    const cfg = (state.appState && state.appState.config) || {};
    const preset = cfg.active_preset || "xbox";
    const bk = (document.getElementById("recBackendSelect") || {}).value || "auto";
    const effBk = bk === "auto" ? (det.effective_backend || "obs") : bk;
    const modeVal = (document.getElementById("recModeSelect") || {}).value || "replay";
    const ext = effBk === "ffmpeg"
      ? ((document.getElementById("recFfFormat") || {}).value || "mkv")
      : ((det.obs && det.obs.record_format) || (document.getElementById("recFfFormat") || {}).value || "mkv");
    const now = new Date();
    const pad2 = (n) => String(n).padStart(2, "0");
    const dStr = `${now.getFullYear()}${pad2(now.getMonth() + 1)}${pad2(now.getDate())}`;
    const tStr = `${pad2(now.getHours())}${pad2(now.getMinutes())}${pad2(now.getSeconds())}`;
    let tpl = (tplIn.value || "").trim() || "{preset}_{date}_{time}";
    tpl = tpl
      .replace(/\{preset\}/gi, preset)
      .replace(/\{date\}/gi, dStr)
      .replace(/\{time\}/gi, tStr)
      .replace(/\{backend\}/gi, effBk)
      .replace(/\{mode\}/gi, modeVal)
      .replace(/\{orig\}/gi, "clip")
      .replace(/\{[a-z0-9_]+\}/gi, "");
    const body = sanitizeClientBody(tpl) || `${preset}_${dStr}_${tStr}`;
    const clips = (rs.clips_summary && rs.clips_summary.clips) || [];
    let maxSeq = 0;
    clips.forEach((c) => { if (typeof c.seq === "number" && c.seq > maxSeq) maxSeq = c.seq; });
    const n1 = maxSeq + 1;
    const lockEl = document.querySelector(".rec-seq-lock");
    if (lockEl) lockEl.textContent = `${n1}_`;
    prevEl.textContent = `${n1}_${body}.${ext} · ${n1 + 1}_…`;
  }

  function formatDurationSec(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const p = (v) => String(v).padStart(2, "0");
    return h > 0 ? `${h}:${p(m)}:${p(r)}` : `${p(m)}:${p(r)}`;
  }

  function syncRecorderTelemetryPolling() {
    const viewActive = Boolean(document.getElementById("viewRecorder") && document.getElementById("viewRecorder").classList.contains("active"));
    const isRec = Boolean(state.recorderState && state.recorderState.is_recording);
    if (viewActive && isRec) {
      if (!state.recorderPollTimer) {
        state.recorderPollTimer = setInterval(() => { loadRecorderData(); }, 2500);
      }
    } else if (state.recorderPollTimer) {
      clearInterval(state.recorderPollTimer);
      state.recorderPollTimer = null;
    }
  }

  async function autoSaveRecorderSettings(extraPatch, toastOnSuccess) {
    const api = getApi();
    if (!api || !api.save_recorder_settings) return;
    const patch = { ...collectRecorderSettingsPatch(), ...(extraPatch || {}) };
    const r = await api.save_recorder_settings(patch);
    if (toastOnSuccess) showToast((r && r.message) || t("toast_saved"));
    await loadRecorderData();
  }

  function updateCaptureScopeControls(rec, openWins, vidDevs, effBk, tgt) {
    const scopeSel = document.getElementById("recCaptureScope");
    const winSel = document.getElementById("recWindowSelect");
    const vidSel = document.getElementById("recVideoDevSelect");
    const strictWrap = document.getElementById("recStrictWindowWrap");
    const strictCb = document.getElementById("recStrictWindow");
    const hintBar = document.getElementById("recCaptureHintBar");
    const hintTxt = document.getElementById("recCaptureHintText");

    const scope = (scopeSel && scopeSel.value) || (rec && rec.capture_scope) || "stream_window";
    if (winSel) {
      const prevTitle = (rec && rec.capture_window_title) || winSel.value || "";
      const prevExe = (rec && rec.capture_window_exe) || "";
      winSel.innerHTML = `<option value="">${escapeHtml(t("rec_pick_window_ph"))}</option>`;
      (openWins || []).forEach((w) => {
        const opt = document.createElement("option");
        opt.value = w.title || "";
        opt.dataset.exe = w.process_name || "";
        const procTag = w.process_name ? ` [${w.process_name}]` : "";
        const minTag = w.is_minimized ? ` (${t("rec_win_minimized")})` : "";
        opt.textContent = `${w.title}${procTag}${minTag}`;
        if ((prevTitle && w.title === prevTitle) || (!prevTitle && prevExe && w.process_name === prevExe)) {
          opt.selected = true;
        }
        winSel.appendChild(opt);
      });
      winSel.style.display = scope === "window" ? "inline-block" : "none";
    }
    if (vidSel) {
      const prevDev = (rec && rec.capture_video_device) || vidSel.value || "";
      vidSel.innerHTML = `<option value="">${escapeHtml(t("rec_pick_card_auto"))}</option>`;
      (vidDevs || []).forEach((d) => {
        const opt = document.createElement("option");
        opt.value = d.name || "";
        opt.textContent = d.is_capture_card ? `★ ${d.name}` : d.name;
        if (prevDev && d.name === prevDev) opt.selected = true;
        vidSel.appendChild(opt);
      });
      vidSel.style.display = scope === "capture_card" ? "inline-block" : "none";
    }
    if (strictWrap && strictCb) {
      strictWrap.style.display = (scope === "stream_window" || scope === "window") ? "inline-flex" : "none";
    }
    if (hintBar && hintTxt) {
      const info = tgt || {};
      const effScope = info.effective_scope || scope;
      const targetLabel = info.label || "";
      let tone = "info";
      let msg = "";
      if (effScope === "blocked" || info.ready === false) {
        tone = "err";
        msg = info.warning || t("rec_hint_blocked");
      } else if (effScope === "capture_card") {
        tone = "ok";
        msg = t("rec_hint_card_ok").replace("{dev}", targetLabel || "Capture Card");
      } else if (effScope === "window") {
        if (effBk === "obs") {
          tone = info.is_minimized ? "warn" : "ok";
          msg = (info.is_minimized ? t("rec_hint_win_min") : t("rec_hint_obs_wgc")).replace("{win}", targetLabel);
        } else if (effBk === "ffmpeg") {
          tone = info.is_minimized ? "warn" : "ok";
          msg = (info.is_minimized ? t("rec_hint_win_min") : t("rec_hint_ff_win")).replace("{win}", targetLabel);
        } else {
          tone = "info";
          msg = t("rec_hint_focus_win").replace("{win}", targetLabel);
        }
      } else if (info.fell_back_to_desktop) {
        tone = "warn";
        msg = info.warning || t("rec_hint_fallback_desktop");
      } else {
        tone = "info";
        msg = t("rec_hint_desktop");
      }
      hintBar.className = `rec-capture-hint ${tone}`;
      hintTxt.textContent = msg;
    }
  }

  function renderRecorderView() {
    const rs = state.recorderState;
    const ctrlPane = document.getElementById("recControlPane");
    const storPane = document.getElementById("recStoragePane");
    const clipPane = document.getElementById("recClipsPane");
    const bC = document.getElementById("btnRecTabControl");
    const bS = document.getElementById("btnRecTabStorage");
    const bL = document.getElementById("btnRecTabClips");
    if (ctrlPane && storPane && clipPane && bC && bS && bL) {
      bC.classList.toggle("active", state.recorderTab === "control");
      bS.classList.toggle("active", state.recorderTab === "storage");
      bL.classList.toggle("active", state.recorderTab === "clips");
      ctrlPane.style.display = state.recorderTab === "control" ? "flex" : "none";
      storPane.style.display = state.recorderTab === "storage" ? "flex" : "none";
      clipPane.style.display = state.recorderTab === "clips" ? "flex" : "none";
    }
    if (!rs) return;

    const rec = rs.recorder || {};
    const det = rs.detected || {};
    const tgt = rs.target_status || {};
    const rt = rs.runtime_status || {};
    const effSplit = rs.effective_split || {};
    const isRec = Boolean(rs.is_recording);

    const led = document.getElementById("recLed");
    const dot = document.getElementById("recDot");
    const titleEl = document.getElementById("recStatusTitle");
    const metaEl = document.getElementById("recStatusMeta");
    const toggleBtn = document.getElementById("btnRecToggle");
    if (led) led.className = "status-led " + (isRec ? "ok" : "idle");
    if (dot) dot.classList.toggle("show", isRec || Boolean(rec.auto_link_quick_play));
    if (titleEl) titleEl.textContent = t(isRec ? "rec_status_active" : "rec_status_idle");
    if (toggleBtn) {
      toggleBtn.textContent = t(isRec ? "rec_btn_stop" : "rec_btn_start");
      toggleBtn.className = isRec ? "btn btn-sm btn-danger" : "btn btn-sm btn-primary";
    }
    const splitNowBtn = document.getElementById("btnRecSplitNow");
    if (splitNowBtn) splitNowBtn.style.display = isRec ? "inline-flex" : "none";
    if (metaEl) {
      if (isRec) {
        const segNum = rt.current_segment || 1;
        const durTxt = formatDurationSec(rt.segment_elapsed_sec || rt.elapsed_sec || 0);
        const mbTxt = rt.current_size_mb > 0 ? ` · ${rt.current_size_mb} MB` : "";
        const fbTxt = rt.used_fallback ? ` · [${t("rec_using_fallback")}]` : "";
        metaEl.textContent = `#${segNum} · ${durTxt}${mbTxt}${fbTxt} · ${(det.effective_backend || rec.backend || "auto").toUpperCase()}`;
      } else {
        const targetTxt = tgt.configured
          ? (tgt.online ? `${tgt.path} (${tgt.filesystem || ""} · ${tgt.free_gb} GB · ~${tgt.est_hours}h)` : `${tgt.path} [OFFLINE]`)
          : t("rec_target_unset");
        let splitTxt = "";
        if (effSplit.mode === "time") splitTxt = ` · ${effSplit.split_minutes}m`;
        else if (effSplit.mode === "size") splitTxt = ` · ${effSplit.split_size_mb}MB`;
        else if (effSplit.mode === "both") splitTxt = ` · ${effSplit.split_minutes}m/${effSplit.split_size_mb}MB`;
        metaEl.textContent = `${(det.effective_backend || rec.backend || "auto").toUpperCase()} | ${targetTxt}${splitTxt}`;
      }
    }

    const bkBadge = document.getElementById("recBackendBadge");
    if (bkBadge) bkBadge.textContent = (det.effective_backend || "auto").toUpperCase();
    const bkSel = document.getElementById("recBackendSelect");
    if (bkSel) bkSel.value = rec.backend || "auto";
    const scopeSel = document.getElementById("recCaptureScope");
    if (scopeSel) scopeSel.value = rec.capture_scope || "stream_window";
    const strictCb = document.getElementById("recStrictWindow");
    if (strictCb) strictCb.checked = Boolean(rec.strict_window_only);
    updateCaptureScopeControls(rec, rs.open_windows || [], rs.video_devices || [], det.effective_backend || rec.backend || "auto", rs.capture_target || {});
    document.getElementById("recModeSelect").value = rec.mode || "replay";
    document.getElementById("recAutoLink").checked = Boolean(rec.auto_link_quick_play);
    document.getElementById("recSplitMode").value = rec.split_mode || "off";
    document.getElementById("recSplitMinutes").value = String(rec.split_minutes || 30);
    document.getElementById("recSplitSizeMb").value = String(rec.split_size_mb || 2048);
    document.getElementById("recFilenameTemplate").value = rec.filename_template || "{preset}_{date}_{time}";
    document.getElementById("recFfFormat").value = rec.ffmpeg_format || "mkv";
    document.getElementById("recFfEncoder").value = rec.ffmpeg_encoder || "auto";
    const ffAudioEl = document.getElementById("recFfAudio");
    if (ffAudioEl) ffAudioEl.value = rec.ffmpeg_audio_device || "";
    document.getElementById("recFfBitrate").value = String(rec.ffmpeg_bitrate_mbps || 10);
    document.getElementById("recFfExe").value = rec.ffmpeg_exe_path || ((det.ffmpeg && det.ffmpeg.exe_path) || "");
    document.getElementById("recCustomExe").value = rec.custom_exe_path || "";
    document.getElementById("recHotkeyRecord").value = rec.hotkey_record || "";
    document.getElementById("recHotkeyReplay").value = rec.hotkey_replay || "";
    document.getElementById("recOutputDir").value = rec.output_dir || "";
    document.getElementById("recFallbackDir").value = rec.fallback_dir || "";
    document.getElementById("recAutoOffload").checked = rec.auto_offload !== false;
    document.getElementById("recApplyNamingOnOffload").checked = rec.apply_naming_on_offload !== false;
    document.getElementById("recAutoPrune").checked = Boolean(rec.auto_prune);
    document.getElementById("recMaxGb").value = String(rec.max_storage_gb || 0);

    const migBtn = document.getElementById("btnRecMigrateFallback");
    if (migBtn) {
      const pendingFb = Number(rs.fallback_pending_count || 0);
      if (pendingFb > 0 && tgt.online) {
        migBtn.style.display = "inline-flex";
        migBtn.textContent = `${t("rec_btn_migrate_fb")} (${pendingFb})`;
      } else {
        migBtn.style.display = "none";
      }
    }

    const obsMeta = document.getElementById("recObsMeta");
    const obsWarn = document.getElementById("recObsSyncWarn");
    if (obsMeta) {
      const obs = det.obs || {};
      if (obs.installed) {
        const sp = obs.split_enabled ? ` · split:${obs.split_type || "On"}` : "";
        obsMeta.textContent = `${obs.profile_name || "Default"} [${obs.record_format || "native"}${sp}] → ${obs.record_path || "-"}`;
        if (obsWarn) {
          const norm = (s) => String(s || "").trim().replace(/[\\/]+$/, "").toLowerCase();
          const needSync = Boolean(rec.output_dir && obs.record_path && norm(rec.output_dir) !== norm(obs.record_path));
          obsWarn.style.display = needSync ? "inline-flex" : "none";
        }
      } else {
        obsMeta.textContent = t("rec_obs_missing");
        if (obsWarn) obsWarn.style.display = "none";
      }
    }
    toggleRecBackendRows(rec.backend || "auto");
    syncRecorderTelemetryPolling();

    // Drives list
    const drivesHost = document.getElementById("recDrivesList");
    if (drivesHost) {
      drivesHost.innerHTML = "";
      (rs.drives || []).forEach((d) => {
        const row = document.createElement("div");
        row.className = "rec-drive-row";
        const warnTxt = d.supports_large_files ? "" : ` · ⚠ ${t("rec_fat32_warn")}`;
        const descTxt = t("rec_drive_meta")
          .replace("{type}", d.drive_type_label || "")
          .replace("{free}", String(d.free_gb))
          .replace("{total}", String(d.total_gb))
          .replace("{hours}", String(d.est_hours));
        row.innerHTML = `
          <div class="list-row-col">
            <div class="row-head">
              <span class="mono">${escapeHtml(d.device_id)}</span> ${escapeHtml(d.volume_name || d.drive_type_label)}
              <span class="ext-tag">${escapeHtml(d.filesystem)}</span>
              ${d.recommended ? `<span class="sev-text ok">● ${escapeHtml(t("rec_drive_rec"))}</span>` : ""}
            </div>
            <div class="row-desc">${escapeHtml(descTxt)}${escapeHtml(warnTxt)}</div>
          </div>
        `;
        const useBtn = document.createElement("button");
        useBtn.type = "button";
        useBtn.className = d.recommended ? "btn btn-sm btn-primary" : "btn btn-sm";
        useBtn.textContent = t("rec_btn_use_drive");
        useBtn.addEventListener("click", async () => {
          const api = getApi();
          if (!api || !api.save_recorder_settings) return;
          const nextDir = `${d.device_id}\\WireDeckClips`;
          document.getElementById("recOutputDir").value = nextDir;
          const patch = { output_dir: nextDir };
          if (!d.supports_large_files) {
            const curSplit = document.getElementById("recSplitMode").value || "off";
            const curMb = parseInt(document.getElementById("recSplitSizeMb").value, 10) || 2048;
            if (curSplit === "off") patch.split_mode = "size";
            else if (curSplit === "time") patch.split_mode = "both";
            if (curMb > 3800) patch.split_size_mb = 3800;
            else if (curSplit === "off" || curSplit === "time") patch.split_size_mb = Math.min(curMb, 3800);
          }
          const r = await api.save_recorder_settings(patch);
          showToast(!d.supports_large_files ? t("rec_toast_fat32_cap") : ((r && r.message) || t("toast_saved")));
          await loadRecorderData();
        });
        row.appendChild(useBtn);
        drivesHost.appendChild(row);
      });
    }

    // Clips library
    const summaryEl = document.getElementById("recClipsSummary");
    const clipsData = rs.clips_summary || { clips: [], total_count: 0, total_gb: 0 };
    if (summaryEl) {
      summaryEl.textContent = t("rec_clips_count")
        .replace("{count}", String(clipsData.total_count || 0))
        .replace("{gb}", String(clipsData.total_gb || 0));
    }
    const clipsHost = document.getElementById("recClipsList");
    if (clipsHost) {
      clipsHost.innerHTML = "";
      const clips = clipsData.clips || [];
      if (!clips.length) {
        clipsHost.innerHTML = `<div class="list-row"><span class="row-desc">${escapeHtml(t("rec_clips_empty"))}</span></div>`;
      } else {
        clips.forEach((c) => {
          const row = document.createElement("div");
          row.className = "rec-clip-row";
          const seqBadge = c.seq > 0 ? `<span class="ext-tag mono">#${c.seq}</span>` : "";
          row.innerHTML = `
            <div class="list-row-col">
              <div class="row-head">${seqBadge}<span class="ext-tag">${escapeHtml(String(c.ext || "").toUpperCase())}</span> ${escapeHtml(c.name)}</div>
              <div class="row-desc mono">${c.size_mb} MB · ${escapeHtml(c.mtime_iso || "")}</div>
            </div>
          `;
          const acts = document.createElement("div");
          acts.className = "row-actions";
          const playBtn = document.createElement("button");
          playBtn.type = "button";
          playBtn.className = "btn btn-sm btn-primary";
          playBtn.textContent = t("rec_clip_play");
          playBtn.addEventListener("click", () => getApi() && getApi().recorder_open_clip(c.path));
          acts.appendChild(playBtn);

          const renBtn = document.createElement("button");
          renBtn.type = "button";
          renBtn.className = "btn btn-sm";
          renBtn.textContent = t("rec_clip_rename");
          renBtn.addEventListener("click", () => {
            state.activeRenameClip = c;
            const seqNum = c.seq > 0 ? c.seq : 1;
            const dotIdx = c.name.lastIndexOf(".");
            const stem = dotIdx > 0 ? c.name.slice(0, dotIdx) : c.name;
            const extStr = dotIdx > 0 ? c.name.slice(dotIdx) : `.${c.ext || "mkv"}`;
            const body = stem.replace(/^\d+_/, "");
            document.getElementById("renameClipSeqPrefix").textContent = `${seqNum}_`;
            document.getElementById("renameClipInput").value = body;
            document.getElementById("renameClipExt").textContent = extStr;
            document.getElementById("renameClipModal").classList.add("open");
            document.getElementById("renameClipInput").focus();
          });
          acts.appendChild(renBtn);

          const revBtn = document.createElement("button");
          revBtn.type = "button";
          revBtn.className = "btn btn-sm";
          revBtn.textContent = t("rec_clip_reveal");
          revBtn.addEventListener("click", () => getApi() && getApi().recorder_reveal_clip(c.path));
          acts.appendChild(revBtn);

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger";
          delBtn.textContent = t("rec_clip_del");
          delBtn.addEventListener("click", async () => {
            const api = getApi();
            if (!api || !api.recorder_delete_clip) return;
            const res = await api.recorder_delete_clip(c.path);
            showToast((res && res.message) || "OK");
            await loadRecorderData();
          });
          acts.appendChild(delBtn);

          row.appendChild(acts);
          clipsHost.appendChild(row);
        });
      }
    }
  }

  function collectRecorderSettingsPatch() {
    const winSel = document.getElementById("recWindowSelect");
    const selOpt = winSel && winSel.selectedIndex >= 0 ? winSel.options[winSel.selectedIndex] : null;
    return {
      backend: document.getElementById("recBackendSelect").value,
      capture_scope: (document.getElementById("recCaptureScope") || {}).value || "stream_window",
      capture_window_title: winSel ? winSel.value.trim() : "",
      capture_window_exe: (selOpt && selOpt.dataset && selOpt.dataset.exe) ? selOpt.dataset.exe.trim() : "",
      capture_video_device: (document.getElementById("recVideoDevSelect") || {}).value || "",
      strict_window_only: Boolean(document.getElementById("recStrictWindow") && document.getElementById("recStrictWindow").checked),
      mode: document.getElementById("recModeSelect").value,
      auto_link_quick_play: Boolean(document.getElementById("recAutoLink").checked),
      split_mode: document.getElementById("recSplitMode").value,
      split_minutes: parseInt(document.getElementById("recSplitMinutes").value, 10) || 30,
      split_size_mb: parseInt(document.getElementById("recSplitSizeMb").value, 10) || 2048,
      filename_template: (document.getElementById("recFilenameTemplate").value || "").trim(),
      ffmpeg_format: document.getElementById("recFfFormat").value,
      ffmpeg_encoder: document.getElementById("recFfEncoder").value,
      ffmpeg_audio_device: (document.getElementById("recFfAudio") || {}).value || "",
      ffmpeg_bitrate_mbps: parseInt(document.getElementById("recFfBitrate").value, 10) || 10,
      ffmpeg_exe_path: document.getElementById("recFfExe").value.trim(),
      custom_exe_path: document.getElementById("recCustomExe").value.trim(),
      hotkey_record: document.getElementById("recHotkeyRecord").value.trim(),
      hotkey_replay: document.getElementById("recHotkeyReplay").value.trim(),
      output_dir: document.getElementById("recOutputDir").value.trim(),
      fallback_dir: document.getElementById("recFallbackDir").value.trim(),
      auto_offload: Boolean(document.getElementById("recAutoOffload").checked),
      apply_naming_on_offload: Boolean(document.getElementById("recApplyNamingOnOffload").checked),
      auto_prune: Boolean(document.getElementById("recAutoPrune").checked),
      max_storage_gb: parseFloat(document.getElementById("recMaxGb").value) || 0,
    };
  }

  async function loadRecorderData() {
    const api = getApi();
    if (!api || !api.get_recorder_state) return;
    state.recorderState = await api.get_recorder_state();
    renderRecorderView();
  }

  function renderUpdateModal(rep) {
    state.updateReport = rep || null;
    const cfg = (state.appState && state.appState.config) || {};
    const appInfo = (rep && rep.app) || {};
    const plgInfo = (rep && rep.plugins) || {};

    const modeTag = document.getElementById("updCoreModeTag");
    if (modeTag) {
      modeTag.textContent = t((state.appState && state.appState.is_portable) ? "upd_mode_portable" : "upd_mode_installer");
    }
    const verBadge = document.getElementById("updCoreVerBadge");
    if (verBadge) {
      const curV = appInfo.current_version || (state.appState && state.appState.app_version) || "0.2.2";
      const latV = appInfo.latest_version || "";
      verBadge.textContent = appInfo.has_update && latV ? `v${curV} → v${latV}` : `v${curV}`;
    }

    const statusBadge = document.getElementById("updCoreStatusBadge");
    const applyBtn = document.getElementById("btnUpdApplyCore");
    const notesBox = document.getElementById("updReleaseNotes");
    const shaTag = document.getElementById("updShaTag");
    const relBtn = document.getElementById("btnUpdOpenRelease");

    if (statusBadge) {
      if (!rep) {
        statusBadge.className = "upd-badge";
        statusBadge.textContent = t("upd_checking");
      } else if (!appInfo.ok) {
        statusBadge.className = "upd-badge err";
        statusBadge.textContent = appInfo.error || t("upd_check_failed");
      } else if (appInfo.has_update) {
        statusBadge.className = "upd-badge has-update";
        statusBadge.textContent = t("upd_new_available").replace("{v}", appInfo.latest_version || "");
      } else {
        statusBadge.className = "upd-badge";
        statusBadge.textContent = t("upd_up_to_date");
      }
    }

    if (notesBox) {
      const notes = String(appInfo.release_notes || "").trim();
      if (notes) {
        notesBox.textContent = notes;
        notesBox.style.display = "block";
      } else {
        notesBox.style.display = "none";
      }
    }

    if (shaTag) {
      if (appInfo.sha256_ready && appInfo.sha256) {
        shaTag.textContent = `SHA-256: ${appInfo.sha256.slice(0, 16)}… (${appInfo.asset_name || "pkg"})`;
      } else if (appInfo.has_update) {
        shaTag.textContent = t("upd_no_sha");
      } else {
        shaTag.textContent = `SHA-256: —`;
      }
    }

    if (applyBtn) {
      applyBtn.disabled = !(appInfo.ok && appInfo.has_update && appInfo.sha256_ready && appInfo.download_url);
    }
    if (relBtn) {
      relBtn.dataset.url = appInfo.release_url || "";
    }

    const dot = document.getElementById("updateDot");
    if (dot) dot.classList.toggle("show", Boolean(rep && rep.has_any_update));

    const plgListEl = document.getElementById("updPluginsList");
    const updAllPlgBtn = document.getElementById("btnUpdAllPlugins");
    const items = plgInfo.plugins || [];
    const updCount = Number(plgInfo.updates_count || 0);
    if (updAllPlgBtn) {
      updAllPlgBtn.disabled = updCount <= 0;
    }
    if (plgListEl) {
      plgListEl.innerHTML = "";
      if (!items.length) {
        const empty = document.createElement("div");
        empty.className = "row-desc";
        empty.textContent = t("upd_no_plugins_installed");
        plgListEl.appendChild(empty);
      } else {
        items.forEach((p) => {
          const row = document.createElement("div");
          row.className = "upd-plugin-row";
          row.innerHTML = `
            <div>
              <strong>${escapeHtml(p.name || p.id)}</strong>
              <span class="mono" style="margin-left:6px;color:var(--text-muted);">v${escapeHtml(p.current_version)}</span>
            </div>
            <span class="${p.has_update ? "upd-badge has-update" : "row-desc"}">
              ${p.has_update ? escapeHtml(`→ v${p.latest_version}`) : escapeHtml(t("upd_plugin_latest"))}
            </span>
          `;
          plgListEl.appendChild(row);
        });
      }
    }

    const chSel = document.getElementById("updChannel");
    const autoCb = document.getElementById("updAutoCheck");
    const feedIn = document.getElementById("updFeedUrl");
    if (chSel) chSel.value = cfg.update_channel || "stable";
    if (autoCb) autoCb.checked = cfg.auto_check_updates !== false;
    if (feedIn) feedIn.value = cfg.update_feed_url || "";
  }

  async function openUpdateModal(runCheckNow) {
    renderUpdateModal(state.updateReport);
    document.getElementById("updateModal").classList.add("open");
    if (runCheckNow || !state.updateReport) {
      await runUpdateCheck(false);
    }
  }

  async function runUpdateCheck(silentDotOnly) {
    const api = getApi();
    if (!api || !api.check_updates) return;
    if (!silentDotOnly) renderUpdateModal(null);
    try {
      const ch = (document.getElementById("updChannel") || {}).value || "";
      const url = (document.getElementById("updFeedUrl") || {}).value || "";
      const res = await api.check_updates(silentDotOnly ? "" : ch, silentDotOnly ? "" : url.trim());
      state.updateReport = res;
      const dot = document.getElementById("updateDot");
      if (dot) dot.classList.toggle("show", Boolean(res && res.has_any_update));
      if (!silentDotOnly) {
        renderUpdateModal(res);
      }
    } catch (err) {
      if (!silentDotOnly) showToast(String(err));
    }
  }

  function updateUninstallSummary() {
    const sumEl = document.getElementById("uninstSummaryText");
    if (!sumEl) return;
    const wdChecked = document.querySelectorAll(".uninst-wd-cb:checked").length;
    const assocOn = Boolean(document.getElementById("uninstAssocToggle") && document.getElementById("uninstAssocToggle").checked);
    const assocChecked = assocOn ? document.querySelectorAll(".uninst-assoc-cb:checked").length : 0;
    sumEl.textContent = t("uninst_summary")
      .replace("{wd}", String(wdChecked))
      .replace("{assoc}", String(assocChecked));
  }

  function renderUninstallModal(scan) {
    state.uninstallScan = scan || null;
    const metaEl = document.getElementById("uninstMetaLine");
    if (metaEl && scan) {
      const modeTxt = scan.is_portable ? t("upd_mode_portable") : t("upd_mode_installer");
      metaEl.textContent = `${modeTxt} · ${scan.exe_dir || ""}`;
    }

    const netRow = document.getElementById("uninstNetRow");
    const netCb = document.getElementById("uninstRestoreNetCb");
    const netDesc = document.getElementById("uninstNetDesc");
    const netInfo = (scan && scan.network_restore) || {};
    if (netRow && netCb) {
      if (netInfo.needed) {
        netRow.style.display = "block";
        netCb.checked = true;
        const parts = [];
        if (netInfo.nat_active) parts.push("WireDeckNat");
        if (netInfo.wifi_tuned) parts.push("Wi-Fi");
        if (netDesc) netDesc.textContent = `${t("uninst_net_active_prefix")} ${parts.join(" + ")}`;
      } else {
        netRow.style.display = "none";
        netCb.checked = false;
      }
    }

    const wdList = document.getElementById("uninstWdList");
    const wdItems = (scan && scan.wiredeck_items) || [];
    const wdTitleMap = {
      wd_shortcuts: ["uninst_item_shortcuts", "uninst_item_shortcuts_d"],
      wd_plugins: ["uninst_item_plugins", "uninst_item_plugins_d"],
      wd_userdata: ["uninst_item_userdata", "uninst_item_userdata_d"],
      wd_gpu_prefs: ["uninst_item_gpu", "uninst_item_gpu_d"],
    };
    if (wdList) {
      wdList.innerHTML = "";
      if (!wdItems.length) {
        const emp = document.createElement("div");
        emp.className = "uninst-check-item";
        emp.innerHTML = `<span class="row-desc">${escapeHtml(t("uninst_no_wd_items"))}</span>`;
        wdList.appendChild(emp);
      } else {
        wdItems.forEach((item) => {
          const [tk, dk] = wdTitleMap[item.id] || [item.id, item.id];
          const lbl = document.createElement("label");
          lbl.className = "uninst-check-item";
          const sz = Number(item.size_mb || 0) > 0 ? `${item.size_mb} MB` : `${item.count || 0}`;
          lbl.innerHTML = `
            <span class="uninst-check-left">
              <input type="checkbox" class="uninst-wd-cb" value="${escapeHtml(item.id)}" />
              <span>
                <div class="row-head">${escapeHtml(t(tk) || item.id)}</div>
                <div class="row-desc">${escapeHtml(t(dk) || (item.paths || []).join(", "))}</div>
              </span>
            </span>
            <span class="ext-tag mono">${escapeHtml(sz)}</span>
          `;
          wdList.appendChild(lbl);
        });
      }
    }

    // Companion apps section ALWAYS starts OFF & UNCHECKED!
    const assocToggle = document.getElementById("uninstAssocToggle");
    const assocList = document.getElementById("uninstAssocList");
    const assocSub = document.getElementById("uninstAssocSub");
    const assocApps = (scan && scan.associated_apps) || [];
    if (assocToggle) assocToggle.checked = false;
    if (assocList) {
      assocList.style.display = "none";
      assocList.innerHTML = "";
      if (!assocApps.length) {
        if (assocToggle) assocToggle.disabled = true;
        if (assocSub) assocSub.textContent = t("uninst_assoc_none");
      } else {
        if (assocToggle) assocToggle.disabled = false;
        if (assocSub) {
          assocSub.textContent = t("uninst_assoc_detected").replace("{n}", String(assocApps.length));
        }
        assocApps.forEach((app) => {
          const lbl = document.createElement("label");
          lbl.className = "uninst-check-item";
          const catTag = t(`uninst_cat_${app.category}`) || app.category;
          const verTxt = app.version ? `v${app.version}` : catTag;
          lbl.innerHTML = `
            <span class="uninst-check-left">
              <input type="checkbox" class="uninst-assoc-cb" value="${escapeHtml(app.id)}" />
              <span>
                <div class="row-head">
                  ${escapeHtml(app.name)}
                  ${app.used_by_wiredeck ? `<span class="ext-tag" style="margin-left:5px;">${escapeHtml(t("uninst_used_tag"))}</span>` : ""}
                </div>
                <div class="row-desc mono">${escapeHtml(app.install_location || t("uninst_official_uninstaller"))}</div>
              </span>
            </span>
            <span class="ext-tag">${escapeHtml(verTxt)}</span>
          `;
          assocList.appendChild(lbl);
        });
      }
    }

    const progBox = document.getElementById("uninstProgressBox");
    if (progBox) {
      progBox.style.display = "none";
      progBox.textContent = "";
    }
    document.querySelectorAll(".uninst-wd-cb, .uninst-assoc-cb").forEach((cb) => {
      cb.checked = false;
      cb.addEventListener("change", updateUninstallSummary);
    });
    updateUninstallSummary();
  }

  async function openUninstallWizard(standalone) {
    const api = getApi();
    if (standalone) {
      document.body.classList.add("uninstall-standalone");
    }
    document.getElementById("uninstallModal").classList.add("open");
    if (api && api.scan_uninstall_items) {
      const scan = await api.scan_uninstall_items();
      renderUninstallModal(scan);
    }
  }

  async function refreshAll(silent) {
    const api = getApi();
    if (!api) return;
    setBusy(true);
    try {
      const st = await api.get_state();
      state.appState = st;
      const verTextEl = document.getElementById("versionText");
      if (verTextEl) verTextEl.textContent = "v" + st.app_version;
      else document.getElementById("versionLabel").textContent = "v" + st.app_version;
      syncConfigDisplay(st.config);

      if (st.uninstall_wizard_mode) {
        await openUninstallWizard(true);
        return;
      }

      renderBridgeState(st.bridge);

      const activeT = st.preview_view === "wireless"
        ? "wireless"
        : (st.preview_view === "home" ? "wired" : state.transport);
      const wantHotspotCreds = activeT === "wireless" && (state.wirelessSubmode === "hotspot" || !state.hotspotCreds);
      const activeViewPlugins = document.getElementById("viewPlugins") && document.getElementById("viewPlugins").classList.contains("active");
      const activeViewRecorder = document.getElementById("viewRecorder") && document.getElementById("viewRecorder").classList.contains("active");
      const wantCatalog = Boolean(st.preview_view === "plugins" || activeViewPlugins);
      const wantRecorder = Boolean(st.preview_view === "recorder" || activeViewRecorder);
      const tasks = [
        activeT === "wired" ? api.diagnose() : Promise.resolve(null),
        activeT === "wireless" && api.diagnose_wireless ? api.diagnose_wireless() : Promise.resolve(null),
        wantHotspotCreds && api.get_hotspot_credentials ? api.get_hotspot_credentials() : Promise.resolve(null),
        api.list_extensions(),
        api.list_toolkits ? api.list_toolkits() : Promise.resolve([]),
        api.list_plugins ? api.list_plugins() : Promise.resolve([]),
        api.get_plugin_badges ? api.get_plugin_badges() : Promise.resolve([]),
        wantCatalog && api.fetch_plugin_catalog ? api.fetch_plugin_catalog("") : Promise.resolve(null),
        wantRecorder && api.get_recorder_state ? api.get_recorder_state() : Promise.resolve(null),
        api.get_system_health ? api.get_system_health() : Promise.resolve(null),
      ];
      const [rep, wRep, creds, exts, tools, plgInst, plgBadges, plgCat, recSt, sysHealth] = await Promise.all(tasks);
      if (creds && creds.ok) state.hotspotCreds = creds;
      if (Array.isArray(plgInst)) state.pluginsInstalled = plgInst;
      if (Array.isArray(plgBadges)) state.pluginBadges = plgBadges;
      if (plgCat) state.pluginsCatalog = plgCat;
      if (recSt && recSt.ok) state.recorderState = recSt;
      renderSystemHealthBadge(sysHealth);
      if (rep) {
        state.wiredStale = false;
        renderReport(rep);
      }
      if (wRep) {
        state.wirelessStale = false;
        renderWirelessReport(wRep);
      }
      renderExtensions(exts);
      renderToolkits(tools);
      renderPluginsView();
      renderRecorderView();

      if (!refreshAll._initialRouted) {
        refreshAll._initialRouted = true;
        if (st.preview_view) {
          await switchView(st.preview_view, true);
        } else if (state.transport === "wireless") {
          await switchView("wireless", true);
        }
        if (st.preview_state === "update") {
          await openUpdateModal(true);
        } else if (!st.preview_state && st.config && st.config.auto_check_updates !== false) {
          setTimeout(() => runUpdateCheck(true), 1200);
        }
      }
      if (st.show_onboarding && !state.onboardingDismissed) {
        openOnboardingModal();
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
        if (showPreview) box.style.display = "block";
      }
      showToast(t("ai_copied_hint"));
    } else {
      showToast(res.message || "Export failed");
    }
  }

  async function saveLanPeerIp(raw) {
    const api = getApi();
    if (!api) return true;
    const val = raw.trim();
    const res = await api.save_settings({ wireless_peer_ip: val });
    if (!res.ok) {
      showToast(res.message || res.code);
      return false;
    }
    document.getElementById("setWirelessPeerIp").value = val;
    return true;
  }

  function wireEvents() {
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.getAttribute("data-nav"), false));
    });
    document.getElementById("sbProfile").addEventListener("click", () => switchView("settings", true));
    const sbHealthBtn = document.getElementById("sbHealthBtn");
    if (sbHealthBtn) sbHealthBtn.addEventListener("click", openSystemHealthModal);

    document.getElementById("btnRefresh").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnWRefresh").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnWizRefresh").addEventListener("click", () => refreshAll(false));
    document.getElementById("btnAiRefresh").addEventListener("click", () => refreshAll(false));

    document.getElementById("btnEnableDirect").addEventListener("click", () => runAction("enable-direct", {}));
    document.getElementById("btnDisableDirect").addEventListener("click", () => runAction("disable-direct", {}));

    // Wireless submodes & credentials
    document.getElementById("btnSubHotspot").addEventListener("click", async () => {
      state.wirelessSubmode = "hotspot";
      const api = getApi();
      if (api && api.set_wireless_submode) await api.set_wireless_submode("hotspot");
      await refreshAll(true);
    });
    document.getElementById("btnSubLan").addEventListener("click", async () => {
      state.wirelessSubmode = "lan";
      const api = getApi();
      if (api && api.set_wireless_submode) await api.set_wireless_submode("lan");
      await refreshAll(true);
    });
    document.getElementById("btnWShowPass").addEventListener("click", () => {
      state.showHotspotPass = !state.showHotspotPass;
      if (state.wirelessReport) renderWirelessReport(state.wirelessReport);
    });
    document.getElementById("btnWCopyPass").addEventListener("click", async () => {
      const api = getApi();
      let pass = state.hotspotCreds && state.hotspotCreds.passphrase;
      if (!pass && api && api.get_hotspot_credentials) {
        const c = await api.get_hotspot_credentials();
        if (c && c.ok) {
          state.hotspotCreds = c;
          pass = c.passphrase;
        }
      }
      if (pass) {
        await copyText(pass);
        showToast(t("toast_copied"));
      }
    });
    document.getElementById("wLanPeerInput").addEventListener("blur", async (e) => {
      await saveLanPeerIp(e.target.value);
    });
    document.getElementById("wLanPeerInput").addEventListener("keydown", async (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (await saveLanPeerIp(e.target.value)) {
          document.getElementById("btnWTestJitter").click();
        }
      }
    });
    document.getElementById("btnWTestJitter").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.ping_wireless_peer_now) return;
      const inEl = document.getElementById("wLanPeerInput");
      if (!(await saveLanPeerIp(inEl.value))) return;
      const outEl = document.getElementById("wJitterResult");
      outEl.style.color = "var(--text-sec)";
      outEl.textContent = t("ping_testing");
      const r = await api.ping_wireless_peer_now(inEl.value.trim());
      if (r.ok) {
        outEl.style.color = "var(--ok)";
        outEl.textContent = `${r.avg_rtt_ms} ms (jitter ${r.jitter_ms} ms)`;
      } else {
        outEl.style.color = "var(--fail)";
        outEl.textContent = t("ping_fail");
      }
    });

    // AI actions
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
      const cmd = state.transport === "wireless" ? "wiredeck-cli.exe diagnose --transport wireless --json" : "wiredeck-cli.exe diagnose --json";
      await copyText(cmd);
      showToast(t("toast_copied"));
    });

    const launchFn = async () => {
      const api = getApi();
      if (!api) return;
      const r = await api.launch_stream_client();
      showToast(r.message || "OK");
    };
    document.getElementById("btnLaunchStream").addEventListener("click", launchFn);

    // Interactive Setup Guide navigation & mode switches
    const switchGuideMode = async (nextTransport, nextSubmode) => {
      const api = getApi();
      state.transport = nextTransport;
      if (nextSubmode) state.wirelessSubmode = nextSubmode;
      state.guideUserPicked = false;
      if (api && api.set_transport) await api.set_transport(nextTransport);
      if (nextSubmode && api && api.set_wireless_submode) await api.set_wireless_submode(nextSubmode);
      await refreshAll(true);
    };
    document.getElementById("btnGuideWired").addEventListener("click", () => switchGuideMode("wired", null));
    document.getElementById("btnGuideHotspot").addEventListener("click", () => switchGuideMode("wireless", "hotspot"));
    document.getElementById("btnGuideLan").addEventListener("click", () => switchGuideMode("wireless", "lan"));
    const guidePresetSel = document.getElementById("guidePresetSelect");
    if (guidePresetSel) {
      guidePresetSel.addEventListener("change", async (e) => {
        const api = getApi();
        if (!api || !api.switch_preset) return;
        state.wiredStale = true;
        state.wirelessStale = true;
        state.guideUserPicked = false;
        const res = await api.switch_preset(e.target.value);
        if (res && res.ok) await refreshAll(true);
      });
    }
    document.getElementById("btnGuidePrev").addEventListener("click", () => {
      if (state.guideStepIdx > 0) {
        state.guideUserPicked = true;
        state.guideStepIdx -= 1;
        renderCurrentGuide();
      }
    });
    document.getElementById("btnGuideNext").addEventListener("click", () => {
      const total = (state.guideSteps || []).length;
      if (state.guideStepIdx < total - 1) {
        state.guideUserPicked = true;
        state.guideStepIdx += 1;
        renderCurrentGuide();
      } else {
        switchView(state.transport === "wireless" ? "wireless" : "home", true);
      }
    });
    document.getElementById("btnGuideRecheck").addEventListener("click", () => refreshAll(false));

    document.getElementById("btnOpenExtFolder").addEventListener("click", () => getApi() && getApi().open_folder("extensions"));
    document.getElementById("btnOpenPluginsFolder").addEventListener("click", () => getApi() && getApi().open_folder("plugins"));

    // Recorder view controls & smooth auto-save
    document.getElementById("btnRecTabControl").addEventListener("click", () => { state.recorderTab = "control"; renderRecorderView(); });
    document.getElementById("btnRecTabStorage").addEventListener("click", () => { state.recorderTab = "storage"; renderRecorderView(); });
    document.getElementById("btnRecTabClips").addEventListener("click", async () => { state.recorderTab = "clips"; await loadRecorderData(); });
    document.getElementById("btnRecRefresh").addEventListener("click", async () => { await loadRecorderData(); showToast(t("toast_refreshed")); });
    document.getElementById("recBackendSelect").addEventListener("change", (e) => { toggleRecBackendRows(e.target.value); autoSaveRecorderSettings({}, false); });
    const capScopeEl = document.getElementById("recCaptureScope");
    if (capScopeEl) capScopeEl.addEventListener("change", () => autoSaveRecorderSettings({}, false));
    const strictEl = document.getElementById("recStrictWindow");
    if (strictEl) strictEl.addEventListener("change", () => autoSaveRecorderSettings({}, false));
    const winSelEl = document.getElementById("recWindowSelect");
    if (winSelEl) winSelEl.addEventListener("change", () => autoSaveRecorderSettings({}, false));
    const vidSelEl = document.getElementById("recVideoDevSelect");
    if (vidSelEl) vidSelEl.addEventListener("change", () => autoSaveRecorderSettings({}, false));
    const refSrcBtn = document.getElementById("btnRecRefreshSources");
    if (refSrcBtn) {
      refSrcBtn.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.list_capture_sources) return;
        const fresh = await api.list_capture_sources();
        if (fresh && fresh.ok && state.recorderState) {
          state.recorderState.open_windows = fresh.open_windows || [];
          state.recorderState.video_devices = fresh.video_devices || [];
          state.recorderState.capture_target = fresh.capture_target || {};
          renderRecorderView();
          showToast(t("toast_refreshed"));
        }
      });
    }
    const shotBtn = document.getElementById("btnRecScreenshot");
    if (shotBtn) {
      shotBtn.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.recorder_take_screenshot) return;
        shotBtn.disabled = true;
        try {
          const res = await api.recorder_take_screenshot();
          showToast((res && res.message) || "OK");
        } finally {
          shotBtn.disabled = false;
        }
      });
    }
    document.getElementById("recModeSelect").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recAutoLink").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recSplitMode").addEventListener("change", () => { updateSplitRowVisibility(); autoSaveRecorderSettings({}, false); });
    document.getElementById("recSplitMinutes").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recSplitSizeMb").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recFfFormat").addEventListener("change", () => { updateLocalFilenamePreview(); autoSaveRecorderSettings({}, false); });
    document.getElementById("recFfEncoder").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    const ffAudioSel = document.getElementById("recFfAudio");
    if (ffAudioSel) ffAudioSel.addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recAutoOffload").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recApplyNamingOnOffload").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recAutoPrune").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.getElementById("recFilenameTemplate").addEventListener("input", updateLocalFilenamePreview);
    document.getElementById("recFilenameTemplate").addEventListener("change", () => autoSaveRecorderSettings({}, false));
    document.querySelectorAll("[data-rec-token]").forEach((chip) => {
      chip.addEventListener("click", () => {
        const tok = chip.getAttribute("data-rec-token") || "";
        const inp = document.getElementById("recFilenameTemplate");
        if (!inp || !tok) return;
        const cur = inp.value.trim();
        inp.value = cur ? (cur.endsWith("_") ? `${cur}${tok}` : `${cur}_${tok}`) : tok;
        updateLocalFilenamePreview();
        autoSaveRecorderSettings({}, false);
      });
    });
    document.getElementById("btnRecToggle").addEventListener("click", async () => {
      const api = getApi();
      if (!api) return;
      const isRec = Boolean(state.recorderState && state.recorderState.is_recording);
      if (!isRec) await autoSaveRecorderSettings({}, false);
      const res = isRec ? await api.recorder_stop() : await api.recorder_start();
      showToast((res && res.message) || "OK");
      await loadRecorderData();
    });
    const splitBtn = document.getElementById("btnRecSplitNow");
    if (splitBtn) {
      splitBtn.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.recorder_split_now) return;
        const res = await api.recorder_split_now();
        showToast((res && res.message) || "OK");
        await loadRecorderData();
      });
    }
    document.getElementById("btnRecReplay").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.recorder_save_replay) return;
      const res = await api.recorder_save_replay();
      showToast((res && res.message) || "OK");
    });
    document.getElementById("btnRecOpenDir").addEventListener("click", () => getApi() && getApi().open_folder("recorder"));
    document.getElementById("btnRecSyncObs").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.recorder_sync_obs_path) return;
      await api.save_recorder_settings(collectRecorderSettingsPatch());
      const res = await api.recorder_sync_obs_path();
      showToast((res && res.message) || "OK");
      await loadRecorderData();
    });
    const saveRecFn = async () => {
      const api = getApi();
      if (!api || !api.save_recorder_settings) return;
      const res = await api.save_recorder_settings(collectRecorderSettingsPatch());
      showToast((res && res.message) || t("toast_saved"));
      await loadRecorderData();
    };
    document.getElementById("btnRecSaveControl").addEventListener("click", saveRecFn);
    document.getElementById("btnRecSaveStorage").addEventListener("click", saveRecFn);
    document.getElementById("btnRecBrowseDir").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.pick_recorder_folder) return;
      const picked = await api.pick_recorder_folder();
      if (picked && picked.ok && picked.path) {
        document.getElementById("recOutputDir").value = picked.path;
        await autoSaveRecorderSettings({ output_dir: picked.path }, true);
      }
    });
    document.getElementById("btnRecBrowseFallback").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.pick_recorder_folder) return;
      const picked = await api.pick_recorder_folder();
      if (picked && picked.ok && picked.path) {
        document.getElementById("recFallbackDir").value = picked.path;
        await autoSaveRecorderSettings({ fallback_dir: picked.path }, true);
      }
    });
    document.getElementById("btnRecMigrateFallback").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.recorder_migrate_fallback) return;
      showToast(t("toast_working"));
      const res = await api.recorder_migrate_fallback();
      showToast((res && res.message) || "OK");
      await loadRecorderData();
    });
    document.getElementById("btnRenameClipCancel").addEventListener("click", () => {
      document.getElementById("renameClipModal").classList.remove("open");
      state.activeRenameClip = null;
    });
    const submitRenameFn = async () => {
      const api = getApi();
      const clip = state.activeRenameClip;
      if (!api || !api.recorder_rename_clip || !clip) return;
      const newBody = (document.getElementById("renameClipInput").value || "").trim();
      const res = await api.recorder_rename_clip(clip.path, newBody);
      showToast((res && res.message) || "OK");
      if (res && res.ok) {
        document.getElementById("renameClipModal").classList.remove("open");
        state.activeRenameClip = null;
        await loadRecorderData();
      }
    };
    document.getElementById("btnRenameClipOk").addEventListener("click", submitRenameFn);
    document.getElementById("renameClipInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitRenameFn(); }
    });
    document.getElementById("btnRecPickFf").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.pick_recorder_exe) return;
      const picked = await api.pick_recorder_exe("ffmpeg");
      if (picked && picked.ok && picked.path) document.getElementById("recFfExe").value = picked.path;
    });
    document.getElementById("btnRecPickCustom").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.pick_recorder_exe) return;
      const picked = await api.pick_recorder_exe("custom");
      if (picked && picked.ok && picked.path) document.getElementById("recCustomExe").value = picked.path;
    });
    document.getElementById("btnRecOffloadNow").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.recorder_offload_now) return;
      await api.save_recorder_settings(collectRecorderSettingsPatch());
      const res = await api.recorder_offload_now();
      showToast((res && res.message) || "OK");
      await loadRecorderData();
    });
    document.getElementById("btnRecPruneNow").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.recorder_prune_now) return;
      await api.save_recorder_settings(collectRecorderSettingsPatch());
      const res = await api.recorder_prune_now();
      showToast((res && res.message) || "OK");
      await loadRecorderData();
    });
    document.getElementById("btnPluginTabCatalog").addEventListener("click", () => {
      state.pluginsTab = "catalog";
      renderPluginsView();
      if (!state.pluginsCatalog) loadPluginsData(true);
    });
    document.getElementById("btnPluginTabInstalled").addEventListener("click", () => {
      state.pluginsTab = "installed";
      renderPluginsView();
    });
    document.getElementById("btnPluginsRefresh").addEventListener("click", async () => {
      await loadPluginsData(true);
      showToast(t("toast_refreshed"));
    });
    document.querySelectorAll("#pluginCatBar .cat-chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        state.pluginsCategory = chip.getAttribute("data-cat") || "all";
        renderPluginsView();
      });
    });
    document.getElementById("btnInstallDirectUrl").addEventListener("click", async () => {
      const api = getApi();
      const inp = document.getElementById("pluginDirectUrlInput");
      const val = (inp && inp.value || "").trim();
      if (!api || !api.install_plugin || !val) return;
      showToast(t("toast_working"));
      const res = await api.install_plugin("", val);
      showToast((res && res.message) || "OK");
      if (res && res.ok) {
        inp.value = "";
        state.pluginsTab = "installed";
        await loadPluginsData(false);
      }
    });
    document.getElementById("btnPluginHooksOverview").addEventListener("click", showPluginHooksOverview);
    document.getElementById("btnPluginCfgCancel").addEventListener("click", () => {
      document.getElementById("pluginConfigModal").classList.remove("open");
      state.activePluginConfig = null;
    });
    document.getElementById("btnPluginCfgSave").addEventListener("click", async () => {
      const api = getApi();
      const entry = state.activePluginConfig;
      if (!api || !api.save_plugin_settings || !entry || !entry.manifest) return;
      const patch = collectPluginFormSettings(entry.manifest);
      const res = await api.save_plugin_settings(entry.manifest.id, patch);
      document.getElementById("pluginConfigModal").classList.remove("open");
      state.activePluginConfig = null;
      showToast((res && res.message) || t("toast_saved"));
      await loadPluginsData(false);
    });
    document.getElementById("btnPluginRegistryCfg").addEventListener("click", () => {
      const cfg = (state.appState && state.appState.config) || {};
      document.getElementById("pluginRegistryInput").value = cfg.plugin_registry_url || "";
      document.getElementById("pluginRegistryModal").classList.add("open");
    });
    document.getElementById("btnPluginRegCancel").addEventListener("click", () => {
      document.getElementById("pluginRegistryModal").classList.remove("open");
    });
    document.getElementById("btnPluginRegReset").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.set_plugin_registry_url) return;
      await api.set_plugin_registry_url("");
      document.getElementById("pluginRegistryModal").classList.remove("open");
      await loadPluginsData(true);
      showToast(t("toast_saved"));
    });
    document.getElementById("btnPluginRegSave").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.set_plugin_registry_url) return;
      const url = document.getElementById("pluginRegistryInput").value.trim();
      await api.set_plugin_registry_url(url);
      document.getElementById("pluginRegistryModal").classList.remove("open");
      await loadPluginsData(true);
      showToast(t("toast_saved"));
    });
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

    // Startup Onboarding modal controls
    document.querySelectorAll("[data-onb-lang]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const lang = btn.getAttribute("data-onb-lang");
        state.onbSelectedLang = lang;
        state.lang = lang;
        applyI18n();
        renderOnboardingModal();
        if (state.report) renderReport(state.report);
        if (state.wirelessReport) renderWirelessReport(state.wirelessReport);
      });
    });
    document.querySelectorAll("[data-onb-transport]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.onbSelectedTransport = btn.getAttribute("data-onb-transport");
        renderOnboardingModal();
      });
    });
    const finishOnboarding = async (targetView) => {
      const api = getApi();
      state.onboardingDismissed = true;
      document.getElementById("onboardingModal").classList.remove("open");
      if (api && api.complete_onboarding) {
        state.wiredStale = true;
        state.wirelessStale = true;
        state.guideUserPicked = false;
        state.guideStepIdx = 0;
        await api.complete_onboarding(state.onbSelectedLang, state.onbSelectedPreset, state.onbSelectedTransport);
        await refreshAll(true);
        await switchView(targetView, true);
      }
    };
    document.getElementById("btnOnbStart").addEventListener("click", () => finishOnboarding("wizard"));
    document.getElementById("btnOnbSkip").addEventListener("click", () => {
      finishOnboarding(state.onbSelectedTransport === "wireless" ? "wireless" : "home");
    });

    // Settings instant switches
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
        if (state.wirelessReport) renderWirelessReport(state.wirelessReport);
        if (state.toolkits.length) renderToolkits(state.toolkits);
      }
    });
    const saveBandFn = async (nextBand) => {
      const api = getApi();
      if (!api || !api.save_settings) return;
      const setHbEl = document.getElementById("setHotspotBand");
      const wHbEl = document.getElementById("wHotspotBandSel");
      if (setHbEl) setHbEl.value = nextBand;
      if (wHbEl) wHbEl.value = nextBand;
      await api.save_settings({ hotspot_band: nextBand });
      const w3 = (state.wirelessReport && layerMapOf(state.wirelessReport).W3) || null;
      const hsOnNow = Boolean(w3 && w3.metrics && w3.metrics.hotspot_state === "On");
      showToast(t(hsOnNow ? "w_toast_band_saved_restart" : "toast_saved"));
      state.wirelessStale = true;
      await refreshAll(true);
    };
    const wHbSel = document.getElementById("wHotspotBandSel");
    if (wHbSel) wHbSel.addEventListener("change", (e) => saveBandFn(e.target.value));
    const setHbSel = document.getElementById("setHotspotBand");
    if (setHbSel) setHbSel.addEventListener("change", (e) => saveBandFn(e.target.value));
    document.getElementById("setProfile").addEventListener("change", async (e) => {
      const api = getApi();
      if (!api || !api.switch_preset) return;
      state.wiredStale = true;
      state.wirelessStale = true;
      const res = await api.switch_preset(e.target.value);
      if (res && res.ok) await refreshAll(true);
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
        wifi_interface: document.getElementById("setWifi").value,
        hotspot_band: (document.getElementById("setHotspotBand") || {}).value || "5ghz",
        keep_awake_during_stream: Boolean(document.getElementById("setKeepAwake") ? document.getElementById("setKeepAwake").checked : true),
        auto_restore_on_exit: Boolean(document.getElementById("setAutoRestoreExit") ? document.getElementById("setAutoRestoreExit").checked : false),
        host_ip: document.getElementById("setHostIp").value.trim(),
        peer_ip: document.getElementById("setPeerIp").value.trim(),
        dns_servers: document.getElementById("setDns").value.trim(),
        wireless_peer_ip: document.getElementById("setWirelessPeerIp").value.trim(),
      };
      const res = await api.save_settings(patch);
      if (!res.ok) {
        showToast(res.message || res.code);
        return;
      }
      showToast(t("toast_saved"));
      state.wiredStale = true;
      state.wirelessStale = true;
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
        if (jr && (jr.message || jr.code)) showToast(jr.message || jr.code);
        await refreshAll(true);
      }
    });

    // Update Center events
    document.getElementById("versionLabel").addEventListener("click", () => openUpdateModal(true));
    document.getElementById("btnOpenUpdates").addEventListener("click", () => openUpdateModal(true));

    // Link Stability Benchmark events
    const runBenchmarkFor = async (tMode) => {
      const api = getApi();
      if (!api || !api.run_link_benchmark) return;
      showToast(t("bench_running"));
      const bm = await api.run_link_benchmark(tMode);
      if (!bm) return;
      const lines = [
        `${t("bench_grade")}: ${bm.grade || "-"} (${bm.transport || tMode} -> ${bm.target_ip || "-"})`,
        `${t("bench_avg")}: ${bm.avg_ms != null ? bm.avg_ms + " ms" : "-"}  |  Min/Max: ${bm.min_ms != null ? bm.min_ms : "-"}/${bm.max_ms != null ? bm.max_ms : "-"} ms`,
        `${t("bench_jitter")}: ${bm.jitter_ms != null ? bm.jitter_ms + " ms" : "-"}  |  ${t("bench_loss")}: ${bm.loss_pct != null ? bm.loss_pct + "%" : "-"} (${bm.received || 0}/${bm.sent || 12})`,
        `${t("bench_rec_bitrate")}: ${bm.recommended_bitrate || "-"}`,
        "",
        bm.summary || "",
      ];
      openTextModal(t("bench_modal_title"), lines.join("\n"));
    };
    const hbBtn = document.getElementById("btnHomeBenchmark");
    if (hbBtn) hbBtn.addEventListener("click", () => runBenchmarkFor("wired"));
    const wbBtn = document.getElementById("btnWirelessBenchmark");
    if (wbBtn) wbBtn.addEventListener("click", () => runBenchmarkFor("wireless"));

    // Config & Plugin Backup / Restore events
    const bkBtn = document.getElementById("btnOpenBackup");
    if (bkBtn) bkBtn.addEventListener("click", () => document.getElementById("backupModal").classList.add("open"));
    const bkClose = document.getElementById("btnBackupClose");
    if (bkClose) bkClose.addEventListener("click", () => document.getElementById("backupModal").classList.remove("open"));
    const bkExp = document.getElementById("btnBackupExport");
    if (bkExp) {
      bkExp.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.export_config_backup) return;
        const r = await api.export_config_backup();
        if (r && r.ok) {
          showToast(t("backup_exported").replace("{n}", String(r.plugin_config_count || 0)));
          document.getElementById("backupModal").classList.remove("open");
        } else if (r && r.code !== "CANCELLED") {
          showToast((r && r.message) || "Export failed");
        }
      });
    }
    const bkImp = document.getElementById("btnBackupImport");
    if (bkImp) {
      bkImp.addEventListener("click", async () => {
        const api = getApi();
        if (!api || !api.import_config_backup) return;
        const r = await api.import_config_backup();
        if (r && r.ok) {
          showToast(t("backup_imported").replace("{n}", String(r.restored_plugin_configs || 0)));
          document.getElementById("backupModal").classList.remove("open");
          state.wiredStale = true;
          state.wirelessStale = true;
          await refreshAll(true);
        } else if (r && r.code !== "CANCELLED") {
          showToast((r && r.message) || "Import failed");
        }
      });
    }
    document.getElementById("btnUpdClose").addEventListener("click", () => {
      document.getElementById("updateModal").classList.remove("open");
    });
    document.getElementById("btnUpdRecheck").addEventListener("click", () => runUpdateCheck(false));
    document.getElementById("btnUpdOpenRelease").addEventListener("click", () => {
      const api = getApi();
      const btn = document.getElementById("btnUpdOpenRelease");
      if (api && api.open_release_url) api.open_release_url((btn && btn.dataset.url) || "");
    });
    document.getElementById("btnUpdSaveCfg").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.save_update_settings) return;
      const patch = {
        update_channel: (document.getElementById("updChannel") || {}).value || "stable",
        update_feed_url: ((document.getElementById("updFeedUrl") || {}).value || "").trim(),
        auto_check_updates: Boolean(document.getElementById("updAutoCheck") && document.getElementById("updAutoCheck").checked),
      };
      await api.save_update_settings(patch);
      showToast(t("toast_saved"));
      await runUpdateCheck(false);
    });
    document.getElementById("btnUpdApplyCore").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.apply_core_update) return;
      const btn = document.getElementById("btnUpdApplyCore");
      btn.disabled = true;
      showToast(t("upd_downloading"));
      try {
        const res = await api.apply_core_update((state.updateReport && state.updateReport.app) || null);
        showToast((res && res.message) || "OK");
      } finally {
        btn.disabled = false;
      }
    });
    document.getElementById("btnUpdAllPlugins").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.update_all_plugins) return;
      const btn = document.getElementById("btnUpdAllPlugins");
      btn.disabled = true;
      showToast(t("toast_working"));
      try {
        const res = await api.update_all_plugins(null);
        showToast((res && res.message) || "OK");
        await loadPluginsData(false);
        await runUpdateCheck(false);
      } finally {
        btn.disabled = false;
      }
    });

    // Interactive Uninstaller events
    document.getElementById("btnOpenUninstall").addEventListener("click", () => openUninstallWizard(false));
    document.getElementById("uninstAssocToggle").addEventListener("change", (e) => {
      const on = Boolean(e.target.checked);
      const listEl = document.getElementById("uninstAssocList");
      if (listEl) listEl.style.display = on ? "block" : "none";
      if (!on) {
        document.querySelectorAll(".uninst-assoc-cb").forEach((cb) => {
          cb.checked = false;
        });
      }
      updateUninstallSummary();
    });
    document.getElementById("btnUninstCancel").addEventListener("click", async () => {
      const api = getApi();
      if (document.body.classList.contains("uninstall-standalone") && api && api.cancel_uninstall_wizard) {
        await api.cancel_uninstall_wizard();
      } else {
        document.getElementById("uninstallModal").classList.remove("open");
      }
    });
    document.getElementById("btnUninstConfirm").addEventListener("click", async () => {
      const api = getApi();
      if (!api || !api.execute_uninstall) return;
      const btn = document.getElementById("btnUninstConfirm");
      const cancelBtn = document.getElementById("btnUninstCancel");
      const progBox = document.getElementById("uninstProgressBox");

      const selected = [];
      document.querySelectorAll(".uninst-wd-cb:checked").forEach((cb) => selected.push(cb.value));
      const assocOn = Boolean(document.getElementById("uninstAssocToggle") && document.getElementById("uninstAssocToggle").checked);
      if (assocOn) {
        document.querySelectorAll(".uninst-assoc-cb:checked").forEach((cb) => selected.push(cb.value));
      }
      const restoreNet = Boolean(document.getElementById("uninstRestoreNetCb") && document.getElementById("uninstRestoreNetCb").checked);

      btn.disabled = true;
      cancelBtn.disabled = true;
      if (progBox) {
        progBox.style.display = "block";
        progBox.textContent = t("uninst_running");
      }
      try {
        const res = await api.execute_uninstall({ selected_ids: selected, restore_network: restoreNet });
        const lines = [res.message || "OK"];
        ((res.details && res.details.executed) || []).forEach((it) => lines.push(`✓ ${it.message}`));
        ((res.details && res.details.failed) || []).forEach((it) => lines.push(`! ${it.message}`));
        if (res.self_uninstall && res.self_uninstall.message) {
          lines.push(`→ ${res.self_uninstall.message}`);
        }
        if (progBox) {
          progBox.innerHTML = lines.map((l) => `<div>${escapeHtml(l)}</div>`).join("");
        }
        showToast(res.message || "OK");
      } finally {
        btn.disabled = false;
        cancelBtn.disabled = false;
      }
    });
  }

  window.addEventListener("DOMContentLoaded", () => {
    wireEvents();
    applyI18n();
    renderLoadingSkeleton("diagTable", ["node_nic", "node_cable", "node_gate", "node_share", "node_console"]);
    renderLoadingSkeleton("wDiagTable", ["w_node_nic", "w_node_rf", "w_node_net", "w_node_gate", "w_node_device"]);
  });

  window.addEventListener("pywebviewready", () => {
    refreshAll(true);
  });
})();
