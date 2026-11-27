"""Privileged wireless actions: 5 GHz Mobile Hotspot toggle & reversible Wi-Fi anti-stutter optimization."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from wiredeck.config import AppConfig
from wiredeck.models import ActionResult
from wiredeck.paths import AppPaths
from wiredeck.ps_runner import PsResult, run_powershell
from wiredeck.version import FW_RULE_DIRECT, FW_RULE_NAT, NAT_NAME
from wiredeck.wireless_diagnostics import select_wifi_adapter
from wiredeck.wireless_probes import WIFI_POWERSAVE_TARGETS, collect_wireless_snapshot

MAX_LOG_FILES = 14


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _append_bom_log(paths: AppPaths, action: str, lines: list[str]) -> str:
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    log_path = paths.logs_dir / f"wiredeck_{day}.log"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    block = [f"[{stamp}] ACTION={action}"] + [f"  {line}" for line in lines] + [""]
    text = "\r\n".join(block)
    if not log_path.exists():
        log_path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    else:
        with log_path.open("ab") as fh:
            fh.write(text.encode("utf-8"))
    logs = sorted(paths.logs_dir.glob("wiredeck_*.log"))
    for stale in logs[:-MAX_LOG_FILES]:
        try:
            stale.unlink()
        except OSError:
            pass
    return str(log_path)


def _save_wifi_backup_once(paths: AppPaths, backup_payload: dict[str, Any]) -> None:
    """Persist original Wi-Fi adapter properties once before WireDeck tunes them."""
    backup_path = paths.runtime_dir / "wifi_backup.json"
    if backup_path.is_file():
        return
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".wifi_bk_", suffix=".tmp", dir=str(paths.runtime_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(backup_payload, ensure_ascii=False, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, backup_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _load_wifi_backup(paths: AppPaths) -> dict[str, Any] | None:
    backup_path = paths.runtime_dir / "wifi_backup.json"
    if not backup_path.is_file():
        return None
    try:
        data = json.loads(backup_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


_WINRT_HELPER_PS = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null

$asTaskOp = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
$asTaskAct = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
})[0]

function Await-WinRTOp($op, $type) {
  $t = $asTaskOp.MakeGenericMethod($type).Invoke($null, @($op))
  $t.Wait() | Out-Null
  return $t.Result
}
function Await-WinRTAction($act) {
  $t = $asTaskAct.Invoke($null, @($act))
  $t.Wait() | Out-Null
}
"""


