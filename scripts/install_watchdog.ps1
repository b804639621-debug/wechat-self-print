# 安装/卸载 自助打印看门狗（开机自检 + 小屏离线提醒）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1          # 安装并立即自检一次
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1 -Test    # 只自检，不安装
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_watchdog.ps1 -Uninstall
#
# 说明：
#   - 不需要管理员权限：只用当前用户 HKCU\...\Run + 一个隐藏窗口的 .vbs 启动器。
#   - 启动器放 %LOCALAPPDATA%\PrintShop\watchdog-launch.vbs，不污染仓库（重新构建 dist 也不会被删）。
#   - 常驻进程就是 powershell，PID 记在 dist\logs\watchdog.pid；日志 dist\logs\watchdog.log。
#   - 卸载会同时杀掉常驻进程；本脚本不会再拉起它。
param(
    [switch]$Uninstall,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$watchdog = Join-Path $repo 'scripts\watchdog.ps1'
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$valueName = 'PrintShopWatchdog'
$launcherDir = Join-Path $env:LOCALAPPDATA 'PrintShop'
$launcher = Join-Path $launcherDir 'watchdog-launch.vbs'
$pidFile = Join-Path $repo 'dist\logs\watchdog.pid'

function Get-RunValue {
    if (-not (Test-Path $runKey)) { return $null }
    $item = Get-ItemProperty -Path $runKey
    if ($null -eq $item.PSObject.Properties[$valueName]) { return $null }
    return $item.PSObject.Properties[$valueName].Value
}

function Stop-Watchdog {
    if (Test-Path -LiteralPath $pidFile) {
        $procId = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($procId -match '^\d+$') {
            $p = Get-Process -Id ([int]$procId) -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -eq 'powershell') {
                Stop-Process -Id $p.Id -Force
                Write-Output "已停止常驻看门狗进程 PID=$($p.Id)"
            }
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

if ($Uninstall) {
    if ($null -ne (Get-RunValue)) {
        Remove-ItemProperty -Path $runKey -Name $valueName -ErrorAction SilentlyContinue
        Write-Output "已移除自启动项：$valueName"
    } else {
        Write-Output "未发现自启动项：$valueName（无需卸载）"
    }
    Stop-Watchdog
    if (Test-Path -LiteralPath $launcher) {
        Remove-Item -LiteralPath $launcher -Force -ErrorAction SilentlyContinue
        Write-Output "已删除启动器：$launcher"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $watchdog)) {
    Write-Error "找不到看门狗脚本：$watchdog"
    exit 1
}

if (-not $Test) {
    if (-not (Test-Path -LiteralPath $launcherDir)) {
        New-Item -ItemType Directory -Path $launcherDir -Force | Out-Null
    }
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $ps)) { $ps = 'powershell.exe' }
    $vbs = @(
        "' 由 printshop install_watchdog.ps1 生成：隐藏窗口拉起看门狗，不要手工改。",
        'Dim sh, cmd',
        'Set sh = CreateObject("WScript.Shell")',
        ('cmd = "{0} -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File ""{1}"" -Loop -Startup"' -f $ps, $watchdog),
        'sh.Run cmd, 0, False'
    ) -join "`r`n"
    # wscript 需要 UTF-16LE + BOM 才认中文路径
    [System.IO.File]::WriteAllText($launcher, $vbs, (New-Object System.Text.UnicodeEncoding($false, $true)))

    if (-not (Test-Path $runKey)) { New-Item -Path $runKey -Force | Out-Null }
    $cmdLine = '"{0}" "{1}"' -f (Join-Path $env:SystemRoot 'System32\wscript.exe'), $launcher
    Set-ItemProperty -Path $runKey -Name $valueName -Value $cmdLine -Type String
    Write-Output "已安装开机自检/离线提醒：$cmdLine"
    Write-Output "启动器：$launcher"
    Write-Output '下次登录自动生效；本窗口结束前会先跑一次自检。'
}

# 立即自检一次（-Test 也走这里），并顺带拉起常驻进程
if ($Test) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdog -Once
} else {
    Stop-Watchdog
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdog -Once
    Start-Process -FilePath 'wscript.exe' -ArgumentList @($launcher) | Out-Null
    $started = $false
    foreach ($i in 1..15) {
        Start-Sleep -Seconds 1
        if (Test-Path -LiteralPath $pidFile) { $started = $true; break }
    }
    if ($started) {
        Write-Output ("常驻看门狗已启动，PID={0}" -f (Get-Content -LiteralPath $pidFile -Raw).Trim())
    } else {
        Write-Output '警告：常驻进程 15s 内没起来，请查看 dist\logs\watchdog.log'
    }
}
