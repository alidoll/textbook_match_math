# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：打包教材匹配系统为桌面应用。

用法：
  pyinstaller desktop/textbook-match.spec --noconfirm
产物：
  dist/TextbookMatch/TextbookMatch.exe  (Windows)
  dist/TextbookMatch.app                (macOS)
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None

# 仓库根
repo_root = Path(SPECPATH).resolve().parent  # desktop/ → 仓库根

# ---- collect_all：打包原生库及其数据文件 ----
datas = []
binaries = []
hiddenimports = []

for pkg in [
    "cv2",
    "fitz",
    "pymupdf",
    "rapidocr_onnxruntime",
    "onnxruntime",
    "langchain_openai",
    "langchain_core",
    "pdfplumber",
    "openpyxl",
    "mysql.connector",
    "PIL",
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# ---- 显式 data files ----
# backend/app 整个目录（Python 源码 + templates + static + references）
# PyInstaller 静态分析无法发现动态 sys.path 导入的模块，需显式包含
datas += [
    (str(repo_root / "backend" / "app"), "backend/app"),
]

# ---- hidden imports 补充 ----
# backend/app 整个包树（动态导入，PyInstaller 静态分析发现不了）
try:
    hiddenimports += collect_submodules("app")
except Exception:
    pass

hiddenimports += [
    "mysql.connector",
    "mysql.connector.constants",
    "mysql.connector.locales",
    "mysql.connector.locales.eng",
    "mysql.connector.dbapi",
    "langchain_openai",
    "langchain_core.messages",
    "langchain_core.prompts",
]

a = Analysis(
    [str(repo_root / "desktop" / "launch.py")],
    pathex=[str(repo_root / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TextbookMatch",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TextbookMatch",
)

# macOS .app bundle
app_bUNDLE = BUNDLE(
    coll,
    name="TextbookMatch.app",
    icon=None,
    bundle_identifier="com.textbookmatch.app",
)
