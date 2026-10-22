# 运维：防火墙规则瘦身 + 小屏离线看门狗

> 背景事故：2026-10-16 开机后，小屏（ESP32，WiFi 轮询 `http://192.168.31.171:18080/api/screen/state`）
> 一直连不上主控，后台显示「📡 小屏：--」，重启电脑即恢复。

## 一、根因（有据可查）

| 证据 | 内容 |
|---|---|
| `Microsoft-Windows-WFP/Operational` Id=1030 | `Transaction Watchdog Timeout`：Transaction Time 1000ms > 阈值 500ms，`suboptimal policy configuration`；每次开机必现（10-15 09:27、10-16 08:48、10-19 13:40） |
| `Windows Firewall` Id=2059 | 「已从此计算机上的 Windows Defender 防火墙配置中删除所有规则（存储类型 12）」，紧随其后是几十条 2084「添加了重复规则」风暴 |
| 规则规模 | 本地规则 1154 条，209 组同名重复（243 条真冗余），另有 179 条指向已不存在程序 |
| 访问日志 | 故障期间 `printshop.log` 零请求，而 `ping 192.168.31.84` 通 → 入站 TCP 被默认丢弃、ICMP 走内建规则组仍放行 |

结论：不是 USB（USB 只供电 + 调试串口）、不是固件（设备 flash 回读与 10-15 构建逐字节一致，指向 .171:18080）、
不是端口/监听，而是**主机过滤引擎事务超时导致规则表未完整装载**，重启才恢复。

## 二、防火墙规则瘦身（`scripts/fw-slim.ps1`）

```powershell
# 1) 演练（只读，不需要管理员）
powershell -NoProfile -ExecutionPolicy Bypass -File E:\kaifa\自助打印\scripts\fw-slim.ps1
# 2) 确认后执行（需要管理员；自动全量备份）
powershell -NoProfile -ExecutionPolicy Bypass -File E:\kaifa\自助打印\scripts\fw-slim.ps1 -Apply
# 3) 顺带清理「程序文件已不存在」的死规则（更激进，默认关闭）
... -Apply -IncludeStale
```

- 判重签名包含：名字 + 方向 + 动作 + 配置文件 + 程序 + 协议/端口/ICMP + 地址 + 用户/加密 + 服务 + 接口
  （少一项都可能删错，故已全量纳入）。
- 保护名单：`printshop`、远程桌面、文件共享、Core Networking 永不删。
- 保留策略：启用优先 > 程序文件存在优先 > Name 字典序。
- 备份：`dist\logs\fw-backup-<时间>.wfw`，恢复 `netsh advfirewall import "<文件>"`（同目录 `restore-howto.txt`）。
- 执行后自动补建「printshop 18080 inbound」入站规则（幂等）。
- 实测：1154 → 911 条（-Apply）；加 `-IncludeStale` → 约 732 条。

验证（重启后，管理员）：没有 1030 即生效。

```powershell
Get-WinEvent -LogName "Microsoft-Windows-WFP/Operational" -MaxEvents 200 |
  Where-Object { $_.Id -eq 1030 -and $_.TimeCreated -gt (Get-Date).AddMinutes(-10) }
```

## 三、小屏离线看门狗（`scripts/watchdog.ps1` + `install_watchdog.ps1`）

每 5 分钟 + 每次登录后 70s 做一次自检：

| 检查 | 处理 |
|---|---|
| 18080 没在监听 | 尝试拉起 `dist\printshop.exe`；起不来则提醒 |
| 超过 10 分钟没有小屏上报 | ping 小屏：**通** = 主机过滤层卡住（提示重启电脑）；**不通** = 小屏没联网/没供电 |
| 缺 `printshop*` 防火墙规则 | 提醒执行 `fw-slim.ps1 -Apply` 补规则 |

- 日志：`dist\logs\watchdog.log`；状态（提醒去重，30 分钟内同一问题只弹一次）：`dist\logs\watchdog.state.json`
- PID：`dist\logs\watchdog.pid`（单实例保护）
- 自启：`HKCU\...\Run` 的 `PrintShopWatchdog` → `%LOCALAPPDATA%\PrintShop\watchdog-launch.vbs`（隐藏窗口，不需要管理员）
- 手动自检：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Once`
- 安装/卸载：`install_watchdog.ps1` / `install_watchdog.ps1 -Uninstall`
- 调参：`-WarnMinutes`（离线阈值，默认 10）、`-IntervalSeconds`（默认 300）、`-ScreenIp`、`-Port`

## 四、遗留建议

1. printshop 自启偶尔没生效（10-16 那次开机日志整段空白），可考虑改为「开机即启动」的计划任务；
   看门狗已能兜底拉起，但根因未查。
2. 应用内提醒（托盘气泡）体验更好，需要改 `printshop/` 源码并重新构建 `dist\printshop.exe`
   （构建会覆盖正在运行的 exe，需停机作业）。
