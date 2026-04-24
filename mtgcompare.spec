# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for mtgcompare desktop app.

Build:
    uv run pyinstaller mtgcompare.spec --clean --noconfirm

Windows output: dist/mtgcompare/  (folder + .exe inside)
macOS output:   dist/MTG Compare.app  (bundle)

Use the platform build scripts to zip and package:
    Windows: scripts/build.ps1
    macOS:   scripts/build.sh
"""
import sys
from PyInstaller.utils.hooks import collect_all

# yfinance and pandas use dynamic imports that the static analyser misses.
yf_datas,     yf_binaries,     yf_hidden     = collect_all("yfinance")
pandas_datas, pandas_binaries, pandas_hidden = collect_all("pandas")

_hidden = [
    # Scrapers are imported by name in shops.py — list them explicitly
    # so the analyser doesn't miss them.
    "mtgcompare.scrappers.hareruya",
    "mtgcompare.scrappers.scryfall",
    "mtgcompare.scrappers.singlestar",
    "mtgcompare.scrappers.tokyomtg",
] + yf_hidden + pandas_hidden

if sys.platform == "darwin":
    _hidden += ["pystray._darwin"]

a = Analysis(
    ["mtgcompare/launcher.py"],
    pathex=[],
    binaries=yf_binaries + pandas_binaries,
    datas=[
        ("mtgcompare/templates", "mtgcompare/templates"),
        ("mtgcompare/static",    "mtgcompare/static"),
        ("logging.conf",         "."),
    ] + yf_datas + pandas_datas,
    hiddenimports=_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mtgcompare",
    debug=False,
    strip=False,
    upx=sys.platform == "win32",
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=sys.platform == "win32",
    upx_exclude=[],
    name="mtgcompare",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MTG Compare.app",
        icon=None,
        bundle_identifier="com.mtgcompare.app",
        info_plist={
            "CFBundleShortVersionString": "1.2.0",
            "CFBundleDisplayName": "MTG Compare",
            "LSUIElement": True,       # tray-only: no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
