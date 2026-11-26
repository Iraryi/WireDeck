# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 6 multi-EXE --onedir spec: WireDeck.exe (GUI) + wiredeck-cli.exe (CUI)."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
BUILD_DIR = ROOT / "build"
ICON_PATH = str(ROOT / "assets" / "wiredeck.ico")
VERSION_FILE = str(BUILD_DIR / "version_info.txt")

wv_datas, wv_binaries, wv_hidden = collect_all("webview")
pn_datas, pn_binaries, pn_hidden = collect_all("pythonnet")
clr_datas, clr_binaries, clr_hidden = collect_all("clr_loader")

gui_datas = [
    (str(SRC / "wiredeck" / "web"), "wiredeck/web"),
    (str(SRC / "wiredeck" / "toolkits"), "wiredeck/toolkits"),
    (str(ROOT / "assets" / "wiredeck.ico"), "assets"),
    (str(ROOT / "assets" / "logo.svg"), "assets"),
    *wv_datas,
    *pn_datas,
    *clr_datas,
]
gui_binaries = [*wv_binaries, *pn_binaries, *clr_binaries]
gui_hidden = [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "wiredeck.extensions",
    "wiredeck.presets",
    "wiredeck.toolkits",
    "wiredeck.bridge",
    *wv_hidden,
    *pn_hidden,
    *clr_hidden,
]

a_gui = Analysis(
    [str(SRC / "wiredeck" / "app.py")],
    pathex=[str(SRC)],
    binaries=gui_binaries,
    datas=gui_datas,
    hiddenimports=gui_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas", "pytest"],
    noarchive=False,
)

a_cli = Analysis(
    [str(SRC / "wiredeck" / "cli.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(SRC / "wiredeck" / "toolkits"), "wiredeck/toolkits")],
    hiddenimports=[
        "wiredeck.extensions",
        "wiredeck.presets",
        "wiredeck.toolkits",
        "wiredeck.actions",
        "wiredeck.exporter",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["webview", "pythonnet", "clr_loader", "bottle", "tkinter", "pytest"],
    noarchive=False,
)

MERGE(
    (a_gui, "WireDeck", "WireDeck"),
    (a_cli, "wiredeck-cli", "wiredeck-cli"),
)

pyz_gui = PYZ(a_gui.pure)
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="WireDeck",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON_PATH,
    version=VERSION_FILE if Path(VERSION_FILE).is_file() else None,
)

pyz_cli = PYZ(a_cli.pure)
exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="wiredeck-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=ICON_PATH,
    version=VERSION_FILE if Path(VERSION_FILE).is_file() else None,
)

coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.datas,
    exe_cli,
    a_cli.binaries,
    a_cli.datas,
    strip=False,
    upx=False,
    name="WireDeck",
)
