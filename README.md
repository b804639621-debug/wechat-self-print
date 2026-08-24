# 微信扫码自助打印（店内服务）

店内 Windows 主机上的自助打印服务：顾客微信扫码登录 → 文件传输助手发文件 → 店内自动接收 →
店员在管理页选打印机/份数/彩色灰度 → 经 XPS + PrintTicket 提交打印。

- 主控/服务：Python + FastAPI + uvicorn，系统托盘（pystray）常驻
- 打印链路：PyMuPDF 渲染 → XPS 打包 → PowerShell 助手（`printshop/submit_xps.ps1`，
  .NET 互操作，`PrintQueue.CreateXpsDocumentWriter().Write`）→ 打印机
- 配套：ESP32 小屏固件见 `firmware/`；柜台立牌见 `assets/signage.html`

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r printshop\requirements.txt
.\.venv\Scripts\python -m printshop.main     # 启动服务（默认 0.0.0.0:8000）
.\.venv\Scripts\python -m pytest printshop\tests -q
```

## 部署（打包与安装）

### 打包成单文件 exe

```powershell
.\.venv\Scripts\python -m PyInstaller printshop.spec
```

产出 `dist\printshop.exe`（onefile，含 admin/screen 页面、`submit_xps.ps1`、
`vendor\` 与 `tools\SumatraPDF.exe`；数据目录 `inbox/ work/ printshop.db`
由程序在冻结态自动定位到 exe 同级目录）。控制台窗口保留便于看日志；
若要静默后台运行，可把 exe 的快捷方式/任务计划设置为最小化启动。

### 目标机要求（Win10/11）

- **无需安装 Python**（exe 自包含）；**无需安装 .NET**——PowerShell 助手走系统
  自带的 Windows PowerShell 5.1 + .NET Framework 4.x（`System.Printing`、
  `ReachFramework` 均为系统内置程序集）。
- 安装到**用户可写目录**（如 `D:\printshop\`），不要放 Program Files
  （数据目录写 exe 旁）。
- 打印机：安装店内打印机驱动（USB 或网络队列），保证驱动自带的
  PrintTicket 支持双面/彩色/纸张；网络打印机需主机可常通。
- **Office 文件（Word/Excel/PPT）转换双轨**：装有 Microsoft Office 走
  COM 转换；否则需装 LibreOffice（`C:\Program Files\LibreOffice\program\soffice.exe`）。
  两者皆无时 Office 文件会标红报"未安装 Office 或 LibreOffice"。PDF/图片不受影响。
- ⚠️ **路由器必须关闭 AP 隔离**：店内 WiFi 若开启 AP 隔离，小屏与手机都访问不到
  主控服务——部署前置检查，现场最高发坑。

### 开机自启

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1            # 安装（默认 dist\printshop.exe）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall # 卸载
```

写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`（免管理员）。建议同时配置
Windows 自动登录，开机即进桌面拉起服务。

### 首次使用流程

1. 双击/自启运行 `printshop.exe`，托盘出现图标，服务监听 `0.0.0.0:8000`
2. 浏览器开 `http://127.0.0.1:8000/admin`（管理页）；`/screen`（主屏画面）
3. 管理页选打印机，用一份测试 PDF 走一次完整打印，确认双面/彩色/份数正确
4. 手机微信扫码登录 → 文件传输助手发文件 → 店内接收 → 店员打印
5. 打印完成后顾客自动退出登录、文件自动清理（隐私红线）

## 目录结构

```
printshop/         服务源码（FastAPI + 状态机 + XPS 打印引擎）
printshop/static/  admin.html（管理页）、screen.html（主屏页）
printshop/tests/   单测（178 例）
printshop.spec     打包配置（PyInstaller）
scripts/           开机自启安装脚本、dist 冒烟测试
tools/             SumatraPDF（备用调试）、PoC
firmware/          ESP32 小屏固件
```

> ⚠️ **退役/换机注意**：顾客数据（inbox/work/printshop.db、vendor 的 state.json/trace_logs）
> 都落在 exe 同级目录。替换 exe 不会带走它们；设备退役或移交前须**整目录删除**（隐私红线）。
