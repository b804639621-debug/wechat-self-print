# 提交 XPS 到打印机：PrintQueue.CreateXpsDocumentWriter().Write + PrintTicket。
#
# 由 printshop/xps_print.py 调用（subprocess）。PrintTicket 由 Python 生成 XML 传入，
# 本脚本反序列化后经 MergeAndValidatePrintTicket 调和，最后统一用 XpsDocumentWriter
# 提交（禁用 AddJob——柯美 PCL 队列上挂起且任务不创建，M1 §8 坑 3）。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File submit_xps.ps1 `
#     -XpsPath <file.xps> -Printer <name> -TicketPath <ticket.xml>
# 退出码：0=成功；4=打印机不存在；其余非 0=失败（stderr 带详情）。
param(
    [Parameter(Mandatory = $true)][string]$XpsPath,
    [Parameter(Mandatory = $true)][string]$Printer,
    [Parameter(Mandatory = $true)][string]$TicketPath
)

$ErrorActionPreference = 'Stop'

# 中文错误消息以 UTF-8 输出，避免 Python 侧 utf-8 解码乱码（Windows 控制台默认 CP936）。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $errWriter = New-Object System.IO.StreamWriter([Console]::OpenStandardError(), $utf8NoBom)
    $errWriter.AutoFlush = $true
    [Console]::SetError($errWriter)
} catch { }

Add-Type -AssemblyName System.Printing
Add-Type -AssemblyName ReachFramework

$queue = $null
try {
    $server = New-Object System.Printing.LocalPrintServer
    $queue = $server.GetPrintQueue($Printer)
} catch {
    [Console]::Error.WriteLine("获取打印队列失败: $($_.Exception.Message)")
    exit 1
}
if ($null -eq $queue) {
    [Console]::Error.WriteLine("打印机不存在: $Printer")
    exit 4
}

$ticket = $null
try {
    $fs = [System.IO.File]::OpenRead($TicketPath)
    try {
        $ticket = [System.Printing.PrintTicket]::new($fs)
    } finally {
        $fs.Dispose()
    }
} catch {
    [Console]::Error.WriteLine("读取 PrintTicket 失败: $($_.Exception.Message)")
    exit 1
}

try {
    $merged = $queue.MergeAndValidatePrintTicket($queue.UserPrintTicket, $ticket)
    $writer = [System.Printing.PrintQueue]::CreateXpsDocumentWriter($queue)
    $xps = New-Object System.Windows.Xps.Packaging.XpsDocument($XpsPath, [System.IO.FileAccess]::Read)
    try {
        $seq = $xps.GetFixedDocumentSequence()
        $writer.Write($seq, $merged.ValidatedPrintTicket)
    } finally {
        $xps.Close()
    }
} catch {
    [Console]::Error.WriteLine("打印提交失败: $($_.Exception.Message)")
    exit 1
}
Write-Output "OK"
exit 0
