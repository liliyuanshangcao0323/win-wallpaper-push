<#
    Dumps the important MSI tables of an .msi file WITHOUT installing it.

    Uses the Windows Installer COM automation API directly, so:
      * no administrator rights needed
      * nothing is installed, no registry key is written

    Column indices in the MSI API are 1-based, and the column order differs per
    table, so the column names are read from the _Columns table instead of being
    hard coded.
#>
param(
    [Parameter(Mandatory = $true)][string]$Msi
)

$ErrorActionPreference = 'Stop'
$Msi = (Resolve-Path $Msi).Path

$installer = New-Object -ComObject WindowsInstaller.Installer
$db = $installer.GetType().InvokeMember('OpenDatabase', 'InvokeMethod', $null, $installer, @($Msi, 0))

function Invoke-MsiView {
    param([string]$Sql)
    $view = $db.GetType().InvokeMember('OpenView', 'InvokeMethod', $null, $db, @($Sql))
    $view.GetType().InvokeMember('Execute', 'InvokeMethod', $null, $view, $null) | Out-Null
    return $view
}

function Read-MsiRecord {
    param($View, [int]$FieldCount)
    $rec = $View.GetType().InvokeMember('Fetch', 'InvokeMethod', $null, $View, $null)
    if (-not $rec) { return $null }
    $vals = @()
    for ($i = 1; $i -le $FieldCount; $i++) {
        try {
            $vals += [string]$rec.GetType().InvokeMember('StringData', 'GetProperty', $null, $rec, @($i))
        } catch { $vals += '' }
    }
    # The leading comma stops PowerShell from unrolling a 1-element array into
    # a bare string - otherwise $r[0] would later return the first CHARACTER.
    return , $vals
}

function Get-MsiRows {
    param([string]$Table, [string[]]$Want)
    # Resolve the real column order first
    $colView = Invoke-MsiView "SELECT ``Number``,``Name`` FROM ``_Columns`` WHERE ``Table``='$Table'"
    $order = @{}
    $count = 0
    while ($true) {
        $r = Read-MsiRecord -View $colView -FieldCount 2
        if (-not $r) { break }
        $order[$r[1]] = [int]$r[0]
        if ([int]$r[0] -gt $count) { $count = [int]$r[0] }
    }
    $colView.GetType().InvokeMember('Close', 'InvokeMethod', $null, $colView, $null) | Out-Null

    if ($count -eq 0) { return $null }

    $view = Invoke-MsiView "SELECT * FROM ``$Table``"
    $rows = @()
    while ($true) {
        $r = Read-MsiRecord -View $view -FieldCount $count
        if (-not $r) { break }
        $out = [ordered]@{}
        foreach ($w in $Want) {
            if ($order.ContainsKey($w)) { $out[$w] = $r[$order[$w] - 1] } else { $out[$w] = '?' }
        }
        $rows += , $out
    }
    $view.GetType().InvokeMember('Close', 'InvokeMethod', $null, $view, $null) | Out-Null
    return $rows
}

function Get-MsiScalar {
    param([string]$Sql)
    try {
        $view = Invoke-MsiView $Sql
        $r = Read-MsiRecord -View $view -FieldCount 1
        $view.GetType().InvokeMember('Close', 'InvokeMethod', $null, $view, $null) | Out-Null
        if ($r) { return $r[0] }
    } catch { }
    return $null
}

function Show-Rows {
    param($Rows, [string[]]$Props, [string[]]$Headers)
    if (-not $Rows) { Write-Host '   (empty)'; return }
    $parts = @()
    for ($i = 0; $i -lt $Headers.Count; $i++) { $parts += "{$i,-26}" }
    $fmt = '   ' + ($parts -join ' ')
    Write-Host ($fmt -f $Headers)
    foreach ($row in $Rows) {
        $vals = @()
        foreach ($p in $Props) { $vals += [string]$row[$p] }
        Write-Host ($fmt -f $vals)
    }
}

Write-Host ('=' * 78)
Write-Host "  MSI STATIC VALIDATION: $(Split-Path -Leaf $Msi)"
Write-Host ('=' * 78)
Write-Host ("  size: {0:N0} bytes" -f (Get-Item $Msi).Length)

Write-Host "`n[Property table]"
foreach ($p in 'ProductName', 'ProductVersion', 'ProductLanguage', 'Manufacturer',
               'ProductCode', 'UpgradeCode', 'ALLUSERS', 'ARPCOMMENTS') {
    $v = Get-MsiScalar "SELECT ``Value`` FROM ``Property`` WHERE ``Property``='$p'"
    if ($v) { Write-Host ("   {0,-16} {1}" -f $p, $v) }
}

Write-Host "`n[Directory table]"
Show-Rows (Get-MsiRows 'Directory' @('Directory', 'Directory_Parent', 'DefaultDir')) `
          @('Directory', 'Directory_Parent', 'DefaultDir') `
          @('Directory', 'Directory_Parent', 'DefaultDir')

