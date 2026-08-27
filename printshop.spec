# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：微信扫码自助打印（onefile 店内服务形态）。

- 入口：printshop/main.py（其 __main__ 调 main()）
- 数据文件：static/（admin/screen 页面）、submit_xps.ps1（Task 7B 定案：
  .NET 互操作走 PowerShell 助手，必须打入并在冻结态解析其 _MEI 解压路径）、
  vendor/（微信机器人直连模块）。tools/SumatraPDF.exe 不打入（运行时已无调用，
  白占 ~30MB 解压体积；仓内 tools/ 保留供人工调试）。
- console=False：窗口化运行，双击只出现 GUI 窗口、不出现黑色控制台；
  运行日志（应用日志、uvicorn 日志、vendor print 输出）由 main.py 初始化写入
  exe 同级 logs/printshop.log（滚动备份），排障从托盘“打开运行日志”进入。
  数据目录（inbox/work/db）由 main.py 在冻结态 chdir 到 exe 同级目录，
  保证不落 _MEI 临时目录。
- hiddenimports：uvicorn（uvloop/httptools/websockets 等被 hook 覆盖）、
  pystray 的 win32 后端与 pywin32 的 win32print/win32com 显式声明以防漏。
- BLE（Task 10B）：winrt 是命名空间包（顶层无 __init__.py），各业务子模块的
  __init__.py 里 import winrt._winrt_*.pyd（均在 winrt/ 根下）。模块图经
  hiddenimports 显式列出的 9 个业务模块可把相应 .pyd 作为 extension 收集；
  msvcp140.dll（runtime 的 VC 依赖）用 collect_dynamic_libs("winrt") 兜底。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

proj = Path(SPECPATH)
ps1 = proj / "printshop" / "submit_xps.ps1"
vendor = proj / "printshop" / "vendor"
static = proj / "printshop" / "static"

datas = [
    (str(static), "printshop/static"),
    (str(ps1), "printshop"),
]
for p in sorted(vendor.rglob("*")):
    if p.is_file() and "__pycache__" not in p.parts:
        datas.append((str(p), "printshop/vendor"))

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
    # pywebview GUI（Edge WebView2 + WinForms + pythonnet）
    "webview",
    "webview.guilib",
    "webview.platforms",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "webview.platforms.win32",
    "webview.window",
    "webview.event",
    "webview.http",
    "webview.models",
    "webview.screen",
    "webview.util",
    "webview.menu",
    "webview.localization",
    "webview.dom",
    "webview.dom.dom",
    "clr",
    "clr_loader",
    "pythonnet",
    "proxy_tools",
    "bottle",
    # winrt BLE Central 链路（ble_link.py 延迟 import，显式声明防漏收集）
    "winrt.runtime",
    "winrt.system",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.devices.bluetooth",
    "winrt.windows.devices.bluetooth.advertisement",
    "winrt.windows.devices.bluetooth.genericattributeprofile",
    "winrt.windows.devices.enumeration",
    "winrt.windows.devices.radios",
    "winrt.windows.storage.streams",
]

binaries = collect_dynamic_libs("winrt")  # msvcp140.dll（_winrt.pyd 的 VC 运行库）

a = Analysis(
    [str(proj / "printshop" / "main.py")],
    pathex=[str(proj)],
    binaries=binaries,
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
