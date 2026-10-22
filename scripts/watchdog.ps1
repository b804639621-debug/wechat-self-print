# 自助打印看门狗：开机自检 + 小屏离线提醒。
#
# 解决的故障（2026-10-19 实锤）：
#   主机某次开机后防火墙过滤引擎事务超时（WFP/Operational Id=1030），规则表没装完，
#   入站 TCP 被默认丢弃而 ICMP 仍放行 —— 小屏 ping 得通、连不上 18080，
#   后台一直显示「📡 小屏：--」，能一直坏几个小时没人发现。重启电脑即恢复。
#
# 本脚本做三件事：
#   1) 服务没在跑 → 尝试拉起 dist\printshop.exe；
#   2) 小屏超过 -WarnMinutes 分钟没上报 → 弹通知，并区分两种原因：
#        ping 得通 = 主机的网络过滤层卡住（提示重启电脑）
#        ping 不通 = 小屏没联网/没供电（提示查小屏）
#   3) 缺 18080 放行规则 → 提示补规则。
#
# 用法：
#   # 单次自检（手动排查用，结果直接打在屏幕上）
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Once
#   # 常驻（由 install_watchdog.ps1 通过 HKCU Run 拉起，登录后自动运行）
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\watchdog.ps1 -Loop -Startup
#
# 不需要管理员权限（只读规则 + 拉起本服务 + 发通知）。日志：dist\logs\watchdog.log

param(
    [switch]$Loop,
    [switch]$Once,
    [switch]$Startup,
    [int]$WarnMinutes = 10,
    [int]$IntervalSeconds = 300,
    [int]$Port = 18080,
    [string]$ScreenIp = '192.168.31.84',
    [string]$Repo = '',
    [switch]$NoNotify
)

$ErrorActionPreference = 'Continue'
if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }
$repo = $Repo
$logDir = Join-Path $repo 'dist\logs'
$watchLog = Join-Path $logDir 'watchdog.log'
$stateFile = Join-Path $logDir 'watchdog.state.json'
$pidFile = Join-Path $logDir 'watchdog.pid'
$appLog = Join-Path $logDir 'printshop.log'
$exe = Join-Path $repo 'dist\printshop.exe'
$dedupeMinutes = 30

if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    try {
        if ((Test-Path -LiteralPath $watchLog) -and (Get-Item $watchLog).Length -gt 1MB) {
            Move-Item -LiteralPath $watchLog -Destination ($watchLog + '.1') -Force
        }
        Add-Content -LiteralPath $watchLog -Value $line -Encoding UTF8
    } catch {}
}

function Read-State {
    if (Test-Path -LiteralPath $stateFile) {
        try { return (Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json) } catch {}
    }
    return [pscustomobject]@{}
}

function Save-State($state) {
    try { $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $stateFile -Encoding UTF8 } catch {}
}

function Test-Listening([int]$p) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $p, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(800)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch { return $false } finally { $client.Close() }
}

# 从 printshop 访问日志里找最后一次「非回环地址」的小屏轮询时间。
function Get-LastScreenPoll {
    $pattern = 'uvicorn\.access\] (?!127\.0\.0\.1:)[0-9.]+:\d+ - "GET /api/screen/state'
    foreach ($f in @($appLog, "$appLog.1")) {
        if (-not (Test-Path -LiteralPath $f)) { continue }
        try {
            $hit = Select-String -LiteralPath $f -Pattern $pattern -ErrorAction Stop | Select-Object -Last 1
        } catch { continue }
        if ($null -eq $hit) { continue }
        if ($hit.Line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
            try { return [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null) } catch {}
        }
    }
    return $null
}

