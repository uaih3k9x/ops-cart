# Start PXE services via WMI (survives ssh session). Idempotent-ish: kills old instances first.
# Usage: powershell -ExecutionPolicy Bypass -File start-pxe.ps1
# 需要 D:\pxe 下有 pxeserver.py + httpserve.py. 示例地址, 按实际装机网段改.
# 菜单和 ks 只发给 pxe-mode.json 里解锁的机器; Windows 上没有解锁入口, 所以这套只提供救援.
$ErrorActionPreference = "Stop"

# kill previous instances (matched by command line)
Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -match "pxeserver\.py" -or $_.CommandLine -match "httpserve\.py" -or
    ($_.CommandLine -match "http\.server" -and $_.CommandLine -match "pxe")
} | ForEach-Object {
    Write-Output ("stopping old pid " + $_.ProcessId)
    Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null
}

Start-Sleep -Milliseconds 500
Remove-Item D:\pxe\pxeserver.log, D:\pxe\http.log -ErrorAction SilentlyContinue

$py = "C:\Program Files\Python313\python.exe"
$wmi = @{MethodName = "Create"}

# DHCP + TFTP
$r = Invoke-CimMethod Win32_Process @wmi -Arguments @{
    CommandLine = "`"$py`" D:\pxe\pxeserver.py serve --ip 192.0.2.1 --prefix 24 --pool 192.0.2.10,192.0.2.20 --tftp-root D:\pxe\tftp --log D:\pxe\pxeserver.log --pxe-mode D:\pxe\pxe-mode.json --guard-state D:\pxe\pxe-guard.json"
    CurrentDirectory = "D:\pxe"
}
Write-Output ("pxeserver pid: " + $r.ProcessId)

# HTTP repo + ks (via bat wrapper: WMI + inline redirect quoting is broken)
$r = Invoke-CimMethod Win32_Process @wmi -Arguments @{
    CommandLine = "cmd /c D:\pxe\http-serve.bat"
    CurrentDirectory = "D:\pxe"
}
Write-Output ("http pid: " + $r.ProcessId)

Start-Sleep -Seconds 2
Write-Output "--- listening check ---"
Get-NetUDPEndpoint -LocalPort 67, 69 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