Write-Host "`n[File table]"
$f = Get-MsiRows 'File' @('File', 'Component_', 'FileName', 'FileSize', 'Sequence')
if ($f) {
    foreach ($r in $f) {
        Write-Host ("   {0,-16} comp={1,-14} name={2,-26} size={3}" -f $r['File'], $r['Component_'], $r['FileName'], $r['FileSize'])
    }
} else { Write-Host '   (empty)' }

Write-Host "`n[Component table]"
Show-Rows (Get-MsiRows 'Component' @('Component', 'ComponentId', 'Directory_', 'Attributes')) `
          @('Component', 'ComponentId', 'Directory_', 'Attributes') `
          @('Component', 'ComponentId', 'Directory_', 'Attributes')

Write-Host "`n[Registry table - autostart]"
$reg = Get-MsiRows 'Registry' @('Registry', 'Root', 'Key', 'Name', 'Value')
if ($reg) {
    foreach ($r in $reg) {
        Write-Host ("   Root={0}  Key={1}" -f $r['Root'], $r['Key'])
        Write-Host ("   Name={0}" -f $r['Name'])
        Write-Host ("   Value={0}" -f $r['Value'])
    }
} else { Write-Host '   (empty)' }

Write-Host "`n[CustomAction table]"
$ca = Get-MsiRows 'CustomAction' @('Action', 'Type', 'Source', 'Target')
if ($ca) {
    foreach ($r in $ca) {
        if ($r['Action'] -notmatch 'Wix|Sched|Exec|Rollback') {
            Write-Host ("   {0,-16} Type={1,-6} Source={2}" -f $r['Action'], $r['Type'], $r['Source'])
            Write-Host ("        Target={0}" -f $r['Target'])
        }
    }
    Write-Host ("   ... plus {0} WiX-generated extension actions" -f (($ca | Where-Object { $_.Action -match 'Wix|Sched|Exec|Rollback' }).Count))
}

Write-Host "`n[InstallExecuteSequence - our custom action position]"
try {
    $view = Invoke-MsiView "SELECT ``Action``,``Condition``,``Sequence`` FROM ``InstallExecuteSequence`` ORDER BY ``Sequence``"
    while ($true) {
        $r = Read-MsiRecord -View $view -FieldCount 3
        if (-not $r) { break }
        if ($r[0] -match 'CA_KillAgent|InstallFiles|InstallInitialize|RemoveFiles|WixSchedFirewall') {
            Write-Host ("   seq={0,-6} {1,-34} cond=[{2}]" -f $r[2], $r[0], $r[1])
        }
    }
    $view.GetType().InvokeMember('Close', 'InvokeMethod', $null, $view, $null) | Out-Null
} catch { Write-Host "   (failed: $_)" }

Write-Host "`n[Firewall exception table]"
$fwTable = $null
$allTables = @()
$tblView = Invoke-MsiView "SELECT ``Name`` FROM ``_Tables``"
while ($true) {
    $r = Read-MsiRecord -View $tblView -FieldCount 1
    if (-not $r) { break }
    $allTables += $r[0]
    if ($r[0] -match 'irewall') { $fwTable = $r[0] }
}
$tblView.GetType().InvokeMember('Close', 'InvokeMethod', $null, $tblView, $null) | Out-Null

if ($fwTable) {
    Write-Host "   table: $fwTable"
    $colView = Invoke-MsiView "SELECT ``Name`` FROM ``_Columns`` WHERE ``Table``='$fwTable' ORDER BY ``Number``"
    while ($true) {
        $r = Read-MsiRecord -View $colView -FieldCount 1
        if (-not $r) { break }
        Write-Host "   column: $($r[0])"
    }
    $colView.GetType().InvokeMember('Close', 'InvokeMethod', $null, $colView, $null) | Out-Null

    $view = Invoke-MsiView "SELECT * FROM ``$fwTable``"
    $rec = $view.GetType().InvokeMember('Fetch', 'InvokeMethod', $null, $view, $null)
    if ($rec) {
        for ($i = 1; $i -le 12; $i++) {
            try {
                $v = [string]$rec.GetType().InvokeMember('StringData', 'GetProperty', $null, $rec, @($i))
                if ($v) { Write-Host ("     field[{0}] = {1}" -f $i, $v) }
            } catch { }
        }
    } else { Write-Host '   (no rows)' }
    $view.GetType().InvokeMember('Close', 'InvokeMethod', $null, $view, $null) | Out-Null
} else {
    Write-Host '   !! no firewall table found. All tables:'
    Write-Host ('   ' + ($allTables -join ', '))
}

Write-Host "`n[Media / cabinet]"
Show-Rows (Get-MsiRows 'Media' @('DiskId', 'LastSequence', 'Cabinet')) `
          @('DiskId', 'LastSequence', 'Cabinet') @('DiskId', 'LastSequence', 'Cabinet')

Write-Host "`n" + ('=' * 78)
