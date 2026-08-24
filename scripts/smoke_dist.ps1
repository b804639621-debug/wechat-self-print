# dist\printshop.exe 启动冒烟测试（Task 12 审查建议固化）。
# 起 exe 进程 → HTTP 探活 /admin 与 /api/screen/state → 杀进程 → 报告。
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke_dist.ps1 [-ExePath <路径>] [-TimeoutSec 30]
param(
    [string]$ExePath = "",
    [int]$TimeoutSec = 30,
    [int]$Port = 8000
)

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot "..\dist\printshop.exe"
}
$ExePath = [System.IO.Path]::GetFullPath($ExePath)
if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "找不到 exe：$ExePath（先运行 pyinstaller printshop.spec）"
    exit 1
}

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
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        # onefile 有父子两个进程，兜底清子进程
        Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "printshop.exe" -and $_.ProcessId -ne $proc.Id } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Output "已停止测试进程"
    }
}

if ($failed) { Write-Error "冒烟测试未通过"; exit 1 }
Write-Output "冒烟测试通过"
exit 0
