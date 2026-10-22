# 防火墙规则瘦身：清理「同名字 + 同过滤器参数」的冗余规则，降低过滤引擎(WFP)
# 启动时的事务负载。
#
# 背景（2026-10 实测）：本机每次开机都会在 Microsoft-Windows-WFP/Operational
# 打 Id=1030「Transaction Watchdog Timeout」（Transaction Time 1000ms > 阈值 500ms），
# 紧接着 Windows Firewall 打 Id=2059「已从配置中删除所有规则」+ 几十条 2084
# 「添加了重复规则」，规则表整表重建。2026-10-16 那次开机重建没走完，
# 入站 TCP 被默认丢弃（ICMP 走内建规则组仍放行），表现为「小屏 ping 得通、
# 连不上 18080、服务日志零请求」，重启电脑即恢复。
# 规则总数 1154 条中 263 组同名、合计 861 条重复，正是 1030 里说的
# "suboptimal policy configuration"。
#
# 用法（本脚本只读演练不需要管理员，-Apply 需要管理员）：
#   # 1) 演练（不改任何东西，随时可跑）
#   powershell -NoProfile -ExecutionPolicy Bypass -File E:\kaifa\自助打印\scripts\fw-slim.ps1
#   # 2) 看过数字后再真正执行（自动全量备份）
#   powershell -NoProfile -ExecutionPolicy Bypass -File E:\kaifa\自助打印\scripts\fw-slim.ps1 -Apply
#   # 3) 顺带清理「程序文件已不存在」的死规则（更激进，默认关闭）
#   powershell -NoProfile -ExecutionPolicy Bypass -File E:\kaifa\自助打印\scripts\fw-slim.ps1 -Apply -IncludeStale
#
# 可逆：-Apply 会先 netsh advfirewall export 全量备份到 dist\logs\fw-backup-<时间>.wfw，
# 恢复：netsh advfirewall import "<备份文件>"（管理员）。