def enable_hotspot(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    force: bool = False,
    runner: Callable[[str, int], PsResult] = run_powershell,
    snapshot_fn: Callable[..., dict[str, Any]] = collect_wireless_snapshot,
    disable_direct_fn: Callable[[], ActionResult] | None = None,
) -> ActionResult:
    """Start Windows 10/11 Mobile Hotspot locked to 5 GHz (with Wired WinNAT conflict guard)."""
    snapshot = snapshot_fn(cfg, probe_network=False)
    if snapshot.get("wired_nat_active"):
        if not force:
            log_file = _append_bom_log(paths, "enable-hotspot", ["BLOCKED: wired WireDeckNat active"])
            return ActionResult(
                ok=False,
                code="WIRED_NAT_CONFLICT",
                message="有线 WireDeckNat 正在运行，开启热点前需先关闭有线直连共享",
                exit_code=2,
                log_file=log_file,
            )
        if disable_direct_fn is not None:
            dd = disable_direct_fn()
            if not dd.ok:
                return dd

    pre_clean_wired = ""
    if snapshot.get("wired_nat_active") and force and disable_direct_fn is None:
        pre_clean_wired = f"""
Get-NetNat -Name {_ps_quote(NAT_NAME)} -ErrorAction SilentlyContinue | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName {_ps_quote(FW_RULE_NAT)} -ErrorAction SilentlyContinue
Remove-NetFirewallRule -DisplayName {_ps_quote(FW_RULE_DIRECT)} -ErrorAction SilentlyContinue
"""

    band_mode = cfg.hotspot_band if getattr(cfg, "hotspot_band", "") in ("5ghz", "2.4ghz", "auto") else ("5ghz" if cfg.hotspot_prefer_5ghz else "auto")
    script = f"""
{pre_clean_wired}
{_WINRT_HELPER_PS}
$prof = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $prof) {{
  $profiles = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()
  if ($profiles -and $profiles.Count -gt 0) {{ $prof = $profiles[0] }}
}}
if (-not $prof) {{ throw "NO_CONNECTION_PROFILE" }}

$tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($prof)
$ap = $tm.GetCurrentAccessPointConfiguration()
$wasOn = ([string]$tm.TetheringOperationalState -eq 'On')
$origBand = [string]$ap.Band
$band5 = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::FiveGigahertz
$band24 = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::TwoPointFourGigahertz
$bandAuto = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::Auto
$targetBandEnum = $bandAuto
$targetBandStr = 'Auto'
if ('{band_mode}' -eq '5ghz' -and $ap.IsBandSupported($band5)) {{
  $targetBandEnum = $band5
  $targetBandStr = 'FiveGigahertz'
}} elseif ('{band_mode}' -eq '2.4ghz' -and $ap.IsBandSupported($band24)) {{
  $targetBandEnum = $band24
  $targetBandStr = 'TwoPointFourGigahertz'
}}

if ($origBand -ne $targetBandStr) {{
  try {{
    if ($wasOn) {{
      $null = Await-WinRTOp ($tm.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
      $ap = $tm.GetCurrentAccessPointConfiguration()
    }}
    $ap.Band = $targetBandEnum
    Await-WinRTAction ($tm.ConfigureAccessPointAsync($ap))
  }} catch {{}}
}}

$fallbackFrom5g = $false
$started = $false
if ([string]$tm.TetheringOperationalState -eq 'On') {{
  $started = $true
}} else {{
  try {{
    $opRes = Await-WinRTOp ($tm.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ([string]$opRes.Status -eq 'Success') {{
      $started = $true
    }}
  }} catch {{}}
}}

if (-not $started -and '{band_mode}' -eq '5ghz') {{
  try {{
    if ([string]$tm.TetheringOperationalState -eq 'On') {{
      $null = Await-WinRTOp ($tm.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    }}
    $ap2 = $tm.GetCurrentAccessPointConfiguration()
    $fbEnum = if ($ap2.IsBandSupported($band24)) {{ $band24 }} else {{ $bandAuto }}
    $ap2.Band = $fbEnum
    Await-WinRTAction ($tm.ConfigureAccessPointAsync($ap2))
    $opRes2 = Await-WinRTOp ($tm.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ([string]$opRes2.Status -eq 'Success') {{
      $started = $true
      $fallbackFrom5g = $true
    }} else {{
      throw ("TETHER_START_FAILED:" + [string]$opRes2.Status)
    }}
  }} catch {{
    throw
  }}
}} elseif (-not $started) {{
  throw "TETHER_START_FAILED:StartFailed"
}}

$finalAp = $tm.GetCurrentAccessPointConfiguration()
[ordered]@{{
  state = [string]$tm.TetheringOperationalState
  band = [string]$finalAp.Band
  ssid = [string]$finalAp.Ssid
  fallback_from_5g = $fallbackFrom5g
}} | ConvertTo-Json -Compress
"""
    res = runner(script, 25)
    log_file = _append_bom_log(
        paths,
        "enable-hotspot",
        [f"band_mode={band_mode}, prefer_5g={cfg.hotspot_prefer_5ghz}, force={force}, rc={res.returncode}, stdout={res.stdout.strip()}, stderr={res.stderr.strip()}"],
    )
    if res.returncode != 0:
        err_blob = f"{res.stderr}\n{res.stdout}"
        if "NO_CONNECTION_PROFILE" in err_blob:
            return ActionResult(
                ok=False,
                code="NO_UPSTREAM_NETWORK",
                message="Windows 移动热点要求电脑至少先接入任意网络（校园 Wi-Fi、网线或手机 USB 共享均可）",
                exit_code=1,
                details={"stderr": res.stderr.strip()},
                log_file=log_file,
            )
        return ActionResult(
            ok=False,
            code="ENABLE_HOTSPOT_FAILED",
            message="开启移动热点失败，请确认上游已联网且无线网卡未被占用",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )

    actual_band = ""
    fallback_from_5g = False
    try:
        parsed_hs = json.loads(res.stdout.strip().splitlines()[-1]) if res.stdout.strip() else {}
        if isinstance(parsed_hs, dict):
            actual_band = str(parsed_hs.get("band") or "")
            fallback_from_5g = bool(parsed_hs.get("fallback_from_5g", False))
    except Exception:
        pass

    if fallback_from_5g:
        hs_msg = "移动热点已开启（2.4 GHz — 因网卡/上游频段限制，已自动由 5 GHz 降级兼容）"
    elif band_mode == "2.4ghz" or "two" in actual_band.lower():
        hs_msg = "移动热点已开启（2.4 GHz 频段）"
    elif "five" in actual_band.lower():
        hs_msg = "移动热点已开启（5 GHz 频段）"
    else:
        hs_msg = "移动热点已开启"

    return ActionResult(
        ok=True,
        code="HOTSPOT_ENABLED",
        message=hs_msg,
        exit_code=0,
        details={"band": actual_band, "band_mode": band_mode, "fallback_from_5g": fallback_from_5g},
        log_file=log_file,
    )


