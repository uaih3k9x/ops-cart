# Configure point-to-point install link: static IP on USB NIC + firewall rules
# Usage: powershell -ExecutionPolicy Bypass -File config-net.ps1  (admin)
$ErrorActionPreference = "Stop"

$if = Get-NetAdapter | Where-Object { $_.InterfaceDescription -like "Realtek USB*" -and $_.Status -eq "Up" }
if (-not $if) { throw "USB NIC not found" }
$if = $if[0]
Write-Output ("NIC: " + $if.Name + " (" + $if.InterfaceDescription + ")")

# drop APIPA, set static install IP
netsh interface ipv4 set address name="$($if.Name)" static 192.168.200.1 255.255.255.0 | Out-Null
Get-NetIPAddress -InterfaceIndex $if.ifIndex -AddressFamily IPv4 |
    Select-Object InterfaceAlias, IPAddress | Format-Table -AutoSize

# firewall: PXE ports in
foreach ($r in @("PXE-DHCP", "PXE-TFTP", "PXE-HTTP")) {
    netsh advfirewall firewall delete rule name=$r 2>$null | Out-Null
}
netsh advfirewall firewall add rule name="PXE-DHCP" dir=in action=allow protocol=UDP localport=67 | Out-Null
netsh advfirewall firewall add rule name="PXE-TFTP" dir=in action=allow protocol=UDP localport=69 | Out-Null
netsh advfirewall firewall add rule name="PXE-HTTP" dir=in action=allow protocol=TCP localport=8000 | Out-Null
Write-Output "firewall rules added"