param(
    [switch]$Apply,
    [switch]$IncludeStale,
    [string]$BackupDir = "",
    # 命中这些名字的规则永不删除（大小写不敏感的正则）
    [string[]]$KeepNamePattern = @('printshop', '远程桌面', 'Remote Desktop',
                                   '文件共享', 'File and Printer Sharing', 'Core Networking')
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if (-not $BackupDir) { $BackupDir = Join-Path $repo 'dist\logs' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$reportLog = Join-Path $BackupDir ("fw-slim-{0}.log" -f $stamp)

function Say([string]$msg) {
    Write-Host $msg
    try { Add-Content -LiteralPath $reportLog -Value $msg -Encoding UTF8 -ErrorAction SilentlyContinue } catch {}
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-FilterMap([string]$cmdlet, [string[]]$props) {
    # 一次性取某类过滤器的全部实例（InstanceID = 规则 Name），返回 hash。
    $map = @{}
    try {
        $items = & $cmdlet -PolicyStore ActiveStore -ErrorAction Stop
    } catch {
        return $map
    }
    foreach ($it in $items) {
        $vals = foreach ($p in $props) {
            $v = $it.$p
            if ($null -eq $v) { '' } elseif ($v -is [array]) { ($v -join ',') } else { [string]$v }
        }
        $map[[string]$it.InstanceID] = ($vals -join ',')
    }
    return $map
}

Say '================ 防火墙规则瘦身 ================'
Say ("时间：{0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
Say ("模式：{0}{1}" -f ($(if ($Apply) { '执行（会删除规则）' } else { '演练（不修改）' }),
                        $(if ($IncludeStale) { ' + 清理失效程序规则' } else { '' })))

if (-not (Test-Path -LiteralPath $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null }

$rules = @(Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object {
    $_.PolicyStoreSourceType -eq 'Local'
})
Say ("本地规则总数：{0}" -f $rules.Count)

# 过滤器参数：决定「两条同名规则是否真的等价」，缺一不可（少一项就可能删错）
$maps = @{
    Program   = Get-FilterMap 'Get-NetFirewallApplicationFilter' @('Program')
    Port      = Get-FilterMap 'Get-NetFirewallPortFilter' @('Protocol', 'LocalPort', 'RemotePort', 'IcmpType')
    Address   = Get-FilterMap 'Get-NetFirewallAddressFilter' @('LocalAddress', 'RemoteAddress')
    Security  = Get-FilterMap 'Get-NetFirewallSecurityFilter' @('Authentication', 'Encryption', 'LocalUser', 'RemoteUser', 'RemoteMachine')
    Service   = Get-FilterMap 'Get-NetFirewallServiceFilter' @('Service')
    IfType    = Get-FilterMap 'Get-NetFirewallInterfaceTypeFilter' @('InterfaceType')
    IfAlias   = Get-FilterMap 'Get-NetFirewallInterfaceFilter' @('InterfaceAlias')
}

function Get-Sig($r) {
    $parts = @($r.DisplayName, $r.Direction, $r.Action, $r.Profile, $r.PolicyStoreSourceType)
    foreach ($k in 'Program', 'Port', 'Address', 'Security', 'Service', 'IfType', 'IfAlias') {
        $v = $maps[$k][[string]$r.Name]
        if ($null -eq $v) { $v = '' }
        $parts += $v
    }
    return ($parts -join '|')
}

$groups = @{}
foreach ($r in $rules) {
    $sig = Get-Sig $r
    if (-not $groups.ContainsKey($sig)) { $groups[$sig] = New-Object System.Collections.ArrayList }
    [void]$groups[$sig].Add($r)
}

$keepRe = ($KeepNamePattern | ForEach-Object { [regex]::Escape($_) }) -join '|'

$toRemove = New-Object System.Collections.ArrayList
$dupGroups = 0
$protectedGroups = 0
$groupReport = New-Object System.Collections.ArrayList

foreach ($sig in $groups.Keys) {
    $g = $groups[$sig]
    if ($g.Count -le 1) { continue }
    $dupGroups++
    if ($g[0].DisplayName -match $keepRe) { $protectedGroups++; continue }

    # 保留哪一个：启用优先 > 程序文件存在优先 > Name 字典序（稳定可复现）
    $keep = $g | Sort-Object -Property `
            @{ Expression = { if ($_.Enabled -eq 'True' -or $_.Enabled -eq $true) { 0 } else { 1 } } },
            @{ Expression = {
                    $p = $maps.Program[[string]$_.Name]
                    if ($p -and $p -ne 'System' -and (Test-Path -LiteralPath $p -ErrorAction SilentlyContinue)) { 0 } else { 1 }
               } },
            @{ Expression = { [string]$_.Name } } | Select-Object -First 1

    $victims = @($g | Where-Object { $_.Name -ne $keep.Name })
    foreach ($v in $victims) { [void]$toRemove.Add($v) }
    if ($g.Count -ge 3) {
        [void]$groupReport.Add(("{0}  x{1}（保留 {2}）" -f $g[0].DisplayName, $g.Count, $keep.Name))
    }
}

Say ("重复组数：{0}（其中 {1} 组命中保护名单，跳过）" -f $dupGroups, $protectedGroups)
Say ("可删除的重复规则：{0} 条（去重后剩 {1} 条）" -f $toRemove.Count, ($rules.Count - $toRemove.Count))

$stale = @()
if ($IncludeStale) {
    foreach ($r in $rules) {
        if ($toRemove.Name -contains $r.Name) { continue }
        if ($r.DisplayName -match $keepRe) { continue }
        $p = $maps.Program[[string]$r.Name]
        if (-not $p -or $p -eq 'System' -or $p -eq 'Any') { continue }
        if ($p -notmatch '^[A-Za-z]:\\') { continue }
        if (-not (Test-Path -LiteralPath $p -ErrorAction SilentlyContinue)) { $stale += $r }
    }
    Say ("程序文件已不存在的死规则：{0} 条" -f $stale.Count)
}

if ($groupReport.Count -gt 0) {
    Say ''
    Say '--- 重复最多的组（前 20）---'
    $groupReport | Select-Object -First 20 | ForEach-Object { Say ("  " + $_) }
}

if (-not $Apply) {
    Say ''
    Say '以上为演练结果，未修改任何东西。'
    Say '确认后加 -Apply 执行（会自动备份）：'
    Say '  powershell -NoProfile -ExecutionPolicy Bypass -File "E:\kaifa\自助打印\scripts\fw-slim.ps1" -Apply'
    Say ("报告已写入：{0}" -f $reportLog)
    return
}

if (-not (Test-Admin)) {
    Write-Host ''
    Write-Host '错误：-Apply 需要管理员权限。请用「终端(管理员)」重开后再执行。' -ForegroundColor Red
    return
}

# ---------- 备份（全量策略导出 + 规则清单 CSV）----------
$wfw = Join-Path $BackupDir ("fw-backup-{0}.wfw" -f $stamp)
$csv = Join-Path $BackupDir ("fw-rules-{0}.csv" -f $stamp)
Say ''
Say '--- 备份 ---'
& netsh advfirewall export $wfw | Out-Null
if (Test-Path -LiteralPath $wfw) {
    Say ("策略备份：{0}（{1:N0} KB）" -f $wfw, ((Get-Item $wfw).Length / 1KB))
} else {
    Say '警告：netsh advfirewall export 未生成备份文件，已中止（不冒险删除）。'
    return
}
$rules | Select-Object DisplayName, Name, DisplayGroup, Direction, Action, Profile, Enabled,
        @{ n = 'Program'; e = { $maps.Program[[string]$_.Name] } },
        @{ n = 'Port'; e = { $maps.Port[[string]$_.Name] } } |
        Export-Csv -LiteralPath $csv -NoTypeInformation -Encoding UTF8
Say ("规则清单：{0}" -f $csv)
$howto = Join-Path $BackupDir 'restore-howto.txt'
@(
    '防火墙规则恢复方法（管理员 PowerShell）：',
    ('  netsh advfirewall import "{0}"' -f $wfw),
    '恢复后执行 gpupdate /force 并重启一次，确保过滤引擎重新装载。',
    ('备份时间：{0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
) | Set-Content -LiteralPath $howto -Encoding UTF8

# ---------- 执行删除 ----------
Say ''
Say '--- 删除重复规则 ---'
$all = @($toRemove) + @($stale)
$done = 0
$failed = 0
foreach ($r in $all) {
    try {
        Remove-NetFirewallRule -Name $r.Name -ErrorAction Stop
        $done++
    } catch {
        $failed++
        Say ("  失败：{0} / {1} -> {2}" -f $r.DisplayName, $r.Name, $_.Exception.Message)
    }
}
Say ("已删除 {0} 条，失败 {1} 条" -f $done, $failed)

# ---------- 补一条明确的 18080 入站放行（幂等）----------
$exe = Join-Path $repo 'dist\printshop.exe'
$needRule = -not (Get-NetFirewallRule -DisplayName 'printshop 18080 inbound' -ErrorAction SilentlyContinue)
if ($needRule -and (Test-Path -LiteralPath $exe)) {
    try {
        New-NetFirewallRule -DisplayName 'printshop 18080 inbound' -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort 18080 -Profile Any -Program $exe | Out-Null
        Say '已补建入站规则：printshop 18080 inbound（TCP 18080，所有配置文件）'
    } catch {
        Say ("补建 18080 规则失败：{0}" -f $_.Exception.Message)
    }
} else {
    Say '入站规则 printshop 18080 inbound 已存在（未重复添加）'
}

# ---------- 复核 ----------
$after = @(Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object { $_.PolicyStoreSourceType -eq 'Local' })
$afterGroups = @{}
foreach ($r in $after) {
    $sig = Get-Sig $r
    if (-not $afterGroups.ContainsKey($sig)) { $afterGroups[$sig] = 0 }
    $afterGroups[$sig]++
}
$afterDup = @($afterGroups.Values | Where-Object { $_ -gt 1 })
Say ''
Say ("复核：规则 {0} 条 -> {1} 条；仍有重复组 {2} 组" -f $rules.Count, $after.Count, $afterDup.Count)
Say ''
Say '下一步：重启一次电脑，然后看是否还有 1030 事件（管理员 PowerShell）：'
Say '  Get-WinEvent -LogName "Microsoft-Windows-WFP/Operational" -MaxEvents 200 |'
Say '    Where-Object { $_.Id -eq 1030 -and $_.TimeCreated -gt (Get-Date).AddMinutes(-10) }'
Say '没有输出 = 事务超时已消失，本次瘦身生效。'
Say ("报告已写入：{0}" -f $reportLog)