function Send-Notice([string]$title, [string]$text) {
    if ($NoNotify) { return }
    # 优先 Win10/11 原生 toast；失败退回 WScript 弹窗（会被店里的人看到）
    try {
        [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
        [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
        $esc = [System.Security.SecurityElement]::Escape($text)
        $xml = "<toast><visual><binding template='ToastGeneric'><text>$title</text><text>$esc</text></binding></visual></toast>"
        $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
        $doc.LoadXml($xml)
        $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
        $aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
        return
    } catch {}
    try {
        $sh = New-Object -ComObject WScript.Shell
        [void]$sh.Popup($text, 20, $title, 0x40 -bor 0x1000)
    } catch {}
}

function Invoke-Check {
    $problems = @()

    $listening = Test-Listening $Port
    if (-not $listening) {
        $proc = Get-Process -Name 'printshop' -ErrorAction SilentlyContinue
        if (-not $proc -and (Test-Path -LiteralPath $exe)) {
            try {
                Start-Process -FilePath $exe -WorkingDirectory (Split-Path -Parent $exe) | Out-Null
                Write-Log "服务未运行，已尝试拉起：$exe"
            } catch {
                Write-Log "拉起服务失败：$($_.Exception.Message)"
            }
        } elseif ($proc) {
            Write-Log ("服务进程在（PID {0}）但端口未就绪，等待 5s" -f ($proc.Id -join ','))
        } else {
            Write-Log "服务未运行，且找不到 $exe"
        }
        Start-Sleep -Seconds 5
        $listening = Test-Listening $Port
        if (-not $listening) {
            $problems += [pscustomobject]@{
                Key   = 'svc_down'
                Title = '自助打印服务没运行'
                Text  = "后台服务(端口 $Port)连不上，小屏会一直停在连接中。请双击启动：$exe"
            }
        }
    }

    $last = Get-LastScreenPoll
    $ageMin = $null
    if ($null -ne $last) { $ageMin = [math]::Round(((Get-Date) - $last).TotalMinutes, 1) }

    if ($listening -and ($null -eq $last -or $ageMin -gt $WarnMinutes)) {
        $pingOk = $false
        try { $pingOk = Test-Connection -ComputerName $ScreenIp -Count 1 -Quiet -ErrorAction Stop } catch {}
        $since = if ($null -eq $last) { '本次开机以来从未收到小屏上报' }
                 else { "已 {0} 分钟没有上报（最后一次 {1:HH:mm:ss}）" -f $ageMin, $last }
        if ($pingOk) {
            $problems += [pscustomobject]@{
                Key   = 'host_filter'
                Title = '小屏在线，但主机收不到它'
                Text  = "小屏 $ScreenIp 能 ping 通，$since。这是主机的网络过滤层卡住了：请重启电脑，重启后会自动恢复，不用动小屏。"
            }
        } else {
            $problems += [pscustomobject]@{
                Key   = 'screen_off'
                Title = '小屏没联网'
                Text  = "$ScreenIp ping 不通，$since。请检查小屏供电(USB)和它连的 WiFi（2.4G KAIXUAN）。"
            }
        }
    }

    try {
        $n = @(Get-NetFirewallRule -DisplayName 'printshop*' -ErrorAction SilentlyContinue).Count
        if ($n -eq 0) {
            $problems += [pscustomobject]@{
                Key   = 'fw_rule'
                Title = '缺防火墙放行规则'
                Text  = "入站 $Port 的放行规则不见了，小屏会连不上。管理员窗口执行：powershell -ExecutionPolicy Bypass -File `"$repo\scripts\fw-slim.ps1`" -Apply"
            }
        }
    } catch {}

    $lastText = if ($null -eq $last) { '从未' } else { $last.ToString('HH:mm:ss') }
    Write-Log ("[check] 服务={0} 最后上报={1} 距今={2}分 问题={3}" -f
        ($(if ($listening) { '在跑' } else { '未运行' })), $lastText,
        ($(if ($null -eq $ageMin) { '-' } else { $ageMin })), $problems.Count)

    return [pscustomobject]@{
        Listening = $listening
        LastPoll  = $last
        AgeMin    = $ageMin
        Problems  = $problems
    }
}

function Send-Problems($problems) {
    if (-not $problems -or $problems.Count -eq 0) { return }
    $state = Read-State
    $warned = $false
    foreach ($p in $problems) {
        $prev = $state.($p.Key)
        $skip = $false
        if ($prev) {
            try {
                $skip = ((Get-Date) - [datetime]::Parse($prev)).TotalMinutes -lt $dedupeMinutes
            } catch { $skip = $false }
        }
        Write-Log ("[warn] {0} - {1}" -f $p.Title, $p.Text)
        if ($skip) { continue }
        Send-Notice $p.Title $p.Text
        $state | Add-Member -NotePropertyName $p.Key -NotePropertyValue ((Get-Date).ToString('o')) -Force
        $warned = $true
    }
    if ($warned) { Save-State $state }
}

# ------------------------------ 主流程 ------------------------------
if ($Loop) {
    # 单实例：已有看门狗在跑就不再起第二份（开机自启与安装脚本可能各拉一次）
    if (Test-Path -LiteralPath $pidFile) {
        $old = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($old -match '^\d+$' -and $old -ne "$PID") {
            $op = Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue
            if ($op -and $op.ProcessName -eq 'powershell') {
                Write-Log ("[loop] 已有看门狗在运行（PID {0}），本实例退出" -f $old)
                return
            }
        }
    }
    $PID | Set-Content -LiteralPath $pidFile -Encoding ASCII
}

if ($Startup) {
    # 登录后让 printshop 自己的自启动先跑完，避免抢跑双开
    Write-Log '[startup] 开机自检：等待 70s 让自服务先启动'
    Start-Sleep -Seconds 70
}

if ($Once) {
    Write-Log '===== 单次自检 ====='
    $r = Invoke-Check
    Send-Problems $r.Problems
    Write-Log ("结论：{0}" -f $(if ($r.Problems.Count -eq 0) { '一切正常' } else { '发现 ' + $r.Problems.Count + ' 个问题（见上）' }))
    return
}

if (-not $Loop) {
    $r = Invoke-Check
    Send-Problems $r.Problems
    return
}

# 常驻模式：登录后自动运行，每 IntervalSeconds 检查一次
Write-Log ("[loop] 看门狗启动，PID={0}，间隔 {1}s，离线阈值 {2} 分钟" -f $PID, $IntervalSeconds, $WarnMinutes)
while ($true) {
    $r = $null
    try {
        $r = Invoke-Check
        Send-Problems $r.Problems
    } catch {
        Write-Log ("[error] 检查异常：{0}" -f $_.Exception.Message)
    }
    Start-Sleep -Seconds $IntervalSeconds
}
