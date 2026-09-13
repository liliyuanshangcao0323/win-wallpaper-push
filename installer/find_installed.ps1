<#
    Prints the MSI ProductCode of the installed "Win Wallpaper Push Agent".

    Why look it up instead of hard coding it:
      Agent.wxs uses <Product Id="*">, so WiX generates a NEW ProductCode on
      every rebuild. Only the UpgradeCode is stable. Looking the product up by
      Publisher keeps the uninstall script working across rebuilds.

    Output : the ProductCode, e.g. {4075B980-939C-477D-9987-3FF90D6D0DFC}
    Exit   : 0 when found, 1 when the product is not installed.
#>
$ErrorActionPreference = 'SilentlyContinue'

$uninstallRoot = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'

$found = Get-ChildItem $uninstallRoot |
    Where-Object { $_.GetValue('Publisher') -eq 'WinWallpaperPush' } |
    Select-Object -First 1

if ($found) {
    Write-Output $found.PSChildName
    exit 0
}

exit 1
