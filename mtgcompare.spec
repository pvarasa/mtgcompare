# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for mtgcompare desktop app.

Build:
    uv run pyinstaller mtgcompare.spec --clean --noconfirm

Output:  dist/mtgcompare/   (folder)
         dist/mtgcompare-windows.zip  (created by scripts/build.ps1)
"""
from PyInstaller.utils.hooks import collect_all

# yfinance and pandas use dynamic imports that the static analyser misses.
yf_datas,     yf_binaries,     yf_hidden     = collect_all("yfinance")
pandas_datas, pandas_binaries, pandas_hidden = collect_all("pandas")

a = Analysis(
    ["mtgcompare/launcher.py"],
    pathex=[],
    binaries=yf_binaries + pandas_binaries,
    datas=[
        ("mtgcompare/templates", "mtgcompare/templates"),
        ("mtgcompare/static",    "mtgcompare/static"),
        ("logging.conf",         "."),
    ] + yf_datas + pandas_datas,
    hiddenimports=[
        # Scrapers are imported by name in shops.py — list them explicitly
        # so the analyser doesn't miss them.
        "mtgcompare.scrappers.hareruya",
        "mtgcompare.scrappers.scryfall",
        "mtgcompare.scrappers.singlestar",
        "mtgcompare.scrappers.tokyomtg",
    ] + yf_hidden + pandas_hidden,
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
    upx=True,
    console=False,      # no terminal window
    icon=None,          # swap for "assets/icon.ico" if you add one later
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="mtgcompare",
)
