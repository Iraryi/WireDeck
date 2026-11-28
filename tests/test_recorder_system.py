from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from wiredeck.app import WireDeckApi
from wiredeck.app import main as app_main
from wiredeck.cli import main as cli_main
from wiredeck.config import AppConfig, RecorderSettings, load_config, save_config
from wiredeck.models import ActionResult
from wiredeck.paths import resolve_app_paths
from wiredeck.ps_runner import PsResult
from wiredeck.recorder import (
    RecorderController,
    build_ffmpeg_command,
    cleanup_stale_part_files,
    delete_clip_file,
    detect_ffmpeg_audio_devices,
    detect_ffmpeg_video_devices,
    detect_obs_info,
    get_next_sequence_index,
    inspect_target_path,
    list_recorded_clips,
    list_storage_drives,
    offload_clips_to_target,
    preview_clip_filenames,
    prune_clips_by_quota,
    rename_clip_with_forced_seq,
    render_clip_filename,
    resolve_capture_target,
    resolve_effective_split_policy,
    sync_obs_capture_source,
    sync_obs_output_dir,
)
from wiredeck.version import NAT_NAME
from wiredeck.win32_helpers import Win32Ops


def test_recorder_config_roundtrip_and_defaults(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    cfg = load_config(paths)
    assert cfg.recorder.backend == "auto"
    assert cfg.recorder.mode == "replay"
    assert cfg.recorder.auto_offload is True
    assert cfg.recorder.ffmpeg_format == "mkv"
    assert cfg.recorder.split_mode == "off"
    assert cfg.recorder.split_minutes == 30
    assert cfg.recorder.split_size_mb == 2048
    assert cfg.recorder.filename_template == "{preset}_{date}_{time}"
    assert cfg.recorder.apply_naming_on_offload is True

    cfg.recorder = RecorderSettings(
        backend="obs",
        mode="auto_record",
        auto_link_quick_play=True,
        output_dir=r"E:\XboxClips",
        ffmpeg_format="mp4",
        ffmpeg_bitrate_mbps=16,
        max_storage_gb=64.0,
        auto_prune=True,
        split_mode="both",
        split_minutes=15,
        split_size_mb=1024,
        filename_template="Halo_{preset}_{date}",
        apply_naming_on_offload=True,
    )
    save_config(paths, cfg)
    reloaded = load_config(paths)
    assert reloaded.recorder.backend == "obs"
    assert reloaded.recorder.mode == "auto_record"
    assert reloaded.recorder.auto_link_quick_play is True
    assert reloaded.recorder.output_dir == r"E:\XboxClips"
    assert reloaded.recorder.ffmpeg_format == "mp4"
    assert reloaded.recorder.ffmpeg_bitrate_mbps == 16
    assert reloaded.recorder.max_storage_gb == 64.0
    assert reloaded.recorder.auto_prune is True
    assert reloaded.recorder.split_mode == "both"
    assert reloaded.recorder.split_minutes == 15
    assert reloaded.recorder.split_size_mb == 1024
    assert reloaded.recorder.filename_template == "Halo_{preset}_{date}"
    assert reloaded.recorder.apply_naming_on_offload is True


def test_storage_drives_classification_exfat_fat32_and_offline_guard(tmp_path: Path):
    mock_disks = [
        {"device_id": "C:", "volume_name": "Windows", "drive_type": 3, "filesystem": "NTFS", "free_bytes": 40 * (1024**3), "total_bytes": 300 * (1024**3)},
        {"device_id": "E:", "volume_name": "USB_1TB", "drive_type": 2, "filesystem": "exFAT", "free_bytes": 110 * (1024**3), "total_bytes": 1000 * (1024**3)},
        {"device_id": "F:", "volume_name": "OLD_8G", "drive_type": 2, "filesystem": "FAT32", "free_bytes": 7 * (1024**3), "total_bytes": 8 * (1024**3)},
    ]
    drives = list_storage_drives(
        runner=lambda s, t=8: PsResult(0, json.dumps(mock_disks), ""),
        bitrate_mbps=10,
    )
    by_id = {d["device_id"]: d for d in drives}
    assert by_id["E:"]["recommended"] is True
    assert by_id["E:"]["supports_large_files"] is True
    assert by_id["E:"]["est_hours"] > 20.0
    assert by_id["F:"]["supports_large_files"] is False

    # FAT32 target emits 4GB warning
    fat_warn = inspect_target_path(r"F:\Clips", drives=drives, min_free_gb=5.0)
    assert any("FAT32" in w for w in fat_warn["warnings"])

    # Unplugged drive reports offline cleanly
    offline = inspect_target_path(r"Z:\MissingUsb", drives=drives)
    assert offline["configured"] is True and offline["online"] is False


def test_detect_obs_info_and_sync_preserves_native_formats(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    appdata = tmp_path / "AppData"
    obs_prof = appdata / "obs-studio" / "basic" / "profiles" / "MyStream"
    obs_prof.mkdir(parents=True)
    (appdata / "obs-studio" / "global.ini").write_text("[Basic]\nProfile=MyStream\nProfileDir=MyStream\n", encoding="utf-8")
    (obs_prof / "basic.ini").write_text(
        "[Output]\nMode=Simple\n[SimpleOutput]\nFilePath=C:\\Users\\Demo\\Videos\nRecFormat2=mkv\nRecEncoder=nvenc_av1\n[AdvOut]\nRecFilePath=C:\\Users\\Demo\\Videos\nRecFormat2=mp4\n",
        encoding="utf-8",
    )

    info = detect_obs_info(appdata_dir=appdata)
    assert info["profile_name"] == "MyStream"
    assert info["record_format"] == "mkv"
    assert info["record_path"] == r"C:\Users\Demo\Videos"

    target = tmp_path / "USB_exFAT" / "Xbox录像"
    res = sync_obs_output_dir(str(target), paths=paths, appdata_dir=appdata)
    assert res.ok is True and res.code == "OBS_SYNCED"
    assert (paths.runtime_dir / "obs_basic_ini_backup.ini").is_file()

    updated = (obs_prof / "basic.ini").read_text(encoding="utf-8-sig")
    assert f"FilePath={target}" in updated
    assert f"RecFilePath={target}" in updated
    assert "RecFormat2=mkv" in updated
    assert "RecEncoder=nvenc_av1" in updated


def test_build_ffmpeg_command_selects_best_encoder():
    cmd_5070 = build_ffmpeg_command(
        "ffmpeg.exe",
        Path("E:/clips/out.mkv"),
        encoder="auto",
        container_fmt="mkv",
        bitrate_mbps=12,
        fps=60,
        system_env={"primary_nvidia_gpu": {"name": "RTX 5070 Ti Laptop", "supports_smooth_motion": True}},
    )
    assert "av1_nvenc" in cmd_5070 and "12M" in cmd_5070 and str(Path("E:/clips/out.mkv")) in cmd_5070

    cmd_3060 = build_ffmpeg_command(
        "ffmpeg.exe",
        Path("E:/clips/out.mp4"),
        encoder="auto",
        system_env={"primary_nvidia_gpu": {"name": "RTX 3060", "supports_smooth_motion": False}},
    )
    assert "hevc_nvenc" in cmd_3060

    cmd_cpu = build_ffmpeg_command("ffmpeg.exe", Path("E:/clips/out.flv"), encoder="auto", system_env={})
    assert "libx264" in cmd_cpu


def test_offload_sha256_verified_prune_and_safe_delete(tmp_path: Path):
    src_dir = tmp_path / "LocalCaptures"
    dst_dir = tmp_path / "外接 U盘" / "Xbox录像"
    src_dir.mkdir(parents=True)

    # Active file <2s old should be skipped
    active_clip = src_dir / "writing_now.mp4"
    active_clip.write_bytes(b"ACTIVE")

    # Ready clips + non-video file
    c1 = src_dir / "clip_one.mkv"
    c1.write_bytes(b"MKV_DATA_" * 1024)
    c2 = src_dir / "clip_two.mp4"
    c2.write_bytes(b"MP4_DATA_" * 1024)
    note = src_dir / "readme.txt"
    note.write_text("keep", encoding="utf-8")

    old_ts = time.time() - 60
    os.utime(c1, (old_ts - 20, old_ts - 20))
    os.utime(c2, (old_ts, old_ts))

    res = offload_clips_to_target([src_dir], dst_dir, min_age_sec=2.0, delete_source=True)
    assert res.ok is True and res.details["moved_count"] == 2
    assert (dst_dir / "clip_one.mkv").is_file()
    assert (dst_dir / "clip_two.mp4").is_file()
    assert not c1.exists() and not c2.exists()
    assert active_clip.exists()  # Untouched active file
    assert note.exists()  # Untouched non-video

    # Duplicate collision check
    dup = src_dir / "clip_one.mkv"
    dup.write_bytes(b"NEW_CLIP_PAYLOAD")
    os.utime(dup, (old_ts - 5, old_ts - 5))
    res2 = offload_clips_to_target([src_dir], dst_dir, min_age_sec=2.0, delete_source=True)
    assert res2.ok is True and (dst_dir / "clip_one_1.mkv").is_file()

    # Non-video in dst_dir must never be touched by prune
    (dst_dir / "keep_me.txt").write_text("safe", encoding="utf-8")
    listed = list_recorded_clips(str(dst_dir))
    assert listed["total_count"] == 3

    # Quota prune with min_free_gb high enough deletes oldest clips first until satisfied
    prune_res = prune_clips_by_quota(
        dst_dir,
        max_storage_gb=0.00001,
        min_free_gb=0.0,
        free_bytes_fn=lambda p: 100 * (1024**3),
    )
    assert prune_res.ok is True and prune_res.details["deleted_count"] >= 1
    assert (dst_dir / "keep_me.txt").is_file()

    # Path traversal delete rejected
    esc = delete_clip_file(dst_dir, dst_dir / ".." / "escape.mp4")
    assert esc.ok is False and esc.code == "PATH_ESCAPE_BLOCKED"


def test_recorder_controller_backends_and_quick_play_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    hotkeys: list[str] = []
    launched: list[tuple[str, list[str]]] = []

    fake_ops = Win32Ops(
        is_process_running=lambda n: False,
        launch_process=lambda exe, args=None, cwd="", minimized=False: launched.append((exe, list(args or []))) or 301,
        send_hotkey=lambda k: hotkeys.append(k) or True,
        wait_for_pid_exit=lambda pid, poll_interval=1.0: True,
        set_keep_awake=lambda en: True,
    )

    ctrl = RecorderController()
    # NVIDIA hotkeys
    cfg_nv = AppConfig(recorder=RecorderSettings(backend="nvidia", mode="replay"))
    r_nv = ctrl.start_recording(cfg_nv, paths, system_env={"primary_nvidia_gpu": {"name": "RTX 5070 Ti"}}, win32_ops=fake_ops)
    r_nv_rep = ctrl.save_replay(cfg_nv, win32_ops=fake_ops)
    assert r_nv.ok and r_nv_rep.ok and hotkeys == ["Alt+F9", "Alt+F10"]

    # Game Bar hotkeys
    hotkeys.clear()
    cfg_gb = AppConfig(recorder=RecorderSettings(backend="gamebar", mode="replay"))
    ctrl.start_recording(cfg_gb, paths, win32_ops=fake_ops)
    ctrl.save_replay(cfg_gb, win32_ops=fake_ops)
    assert hotkeys == ["Win+Alt+R", "Win+Alt+G"]

    # Quick-play silent auto-link triggers session_start & session_end offload
    src_cap = tmp_path / "Captures"
    src_cap.mkdir()
    dst_usb = tmp_path / "USB"
    fake_clip = src_cap / "halo_highlight.mp4"

    def fake_wait_exit(pid: int, poll_interval: float = 1.0) -> bool:
        # Simulate clip written and flushed during gameplay (mtime between session start and now - 3s)
        fake_clip.write_bytes(b"CLIP_OK_" * 100)
        mid_t = time.time() - 3
        os.utime(fake_clip, (mid_t, mid_t))
        return True

    fake_ops_session = Win32Ops(
        is_process_running=lambda n: False,
        send_hotkey=lambda k: hotkeys.append(k) or True,
        wait_for_pid_exit=fake_wait_exit,
        set_keep_awake=lambda en: True,
    )

    cfg_link = AppConfig(
        active_preset="xbox",
        recorder=RecorderSettings(
            backend="nvidia",
            mode="replay",
            auto_link_quick_play=True,
            output_dir=str(dst_usb),
            source_watch_dir=str(src_cap),
            auto_offload=True,
        ),
    )
    save_config(paths, cfg_link)

    import wiredeck.app as app_mod

    api_mock = WireDeckApi(paths=paths, win32_ops=fake_ops_session)
    api_mock._recorder._session_started_ts = time.time() - 10
    monkeypatch.setattr(app_mod, "WireDeckApi", lambda **kw: api_mock)
    monkeypatch.setattr(app_mod, "collect_system_env_cached", lambda: {})
    monkeypatch.setattr(
        app_mod,
        "_run_quick_play_detailed",
        lambda *a, **kw: (True, "", "", {"pid": 7001, "exe_name": "XStreaming.exe", "client_name": "XStreaming"}),
    )
    hotkeys.clear()
    rc = app_main(["--quick-play", "--silent"])
    assert rc == 0
    assert "Alt+F9" in hotkeys
    clips_after = list(dst_usb.glob("1_*.mp4"))
    assert len(clips_after) == 1

    # CLI recorder subcommands
    import wiredeck.cli as cli_mod

    monkeypatch.setattr(cli_mod, "resolve_app_paths", lambda *a, **kw: paths)
    assert cli_main(["recorder", "status", "--json"]) == 0
    assert cli_main(["recorder", "clips", "--json"]) == 0


def test_mandatory_seq_naming_rename_and_offload_sequence(tmp_path: Path):
    from datetime import datetime

    # Always prepends forced `<n>_` even with empty/malformed/spoofed templates
    fixed_dt = datetime(2026, 11, 27, 23, 30, 0)
    assert render_clip_filename(1, "", ext="mkv", preset="xbox", now=fixed_dt) == "1_xbox_20261127_233000.mkv"
    assert render_clip_filename(2, "99_{seq}_boss:fight/run?", ext=".mp4", preset="xbox", now=fixed_dt) == "2_boss_fight_run.mp4"
    assert render_clip_filename(3, "{preset}_{orig}_{mode}", ext="flv", preset="ps5", mode="auto_record", orig_stem="5_raw_cap", now=fixed_dt) == "3_ps5_raw_cap_auto_record.flv"

    dst = tmp_path / "USB_Clips"
    dst.mkdir()
    (dst / "1_existing.mkv").write_bytes(b"A")
    (dst / "4_gap.mp4").write_bytes(b"B")
    assert get_next_sequence_index(dst) == 5

    previews = preview_clip_filenames("Game_{preset}", target_dir=str(dst), ext="mkv", preset="xbox", count=3)
    assert previews == ["5_Game_xbox.mkv", "6_Game_xbox.mkv", "7_Game_xbox.mkv"]

    # Rename clip strictly preserves/enforces leading `<n>_`
    ren_ok = rename_clip_with_forced_seq(dst, dst / "4_gap.mp4", "Boss Kill.mp4")
    assert ren_ok.ok is True
    assert (dst / "4_Boss_Kill.mp4").is_file()
    assert not (dst / "4_gap.mp4").exists()

    # Offload applies chronological sequence starting from max + 1 (5_, 6_, ...) preserving native extensions
    src = tmp_path / "Captures"
    src.mkdir()
    c1 = src / "raw1.mkv"
    c2 = src / "raw2.mp4"
    c1.write_bytes(b"MKV_SEQ_1")
    c2.write_bytes(b"MP4_SEQ_2")
    t_old = time.time() - 30
    os.utime(c1, (t_old - 10, t_old - 10))
    os.utime(c2, (t_old, t_old))

    off = offload_clips_to_target(
        [src],
        dst,
        min_age_sec=2.0,
        delete_source=True,
        apply_naming=True,
        filename_template="Boss_{preset}_{orig}",
        preset="xbox",
    )
    assert off.ok is True and off.details["moved_count"] == 2
    assert (dst / "5_Boss_xbox_raw1.mkv").is_file()
    assert (dst / "6_Boss_xbox_raw2.mp4").is_file()


def test_obs_split_sync_and_fat32_safeguard(tmp_path: Path):
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    appdata = tmp_path / "AppData"
    obs_prof = appdata / "obs-studio" / "basic" / "profiles" / "SplitProf"
    obs_prof.mkdir(parents=True)
    (appdata / "obs-studio" / "global.ini").write_text("[Basic]\nProfileDir=SplitProf\n", encoding="utf-8")
    (obs_prof / "basic.ini").write_text(
        "[Output]\nMode=Advanced\n[AdvOut]\nRecFilePath=C:\\Temp\nRecFormat2=mkv\nRecEncoder=ffmpeg_nvenc\n",
        encoding="utf-8",
    )

    res = sync_obs_output_dir(
        str(tmp_path / "USB"),
        paths=paths,
        appdata_dir=appdata,
        split_mode="both",
        split_minutes=20,
        split_size_mb=1500,
    )
    assert res.ok is True
    ini_txt = (obs_prof / "basic.ini").read_text(encoding="utf-8-sig")
    assert "RecSplitFile=true" in ini_txt
    assert "RecSplitFileTime=20" in ini_txt
    assert "RecSplitFileSize=1500" in ini_txt
    assert "RecFormat2=mkv" in ini_txt

    fat_pol = resolve_effective_split_policy("off", 30, 6000, filesystem="FAT32")
    assert fat_pol["mode"] == "size" and fat_pol["split_size_mb"] == 3800 and fat_pol["fat32_guarded"] is True


def test_recorder_controller_auto_split_by_size_and_duration_and_fallback(tmp_path: Path):
    import threading

    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    fake_ff = tmp_path / "ffmpeg.exe"
    fake_ff.write_bytes(b"MZ")
    out_dir = tmp_path / "USB_Clips"

    # Mock Popen process: writes 60MB to trigger 50MB size split quickly, exits cleanly on b"q\n"
    spawned_paths: list[Path] = []

    class FakeStdin:
        def __init__(self, owner):
            self.owner = owner

        def write(self, data: bytes):
            if b"q" in data:
                self.owner.rc = 0
                self.owner.done.set()

        def flush(self):
            pass

    class FakeProc:
        def __init__(self, out_p: Path, write_large: bool):
            self.out_p = out_p
            self.rc = None
            self.done = threading.Event()
            self.stdin = FakeStdin(self)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(b"\x00" * (52 * 1024 * 1024 if write_large else 1024))

        def poll(self):
            return self.rc

        def wait(self, timeout=None):
            self.done.wait(timeout=timeout)
            self.rc = 0
            return 0

        def terminate(self):
            self.rc = 0
            self.done.set()

    def fake_popen(cmd, **kwargs):
        out_p = Path(cmd[-1])
        spawned_paths.append(out_p)
        # First segment triggers size rollover (>=50MB), second stays small until stopped
        return FakeProc(out_p, write_large=(len(spawned_paths) == 1))

    ctrl = RecorderController(watch_poll_sec=0.05, split_settle_sec=0.02, popen_fn=fake_popen)
    cfg = AppConfig(
        active_preset="xbox",
        recorder=RecorderSettings(
            backend="ffmpeg",
            mode="auto_record",
            ffmpeg_exe_path=str(fake_ff),
            output_dir=str(out_dir),
            split_mode="size",
            split_size_mb=50,
            filename_template="Raid_{preset}",
        ),
    )

    r_start = ctrl.start_recording(cfg, paths)
    assert r_start.ok is True and Path(r_start.details["path"]).name.startswith("1_Raid_xbox")

    deadline = time.time() + 4.0
    while len(spawned_paths) < 2 and time.time() < deadline:
        time.sleep(0.05)

    r_stop = ctrl.stop_recording(cfg, paths)
    assert r_stop.ok is True
    assert len(spawned_paths) >= 2
    assert spawned_paths[0].name.startswith("1_Raid_xbox")
    assert spawned_paths[1].name.startswith("2_Raid_xbox")

    # Verify hotkey backend duration split triggers stop->start rollover
    hotkeys: list[str] = []
    fake_ops = Win32Ops(
        is_process_running=lambda n: False,
        send_hotkey=lambda k: hotkeys.append(k) or True,
        set_keep_awake=lambda en: True,
    )
    ctrl_hk = RecorderController(watch_poll_sec=0.05, split_settle_sec=0.02)
    cfg_hk = AppConfig(
        recorder=RecorderSettings(
            backend="nvidia",
            mode="auto_record",
            split_mode="time",
            split_minutes=1,
        )
    )
    ctrl_hk.start_recording(cfg_hk, paths, win32_ops=fake_ops)
    # Simulate segment start having exceeded 60s
    ctrl_hk._segment_started_mono = time.monotonic() - 120.0
    t_wait = time.time() + 2.0
    while len(hotkeys) < 3 and time.time() < t_wait:
        time.sleep(0.05)
    ctrl_hk.stop_recording(cfg_hk, paths, win32_ops=fake_ops)
    assert hotkeys[:3] == ["Alt+F9", "Alt+F9", "Alt+F9"]

    # Fallback directory failover when primary Z: drive is unplugged
    fb_dir = tmp_path / "LocalFallback"
    spawned_fb: list[Path] = []
    ctrl_fb = RecorderController(popen_fn=lambda cmd, **kw: spawned_fb.append(Path(cmd[-1])) or FakeProc(Path(cmd[-1]), False))
    cfg_fb = AppConfig(
        recorder=RecorderSettings(
            backend="ffmpeg",
            mode="auto_record",
            ffmpeg_exe_path=str(fake_ff),
            output_dir=r"Z:\MissingUSB\Clips",
            fallback_dir=str(fb_dir),
            split_mode="off",
        )
    )
    fb_res = ctrl_fb.start_recording(cfg_fb, paths)
    assert fb_res.ok is True and fb_res.details["used_fallback"] is True
    assert spawned_fb[0].parent == fb_dir
    ctrl_fb.stop_recording(cfg_fb, paths)


def test_part_cleanup_ffmpeg_audio_split_now_and_mid_session_guards(tmp_path: Path):
    import shutil
    import subprocess as sp_mod
    import threading

    # 1. Stale .part cleanup preserves fresh .part & real clips
    pdir = tmp_path / "USB_Part"
    pdir.mkdir()
    stale = pdir / "1_crashed.mkv.part"
    fresh = pdir / "2_copying.mp4.part"
    real = pdir / "3_ok.mkv"
    other = pdir / "notes.part"
    stale.write_bytes(b"OLD_PART")
    fresh.write_bytes(b"NEW_PART")
    real.write_bytes(b"GOOD_VIDEO")
    other.write_bytes(b"KEEP_ME")
    old_t = time.time() - 1200
    os.utime(stale, (old_t, old_t))
    os.utime(other, (old_t, old_t))

    cleaned = cleanup_stale_part_files(pdir, min_age_sec=600.0)
    assert cleaned["cleaned_count"] == 1 and cleaned["files"] == ["1_crashed.mkv.part"]
    assert not stale.exists() and fresh.exists() and real.exists() and other.exists()

    # 2. FFmpeg audio capture command + dshow parser
    dshow_stderr = (
        '[dshow @ 000] "Integrated Webcam" (video)\n'
        '[dshow @ 000] "virtual-audio-capturer" (audio)\n'
        '[dshow @ 000] "Microphone (Realtek Audio)" (audio)\n'
    )
    devs = detect_ffmpeg_audio_devices(
        "ffmpeg.exe",
        runner=lambda cmd: sp_mod.CompletedProcess(cmd, 1, "", dshow_stderr),
    )
    assert devs == ["virtual-audio-capturer", "Microphone (Realtek Audio)"]

    cmd_audio = build_ffmpeg_command("ffmpeg.exe", pdir / "1_av.mkv", audio_device="virtual-audio-capturer")
    assert "audio=virtual-audio-capturer" in cmd_audio and "aac" in cmd_audio
    cmd_video = build_ffmpeg_command("ffmpeg.exe", pdir / "1_v.mkv")
    assert "aac" not in cmd_video

    # 3. Manual split_now + mid-session USB disconnect failover + critical disk-full stop
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    fake_ff = tmp_path / "ffmpeg.exe"
    fake_ff.write_bytes(b"MZ")
    pri_dir = tmp_path / "PriUSB"
    fb_dir = tmp_path / "FallbackLocal"
    fb_dir.mkdir()
    notifs: list[str] = []

    class _FakeStdin:
        def __init__(self, p):
            self.p = p

        def write(self, d: bytes):
            if b"q" in d:
                self.p.rc = 0
                self.p.ev.set()

        def flush(self):
            pass

    class _FakeProc:
        def __init__(self, out_p: Path):
            self.rc = None
            self.ev = threading.Event()
            self.stdin = _FakeStdin(self)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            out_p.write_bytes(b"DATA")

        def poll(self):
            return self.rc

        def wait(self, timeout=None):
            self.ev.wait(timeout=timeout)
            self.rc = 0
            return 0

        def terminate(self):
            self.rc = 0
            self.ev.set()

    spawned: list[Path] = []
    free_bytes_box = {"val": 10 * (1024**3)}
    guard_ops = Win32Ops(
        show_notification=lambda t, m: notifs.append(m) or True,
        send_hotkey=lambda k: True,
        set_keep_awake=lambda en: True,
    )
    ctrl = RecorderController(
        watch_poll_sec=0.04,
        split_settle_sec=0.02,
        mid_offload_delay_sec=0.03,
        critical_free_bytes=100 * 1024 * 1024,
        free_bytes_fn=lambda p: free_bytes_box["val"],
        popen_fn=lambda cmd, **kw: spawned.append(Path(cmd[-1])) or _FakeProc(Path(cmd[-1])),
    )
    cfg = AppConfig(
        active_preset="xbox",
        recorder=RecorderSettings(
            backend="ffmpeg",
            mode="auto_record",
            ffmpeg_exe_path=str(fake_ff),
            output_dir=str(pri_dir),
            fallback_dir=str(fb_dir),
            split_mode="off",
        ),
    )
    r1 = ctrl.start_recording(cfg, paths, win32_ops=guard_ops)
    assert r1.ok is True and spawned[-1].name.startswith("1_")

    # Manual split_now -> switches to 2_... immediately
    r_split = ctrl.split_now(cfg, paths, win32_ops=guard_ops)
    assert r_split.ok is True and Path(r_split.details["path"]).name.startswith("2_")

    # Simulate USB unplugged mid-session by removing pri_dir -> auto fails over into fb_dir!
    shutil.rmtree(pri_dir, ignore_errors=True)
    deadline = time.time() + 2.0
    while not ctrl.get_runtime_status().get("used_fallback") and time.time() < deadline:
        time.sleep(0.04)
    assert ctrl.get_runtime_status()["used_fallback"] is True
    assert ctrl.get_runtime_status()["last_guard_event"] == "USB_FAILOVER"
    assert spawned[-1].parent == fb_dir

    # Now drop free space below critical threshold -> stops cleanly & notifies
    free_bytes_box["val"] = 10 * 1024 * 1024
    deadline2 = time.time() + 2.0
    while ctrl.is_recording and time.time() < deadline2:
        time.sleep(0.04)
    assert ctrl.is_recording is False
    assert ctrl.get_runtime_status()["last_guard_event"] == "DISK_CRITICAL_STOP"
    assert len(notifs) >= 2

    # 4. Mid-session rolling offload on hotkey split_now
    watch_cap = tmp_path / "NV_Cap"
    watch_cap.mkdir()
    usb_roll = tmp_path / "USB_Roll"
    usb_roll.mkdir()
    hk_events: list[str] = []
    hk_ops = Win32Ops(
        is_process_running=lambda n: False,
        send_hotkey=lambda k: hk_events.append(k) or True,
        set_keep_awake=lambda en: True,
    )
    ctrl_roll = RecorderController(watch_poll_sec=0.05, split_settle_sec=0.02, mid_offload_delay_sec=0.05)
    ctrl_roll._session_started_ts = time.time() - 30.0
    cfg_roll = AppConfig(
        active_preset="xbox",
        recorder=RecorderSettings(
            backend="nvidia",
            mode="auto_record",
            output_dir=str(usb_roll),
            source_watch_dir=str(watch_cap),
            auto_offload=True,
            split_mode="off",
        ),
    )
    ctrl_roll.start_recording(cfg_roll, paths, win32_ops=hk_ops)
    seg1_nv = watch_cap / "Desktop 2026.mp4"
    seg1_nv.write_bytes(b"NV_CLIP_ONE" * 50)
    os.utime(seg1_nv, (time.time() - 10, time.time() - 10))
    res_hk_split = ctrl_roll.split_now(cfg_roll, paths, win32_ops=hk_ops)
    assert res_hk_split.ok is True
    deadline3 = time.time() + 2.5
    while seg1_nv.exists() and time.time() < deadline3:
        time.sleep(0.05)
    ctrl_roll.stop_recording(cfg_roll, paths, win32_ops=hk_ops)
    assert not seg1_nv.exists()
    assert len(list(usb_roll.glob("1_*.mp4"))) == 1


def test_humane_capture_scopes_obs_wgc_sync_and_wake_focus(tmp_path: Path):
    from types import SimpleNamespace

    # 1. Video capture card detection parses both modern and legacy formats
    dshow_out = (
        '[dshow @ 00] "USB3.0 HD Video Capture" (video)\n'
        '[dshow @ 00] "Integrated Webcam" (video)\n'
        '[dshow @ 00] "Microphone Array" (audio)\n'
    )
    devs = detect_ffmpeg_video_devices(
        "ffmpeg.exe",
        runner=lambda cmd: SimpleNamespace(returncode=0, stdout="", stderr=dshow_out),
    )
    assert len(devs) == 2
    assert devs[0]["name"] == "USB3.0 HD Video Capture" and devs[0]["is_capture_card"] is True
    assert devs[1]["name"] == "Integrated Webcam" and devs[1]["is_capture_card"] is False

    # 2. Target resolution: stream_window match, fallback, and strict privacy guard
    wins = [
        {"hwnd": 777, "title": "XStreaming", "process_name": "XStreaming.exe", "class_name": "Chrome_WidgetWin_1", "is_minimized": True},
        {"hwnd": 888, "title": "WeChat", "process_name": "WeChat.exe", "class_name": "WeChatMainWnd", "is_minimized": False},
    ]
    cfg_stream = AppConfig(active_preset="xbox", recorder=RecorderSettings(capture_scope="stream_window"))
    tgt_stream = resolve_capture_target(cfg_stream, windows=wins)
    assert tgt_stream["effective_scope"] == "window"
    assert tgt_stream["hwnd"] == 777
    assert tgt_stream["is_minimized"] is True

    # Closed stream window -> falls back unless strict_window_only=True
    tgt_fb = resolve_capture_target(cfg_stream, windows=[wins[1]])
    assert tgt_fb["effective_scope"] == "desktop" and tgt_fb["fell_back_to_desktop"] is True
    cfg_strict = AppConfig(active_preset="xbox", recorder=RecorderSettings(capture_scope="stream_window", strict_window_only=True))
    tgt_blk = resolve_capture_target(cfg_strict, windows=[wins[1]])
    assert tgt_blk["effective_scope"] == "blocked" and tgt_blk["ready"] is False

    # Capture card target resolves cleanly and builds headless dshow command
    cfg_card = AppConfig(recorder=RecorderSettings(capture_scope="capture_card"))
    tgt_card = resolve_capture_target(cfg_card, video_devices=devs)
    assert tgt_card["effective_scope"] == "capture_card"
    assert tgt_card["video_device"] == "USB3.0 HD Video Capture"
    cmd_card = build_ffmpeg_command(
        "ffmpeg.exe",
        tmp_path / "card.mkv",
        capture_scope="capture_card",
        video_device=tgt_card["video_device"],
    )
    assert "video=USB3.0 HD Video Capture" in cmd_card and "desktop" not in cmd_card

    # Targeted window FFmpeg command sets title= + even-dimension pad filter
    cmd_win = build_ffmpeg_command(
        "ffmpeg.exe",
        tmp_path / "win.mkv",
        capture_scope="window",
        window_title="XStreaming",
    )
    assert "title=XStreaming" in cmd_win
    assert "pad=ceil(iw/2)*2:ceil(ih/2)*2" in cmd_win

    # 3. OBS WGC source sync creates WireDeck Capture without deleting user's existing sources
    paths = resolve_app_paths(tmp_path / "app", local_appdata=tmp_path / "la")
    appdata = tmp_path / "AppData"
    scenes_dir = appdata / "obs-studio" / "basic" / "scenes"
    scenes_dir.mkdir(parents=True)
    (appdata / "obs-studio" / "global.ini").write_text("[Basic]\nSceneCollectionFile=Untitles\n", encoding="utf-8")
    scene_json = scenes_dir / "Untitles.json"
    scene_json.write_text(
        json.dumps(
            {
                "current_scene": "Main",
                "sources": [
                    {"name": "UserMic", "id": "wasapi_input_capture", "settings": {}},
                    {"name": "Main", "id": "scene", "settings": {"items": [{"name": "UserMic", "visible": True}]}},
                ],
            }
        ),
        encoding="utf-8",
    )
    sc_res = sync_obs_capture_source(tgt_stream, paths=paths, appdata_dir=appdata)
    assert sc_res["synced"] is True and sc_res["source_id"] == "window_capture"
    assert (paths.runtime_dir / "obs_scene_backup.json").is_file()
    saved_scene = json.loads(scene_json.read_text(encoding="utf-8"))
    src_names = [s["name"] for s in saved_scene["sources"]]
    assert "UserMic" in src_names and "WireDeck Capture" in src_names
    wd_src = next(s for s in saved_scene["sources"] if s["name"] == "WireDeck Capture")
    assert wd_src["settings"]["method"] == 2  # WGC background-occlusion capture
    assert "XStreaming.exe" in wd_src["settings"]["window"]

    # 4. Controller wakes minimized window non-intrusively for FFmpeg & focuses window for NVIDIA hotkey
    woken: list[int] = []
    focused: list[int] = []
    sent_hk: list[str] = []
    ff_fake = tmp_path / "ffmpeg.exe"
    ff_fake.write_bytes(b"MZ")

    class _DummyProc:
        def poll(self):
            return None

    ops_rec = Win32Ops(
        list_windows=lambda: wins,
        restore_window_no_activate=lambda h: woken.append(h) or True,
        focus_window=lambda h: focused.append(h) or True,
        send_hotkey=lambda k: sent_hk.append(k) or True,
        sleep=lambda _s: None,
    )
    cfg_ff_win = AppConfig(
        active_preset="xbox",
        recorder=RecorderSettings(
            backend="ffmpeg",
            capture_scope="stream_window",
            output_dir=str(tmp_path / "out_ff"),
            ffmpeg_exe_path=str(ff_fake),
        ),
    )
    ctrl_ff = RecorderController(popen_fn=lambda cmd, **_kw: _DummyProc())
    r_ff = ctrl_ff.start_recording(cfg_ff_win, paths, win32_ops=ops_rec)
    assert r_ff.ok and woken == [777]
    assert ctrl_ff.get_runtime_status()["capture_target_label"] == "XStreaming"
    ctrl_ff.stop_recording(cfg_ff_win, paths, win32_ops=ops_rec)

    cfg_nv_win = AppConfig(
        active_preset="xbox",
        recorder=RecorderSettings(backend="nvidia", mode="auto_record", capture_scope="stream_window"),
    )
    ctrl_nv = RecorderController()
    r_nv = ctrl_nv.start_recording(cfg_nv_win, paths, system_env={"primary_nvidia_gpu": {"name": "RTX 5070"}}, win32_ops=ops_rec)
    assert r_nv.ok and focused == [777] and sent_hk == ["Alt+F9"]
    ctrl_nv.stop_recording(cfg_nv_win, paths, win32_ops=ops_rec)

    # Strict guard rejects recording if target window is missing
    ops_empty = Win32Ops(list_windows=lambda: [wins[1]], sleep=lambda _s: None)
    r_blk = ctrl_nv.start_recording(
        AppConfig(recorder=RecorderSettings(backend="nvidia", capture_scope="stream_window", strict_window_only=True)),
        paths,
        system_env={"primary_nvidia_gpu": {"name": "RTX 5070"}},
        win32_ops=ops_empty,
    )
    assert r_blk.ok is False and r_blk.code == "CAPTURE_TARGET_MISSING"
