# dist\printshop.exe 启动冒烟测试（Task 12 审查建议固化；Task 10B 增补 BLE 自检）。
# 流程：起 exe → HTTP 探活 /admin /screen /api/screen/state /static/screen.html →
#       强杀 HTTP 实例 → 同 exe 跑一次 --ble-selftest（真实连小屏，屏短暂亮/灭）→ 汇总报告。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_dist.ps1
#       [-ExePath <路径>] [-TimeoutSec 30] [-Port 8000] [-SkipBle]
#   -SkipBle：跳过 BLE 自检（无蓝牙/无小屏环境回退旧行为；默认执行）。
#   注意：BLE 自检是独立进程，不与 HTTP 实例并存（先杀 HTTP 再跑）；跑前需确认
#   无生产 printshop.exe 在跑（两个 BLE Central 抢小屏会互相干扰）。
param(
    [string]$ExePath = "",
    [int]$TimeoutSec = 30,
    [int]$Port = 8000,
    [switch]$SkipBle
)

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot "..\dist\printshop.exe"
}
$ExePath = [System.IO.Path]::GetFullPath($ExePath)
if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "找不到 exe：$ExePath（先运行 pyinstaller printshop.spec）"
    exit 1
}

# ---- 阶段 1：HTTP 探活 -------------------------------------------------------------
$proc = $null
$failed = $false
try {
    $proc = Start-Process -FilePath $ExePath -PassThru -WindowStyle Hidden
    Write-Output "已启动 printshop.exe (PID $($proc.Id))，等待服务就绪（最长 ${TimeoutSec}s，onefile 首次解压较慢）..."

    $ready = $false
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/screen/state" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Milliseconds 800 }
    }
    if (-not $ready) {
        Write-Error "FAIL：服务未在 ${TimeoutSec}s 内就绪"
        $failed = $true
    } else {
        foreach ($path in @("/admin", "/screen", "/api/screen/state", "/static/screen.html")) {
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port$path" -UseBasicParsing -TimeoutSec 5
                Write-Output ("OK   {0} -> {1}" -f $path, $r.StatusCode)
            } catch {
                Write-Output ("FAIL {0} -> {1}" -f $path, $_.Exception.Message)
                $failed = $true
            }
        }
    }
} finally {
    if ($proc -and -not $proc.HasExited) {
        # onefile 有父子两个进程，兜底按父子关系清子进程——绝不按进程名扫杀
        # （本机可能有生产 printshop.exe 在跑，按名字会误杀生产实例）
        $kids = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $proc.Id }
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        foreach ($k in $kids) { Stop-Process -Id $k.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Output "已停止测试进程"
    }
}

if ($failed) {
    Write-Error "冒烟测试未通过（HTTP 阶段失败，跳过 BLE 自检）"
    exit 1
}

# ---- 阶段 2：BLE 自检（独立进程；HTTP 实例已强杀，不与 HTTP 并存） -------------------
if (-not $SkipBle) {
    $bleOut = Join-Path $env:TEMP ("smoke_ble_{0}.out" -f [guid]::NewGuid().ToString("N"))
    $bleErr = Join-Path $env:TEMP ("smoke_ble_{0}.err" -f [guid]::NewGuid().ToString("N"))
    try {
        Write-Output "BLE 自检：$ExePath --ble-selftest（真实连小屏，屏会短暂亮/灭，最长 30s）..."
        $p = Start-Process -FilePath $ExePath -ArgumentList '--ble-selftest' -PassThru -WindowStyle Hidden `
            -RedirectStandardOutput $bleOut -RedirectStandardError $bleErr
        if (-not $p.WaitForExit(30000)) {
            # 超时强杀：先按父 PID 收集子进程再杀（onefile 父子结构），不按名字误伤其他实例
            $kids = Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $p.Id }
            Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
            foreach ($k in $kids) { Stop-Process -Id $k.ProcessId -Force -ErrorAction SilentlyContinue }
            Write-Output "FAIL BLE 自检超时（30s 未退出）"
            $failed = $true
        } else {
            $outText = Get-Content -LiteralPath $bleOut -Raw -ErrorAction SilentlyContinue
            $errText = Get-Content -LiteralPath $bleErr -Raw -ErrorAction SilentlyContinue
            if ($outText) { Write-Output $outText.Trim() }
            if ($errText) { Write-Output ("[stderr] " + $errText.Trim()) }
            if ($p.ExitCode -eq 0 -and $outText -match "BLE SELFTEST PASS") {
                Write-Output "OK   BLE 自检通过（exit=$($p.ExitCode)）"
            } else {
                Write-Output "FAIL BLE 自检未通过（exit=$($p.ExitCode)）"
                $failed = $true
            }
        }
    } finally {
        Remove-Item -LiteralPath $bleOut, $bleErr -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Output "已跳过 BLE 自检（-SkipBle）"
}

if ($failed) { Write-Error "冒烟测试未通过"; exit 1 }
Write-Output "冒烟测试通过"
exit 0