def disable_hotspot(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    runner: Callable[[str, int], PsResult] = run_powershell,
) -> ActionResult:
    """Stop Windows Mobile Hotspot cleanly."""
    script = f"""
{_WINRT_HELPER_PS}
$prof = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $prof) {{
  $profiles = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()
  if ($profiles -and $profiles.Count -gt 0) {{ $prof = $profiles[0] }}
}}
if ($prof) {{
  $tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($prof)
  if ([string]$tm.TetheringOperationalState -eq 'On') {{
    $opRes = Await-WinRTOp ($tm.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ([string]$opRes.Status -ne 'Success') {{
      throw ("TETHER_STOP_FAILED:" + [string]$opRes.Status)
    }}
  }}
}}
Write-Output "DISABLE_HOTSPOT_OK"
"""
    res = runner(script, 20)
    log_file = _append_bom_log(
        paths,
        "disable-hotspot",
        [f"rc={res.returncode}, stdout={res.stdout.strip()}, stderr={res.stderr.strip()}"],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="DISABLE_HOTSPOT_FAILED",
            message="关闭移动热点失败，请查看日志",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    return ActionResult(
        ok=True,
        code="HOTSPOT_DISABLED",
        message="已关闭移动热点",
        exit_code=0,
        log_file=log_file,
    )


def optimize_wifi(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    runner: Callable[[str, int], PsResult] = run_powershell,
    snapshot_fn: Callable[..., dict[str, Any]] = collect_wireless_snapshot,
) -> ActionResult:
    """Snapshot original Wi-Fi settings and apply safe, reversible anti-stutter tuning."""
    snapshot = snapshot_fn(cfg, probe_network=False)
    wifi = select_wifi_adapter(snapshot.get("wifi_adapters") or [], cfg.wifi_interface)
    if wifi is None:
        log_file = _append_bom_log(paths, "optimize-wifi", ["ERROR: no Wi-Fi adapter"])
        return ActionResult(ok=False, code="WIFI_NOT_FOUND", message="未检测到可用无线网卡", exit_code=2, log_file=log_file)
    if str(wifi.get("status", "")).lower() == "disabled":
        log_file = _append_bom_log(paths, "optimize-wifi", ["ERROR: Wi-Fi adapter disabled"])
        return ActionResult(ok=False, code="WIFI_DISABLED", message="无线网卡处于禁用状态", exit_code=2, log_file=log_file)

    wifi_name = str(wifi["name"])
    wlan = snapshot.get("wlan") or {}
    connected = bool(wlan.get("connected", False))
    adv_props = wifi.get("advanced_properties") or []
    present_map = {str(p.get("keyword", "")): p for p in adv_props}

    # Backup original property values + autoconfig state once before modifying
    tracked_keys = ("MIMOPowerSaveMode", "*PacketCoalescing", "PowerSavingMode", "uAPSDSupport", "RoamAggressiveness")
    orig_props: dict[str, str] = {}
    for k in tracked_keys:
        if k in present_map:
            orig_props[k] = str(present_map[k].get("registry_value", ""))

    _save_wifi_backup_once(
        paths,
        {
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "interface_alias": wifi_name,
            "autoconfig_enabled": snapshot.get("autoconfig_enabled"),
            "properties": orig_props,
        },
    )

    cmds: list[str] = [
        f"$wifi = {_ps_quote(wifi_name)}",
        r"""function Set-WifiOptProp([string]$ifName, [string]$kw, [string]$optRe, [string]$fallbackRaw) {
  try {
    $prop = Get-NetAdapterAdvancedProperty -Name $ifName -RegistryKeyword $kw -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $prop) { return }
    $pick = $null
    foreach ($v in @($prop.ValidDisplayValues)) {
      if ([string]$v -match $optRe) { $pick = [string]$v; break }
    }
    if ($pick) {
      Set-NetAdapterAdvancedProperty -Name $ifName -RegistryKeyword $kw -DisplayValue $pick -NoRestart -ErrorAction SilentlyContinue
    } else {
      Set-NetAdapterAdvancedProperty -Name $ifName -RegistryKeyword $kw -RegistryValue $fallbackRaw -NoRestart -ErrorAction SilentlyContinue
    }
  } catch {}
}""",
    ]
    applied_changes: list[str] = []

    for kw, spec in WIFI_POWERSAVE_TARGETS.items():
        if kw not in present_map:
            continue
        opt_re = str(spec["optimal_ps_regex"])
        fallback = str(spec["fallback_raw"])
        cmds.append(f"Set-WifiOptProp $wifi {_ps_quote(kw)} {_ps_quote(opt_re)} {_ps_quote(fallback)}")
        applied_changes.append(f"{kw}->{fallback}")

    if "RoamAggressiveness" in present_map:
        cmds.append(
            "try { Set-NetAdapterAdvancedProperty -Name $wifi -RegistryKeyword 'RoamAggressiveness' -RegistryValue '1' -NoRestart -ErrorAction SilentlyContinue } catch {}"
        )
        applied_changes.append("RoamAggressiveness=1")

    # CRITICAL SAFETY GUARD: only pause background AutoConfig scan when currently connected!
    paused_scan = False
    if connected:
        cmds.append('& netsh wlan set autoconfig enabled=no interface="$wifi" | Out-Null')
        paused_scan = True
        applied_changes.append("autoconfig=no")

    cmds.append("Write-Output 'OPTIMIZE_WIFI_OK'")
    res = runner("\n".join(cmds), 25)
    log_file = _append_bom_log(
        paths,
        "optimize-wifi",
        [f"wifi={wifi_name}, connected={connected}, changes={applied_changes}, rc={res.returncode}, stderr={res.stderr.strip()}"],
    )
    if res.returncode != 0:
        return ActionResult(
            ok=False,
            code="OPTIMIZE_WIFI_FAILED",
            message="无线防抖优化失败，请查看日志",
            exit_code=1,
            details={"stderr": res.stderr.strip()},
            log_file=log_file,
        )
    note = "（已暂停周期后台扫描）" if paused_scan else "（未连 Wi-Fi，保留自动扫描，已关闭网卡节能/合并）"
    return ActionResult(
        ok=True,
        code="WIFI_OPTIMIZED",
        message=f"无线防抖优化已生效{note}",
        exit_code=0,
        details={"wifi": wifi_name, "paused_scan": paused_scan, "changes": applied_changes},
        log_file=log_file,
    )


def restore_wifi(
    paths: AppPaths,
    cfg: AppConfig,
    *,
    runner: Callable[[str, int], PsResult] = run_powershell,
    snapshot_fn: Callable[..., dict[str, Any]] = collect_wireless_snapshot,
) -> ActionResult:
    """Restore WLAN AutoConfig scanning and backed-up Wi-Fi adapter properties."""
    backup = _load_wifi_backup(paths)
    wifi_name = str(backup.get("interface_alias", "")) if backup else ""
    if not wifi_name:
        snapshot = snapshot_fn(cfg, probe_network=False)
        wifi = select_wifi_adapter(snapshot.get("wifi_adapters") or [], cfg.wifi_interface)
        wifi_name = str(wifi["name"]) if wifi else "WLAN"

    cmds: list[str] = [
        f"$wifi = {_ps_quote(wifi_name)}",
        '& netsh wlan set autoconfig enabled=yes interface="$wifi" | Out-Null',
    ]
    if backup and isinstance(backup.get("properties"), dict):
        for kw, val in backup["properties"].items():
            if str(val) != "":
                cmds.append(
                    f"try {{ Set-NetAdapterAdvancedProperty -Name $wifi -RegistryKeyword {_ps_quote(str(kw))} -RegistryValue {_ps_quote(str(val))} -NoRestart -ErrorAction SilentlyContinue }} catch {{}}"
                )
    cmds.append("Write-Output 'RESTORE_WIFI_OK'")

    res = runner("\n".join(cmds), 25)
    log_file = _append_bom_log(
        paths,
        "restore-wifi",
        [f"wifi={wifi_name}, had_backup={backup is not None}, rc={res.returncode}, stderr={res.stderr.strip()}"],
    )
    if res.returncode == 0:
        backup_path = paths.runtime_dir / "wifi_backup.json"
        try:
            backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        return ActionResult(
            ok=True,
            code="WIFI_RESTORED",
            message="已恢复日常无线配置与自动扫描",
            exit_code=0,
            details={"wifi": wifi_name},
            log_file=log_file,
        )
    return ActionResult(
        ok=False,
        code="RESTORE_WIFI_FAILED",
        message="恢复日常无线失败，请查看日志",
        exit_code=1,
        details={"stderr": res.stderr.strip()},
        log_file=log_file,
    )
