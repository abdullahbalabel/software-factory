<#
.SYNOPSIS
  Isolation proof of the Windows hand, run AS the hand (a gates job with runs-on: windows, shell: powershell).
  Prints one line per check and exits 1 if any check fails. See docs/specs/security.md#hands.
#>
param([string[]]$Denied = @('D:\projects', 'D:\secrets', 'C:\Users\owner'))
$failed = 0
function Check($name, [scriptblock]$ok) {
  try { $r = & $ok } catch { $r = $false }
  if ($r) { "PASS  $name" } else { "FAIL  $name"; $script:failed++ }
}
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$p = [Security.Principal.WindowsPrincipal]$id
"runs as: $($id.Name)"
Check 'runs as factory-hand' { $id.Name -like '*\factory-hand' }
Check 'not an administrator' { -not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
Check 'not in Remote Desktop Users / Hyper-V Administrators / docker-users' {
  $groups = $id.Groups | ForEach-Object { $_.Value }
  -not ($groups -contains 'S-1-5-32-555' -or $groups -contains 'S-1-5-32-578' -or
        ($groups | Where-Object { $_ -match '^S-1-5-21-' } | ForEach-Object {
          (New-Object Security.Principal.SecurityIdentifier $_).Translate([Security.Principal.NTAccount]).Value
        }) -match 'docker-users')
}
foreach ($d in $Denied) {
  Check "cannot list $d" {
    try { Get-ChildItem -LiteralPath $d -Force -ErrorAction Stop | Out-Null; $false }
    catch [UnauthorizedAccessException] { $true }
    catch { $_.Exception -is [UnauthorizedAccessException] -or $_.CategoryInfo.Category -eq 'PermissionDenied' }
  }
}
Check 'cannot read a file under D:\projects by full path' {
  $f = 'D:\projects\software-factory\README.md'
  try { [IO.File]::ReadAllText($f) | Out-Null; $false } catch { $_.Exception.InnerException -is [UnauthorizedAccessException] }
}
Check 'cannot reach the Docker engine pipe' {
  try { $c = New-Object IO.Pipes.NamedPipeClientStream '.', 'docker_engine', 'InOut'; $c.Connect(2000); $c.Dispose(); $false }
  catch { $true }
}
Check 'cannot manage Hyper-V' {
  try { Get-VM -ErrorAction Stop | Out-Null; $false } catch { $true }
}
Check 'the runner listens on no TCP port' {
  $pids = @(Get-Process act_runner, factory-hand-service -ErrorAction SilentlyContinue | ForEach-Object Id)
  $pids.Count -gt 0 -and -not (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                               Where-Object { $pids -contains $_.OwningProcess })
}
if ($failed) { "$failed check(s) failed"; exit 1 }
'all isolation checks passed'
