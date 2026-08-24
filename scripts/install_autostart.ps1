# 安装/卸载 自助打印 开机自启（注册表 HKCU Run）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1          # 安装（默认 exe 在脚本同级 ..\dist\printshop.exe）
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1 -ExePath "D:\printshop\printshop.exe"   # 指定 exe
#   powershell -NoProfile -ExecutionPolicy Bypass -File install_autostart.ps1 -Uninstall                             # 卸载
#
# 说明：
#   - 写当前用户 Run 键（HKCU），无需管理员权限。
#   - 启动项只带 exe 路径不带参数；数据目录（inbox/work/db）由 exe 冻结态
#     chdir 到 exe 同级目录，与启动方式无关。
#   - 开机自启由登录会话触发：店内请配置 Windows 自动登录，
#     并保证打印机驱动/网络打印机随系统就绪。
param(
    [string]$ExePath = "",
    [switch]$Uninstall
)

$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$ValueName = "PrintShop"

if ($Uninstall) {
    if (Test-Path $RunKey) {
        $item = Get-ItemProperty -Path $RunKey
        if ($null -ne $item.PSObject.Properties[$ValueName]) {
            Remove-ItemProperty -Path $RunKey -Name $ValueName -ErrorAction Stop
            Write-Output "已移除开机自启项：$ValueName"
        } else {
            Write-Output "未发现自启项：$ValueName（无需卸载）"
        }
    }
    exit 0
}

if (-not $ExePath) {
    $ExePath = Join-Path $PSScriptRoot "..\dist\printshop.exe"
}
$ExePath = [System.IO.Path]::GetFullPath($ExePath)
if (-not (Test-Path -LiteralPath $ExePath)) {
    Write-Error "找不到 exe：$ExePath（可用 -ExePath 指定）"
    exit 1
}

if (-not (Test-Path $RunKey)) {
    New-Item -Path $RunKey -Force | Out-Null
}
Set-ItemProperty -Path $RunKey -Name $ValueName -Value ('"{0}"' -f $ExePath) -Type String

Write-Output "已安装开机自启：$ExePath"
Write-Output "验证：Get-ItemProperty -Path '$RunKey' -Name $ValueName"
Write-Output "卸载：运行本脚本加 -Uninstall"
