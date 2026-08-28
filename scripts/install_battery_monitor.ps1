# 安装/卸载 自助打印电量监控计划任务。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_battery_monitor.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_battery_monitor.ps1 -Uninstall
#
# 说明：
#   - 任务名：PrintShop_BatteryMonitor
#   - 每 10 分钟运行一次 printshop/battery_track.py，首次在 1 分钟后执行。
#   - 使用 PowerShell 计划任务对象，仓库路径含中文也能正确处理。

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$TaskName = "PrintShop_BatteryMonitor"
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $repo "printshop\battery_track.py"

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output "未发现计划任务：$TaskName（无需卸载）"
    } else {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "已卸载 $TaskName"
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "找不到 Python 解释器：$python"
    exit 1
}
if (-not (Test-Path -LiteralPath $script)) {
    Write-Error "找不到电量监控脚本：$script"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument ('"{0}" --base-url http://127.0.0.1:8000' -f $script) `
    -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Output "已注册 PrintShop_BatteryMonitor（每10分钟）"
