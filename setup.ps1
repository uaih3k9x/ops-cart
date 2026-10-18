# Rocky 9.8 PXE server setup: mount ISO, copy boot files, copy HTTP repo
# Usage: powershell -ExecutionPolicy Bypass -File setup.ps1  (admin)
$ErrorActionPreference = "Stop"

$iso = "D:\pxe\Rocky-9.8-x86_64-minimal.iso"
New-Item -ItemType Directory -Force -Path D:\pxe\tftp, D:\pxe\http | Out-Null

$mounted = Mount-DiskImage -ImagePath $iso -ErrorAction Stop
$dl = ($mounted | Get-Volume).DriveLetter
if (-not $dl) { throw "no drive letter after mount" }
$src = "${dl}:"
Write-Output "ISO mounted at $src"

# TFTP boot files (UEFI: shim + grub + kernel + initrd)
Copy-Item "$src\EFI\BOOT\BOOTX64.EFI"    D:\pxe\tftp\shimx64.efi -Force
Copy-Item "$src\EFI\BOOT\grubx64.efi"     D:\pxe\tftp\grubx64.efi -Force
Copy-Item "$src\images\pxeboot\vmlinuz"     D:\pxe\tftp\vmlinuz -Force
Copy-Item "$src\images\pxeboot\initrd.img"   D:\pxe\tftp\initrd.img -Force
Get-ChildItem D:\pxe\tftp | Select-Object Name, Length | Format-Table -AutoSize

# grub embedded prefix (decides where grub.cfg goes)
$bytes = [IO.File]::ReadAllBytes("D:\pxe\tftp\grubx64.efi")
$text = [Text.Encoding]::ASCII.GetString($bytes)
foreach ($pat in @("(tftp", "EFI/rocky", "EFI/BOOT", "boot/grub2")) {
    $i = $text.IndexOf($pat)
    if ($i -ge 0) { Write-Output ("grub prefix hint: " + $pat + " @ " + $i) }
}

# HTTP repo: whole ISO content (.treeinfo, BaseOS, LiveOS)
if (-not (Test-Path D:\pxe\http\rocky\.treeinfo)) {
    robocopy $src D:\pxe\http\rocky /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed code=$LASTEXITCODE" }
}
Write-Output ("HTTP repo top-level entries: " + (Get-ChildItem D:\pxe\http\rocky).Count)

Dismount-DiskImage -ImagePath $iso | Out-Null
Write-Output "setup done"
