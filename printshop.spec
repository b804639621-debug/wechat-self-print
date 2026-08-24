# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：微信扫码自助打印（onefile 店内服务形态）。

- 入口：printshop/main.py（其 __main__ 调 main()）
- 数据文件：static/（admin/screen 页面）、submit_xps.ps1（Task 7B 定案：
  .NET 互操作走 PowerShell 助手，必须打入并在冻结态解析其 _MEI 解压路径）、
  vendor/（微信机器人直连模块）、tools/SumatraPDF.exe（保留备用，存在才打）。
- console=True：店内服务便于看日志；数据目录（inbox/work/db）由 main.py 在
  冻结态 chdir 到 exe 同级目录，保证不落 _MEI 临时目录。
- hiddenimports：uvicorn（uvloop/httptools/websockets 等被 hook 覆盖）、
  pystray 的 win32 后端与 pywin32 的 win32print/win32com 显式声明以防漏。
"""

from pathlib import Path

proj = Path(SPECPATH)
ps1 = proj / "printshop" / "submit_xps.ps1"
vendor = proj / "printshop" / "vendor"
static = proj / "printshop" / "static"
sumatra = proj / "tools" / "SumatraPDF.exe"

datas = [
    (str(static), "printshop/static"),
    (str(ps1), "printshop"),
]
for p in sorted(vendor.rglob("*")):
    if p.is_file() and "__pycache__" not in p.parts:
        datas.append((str(p), "printshop/vendor"))
if sumatra.exists():
    datas.append((str(sumatra), "tools"))

hiddenimports = [
    "win32print",
    "win32com",
    "win32com.client",
    "win32api",
    "win32gui",
    "win32con",
    "pystray._win32",
    "pystray._util",
    "pystray._util.win32",
    "uvicorn",
    "websockets",
]

a = Analysis(
    [str(proj / "printshop" / "main.py")],
    pathex=[str(proj)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["opencv-python-headless", "cv2", "tkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="printshop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
