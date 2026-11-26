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
      WIFI_BAND_24GHZ: ["目前連在 2.4 GHz 頻段", "2.4 GHz 易受干擾導致畫面卡頓，建議切換 5 GHz 或開 5GHz 熱點"],
      WIFI_SIGNAL_WEAK: ["無線訊號強度偏弱", "建議靠近路由器或開啟 5 GHz 電腦熱點直連"],
      WIFI_LINK_SLOW: ["無線協商速率偏低", "高碼率串流建議使用 5 GHz / Wi-Fi 6 頻段"],
      WIRED_NAT_CONFLICT: ["有線 WinNAT 運作中", "有線分享與行動熱點互斥，請先關閉有線直連"],
      HOTSPOT_UNSUPPORTED: ["目前無法開啟行動熱點", "請確認電腦已連網且系統原則允許開啟熱點"],
      HOTSPOT_BAND_24GHZ: ["熱點正運作於 2.4 GHz", "本機支援 5 GHz 熱點，點右側一鍵切換至 5 GHz"],
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
      WIFI_BAND_24GHZ: ["Connected on 2.4 GHz", "2.4 GHz suffers heavy interference; switch to 5 GHz or PC Hotspot"],
      WIFI_SIGNAL_WEAK: ["Weak Wi-Fi Signal", "Move closer to router or start PC 5 GHz Hotspot for direct link"],
      WIFI_LINK_SLOW: ["Low Wi-Fi PHY Rate", "Use 5 GHz / Wi-Fi 6 for smooth high-bitrate game streaming"],
      WIRED_NAT_CONFLICT: ["Wired WinNAT Active", "Wired WireDeckNat conflicts with Mobile Hotspot; stop Wired first"],
      HOTSPOT_UNSUPPORTED: ["Mobile Hotspot Unavailable", "Ensure internet connection exists and hotspot policy allows sharing"],
      HOTSPOT_BAND_24GHZ: ["Hotspot Running 2.4 GHz", "5 GHz hotspot is supported; click Fix Now to lock 5 GHz band"],
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
      WIFI_BAND_24GHZ: ["2.4 GHz 帯で接続中", "2.4 GHz は干渉が多いため 5 GHz または 5G ホットスポットを推奨します"],
      WIFI_SIGNAL_WEAK: ["Wi-Fi 電波が弱い", "ルーターに近づくか、PC の 5 GHz ホットスポットをご利用ください"],
      WIFI_LINK_SLOW: ["リンク速度が低下中", "高画質ストリーミングには 5 GHz / Wi-Fi 6 接続を推奨します"],
      WIRED_NAT_CONFLICT: ["有線 WinNAT が稼働中", "ホットスポットと競合するため、先に有線直結を停止してください"],
      HOTSPOT_UNSUPPORTED: ["モバイルホットスポット不可", "PC のネット接続と共有ポリシーを確認してください"],
      HOTSPOT_BAND_24GHZ: ["2.4 GHz ホットスポット稼働中", "5 GHz 帯に対応しています。ワンクリック修復で 5 GHz に切り替えます"],
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
    if (hsOn) metaParts.push(t("w_detail_hotspot_on"));
    else if (antiOn) metaParts.push(t("w_detail_opt_on"));
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
    document.getElementById("setHotspot5G").checked = cfg.hotspot_prefer_5ghz !== false;
    document.getElementById("wLanPeerInput").value = cfg.wireless_peer_ip || "";
    updateStatusBar(cfg);
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

      const activeT = st.preview_view === "wireless"
        ? "wireless"
        : (st.preview_view === "home" ? "wired" : state.transport);
      const wantHotspotCreds = activeT === "wireless" && (state.wirelessSubmode === "hotspot" || !state.hotspotCreds);
      const tasks = [
        activeT === "wired" ? api.diagnose() : Promise.resolve(null),
        activeT === "wireless" && api.diagnose_wireless ? api.diagnose_wireless() : Promise.resolve(null),
        wantHotspotCreds && api.get_hotspot_credentials ? api.get_hotspot_credentials() : Promise.resolve(null),
        api.list_extensions(),
        api.list_toolkits ? api.list_toolkits() : Promise.resolve([]),
      ];
      const [rep, wRep, creds, exts, tools] = await Promise.all(tasks);
      if (creds && creds.ok) state.hotspotCreds = creds;
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

      if (!refreshAll._initialRouted) {
        refreshAll._initialRouted = true;
        if (st.preview_view) {
          await switchView(st.preview_view, true);
        } else if (state.transport === "wireless") {
          await switchView("wireless", true);
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
        hotspot_prefer_5ghz: Boolean(document.getElementById("setHotspot5G").checked),
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
