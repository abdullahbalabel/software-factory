<#
.SYNOPSIS
  Installs (or removes) the factory's Windows hand on this PC: Gitea act_runner in host mode, as a Windows service
  running as the dedicated low-privilege local user `factory-hand`, label `windows`, registered to ONE product's
  Gitea organisation. See docs/specs/security.md#hands.

.DESCRIPTION
  Run once, elevated (Windows PowerShell 5.1, "Run as administrator"). Idempotent. Changes on this PC:
  - local user factory-hand: member of Users only (removed from Administrators, Remote Desktop Users, Remote
    Management Users, docker-users, Hyper-V Administrators if present); random password known only to the service
    manager; rights: "Log on as a service"; denied: local, Remote Desktop, network and batch logon.
  - D:\factory-hand (bin read-only for the hand; data, work, logs writable; inheritance from D:\ cut, so other
    local users cannot read the runner's token in data\.runner).
  - deny ACEs (full control, inherited by every child) for factory-hand on each -DenyPath.
  - service factory-hand (WinSW 2.12.0 wrapping act_runner 0.6.1 daemon), automatic (delayed) start.
  Nothing listens: the runner only connects out to Gitea over the tailnet (HTTPS). No Docker, Hyper-V, reboot or
  firewall change.

.PARAMETER TokenFile
  File with the Gitea organisation's runner registration token (deleted after use). On the factory VM:
  T=$(sudo cat /srv/factory/secrets/gitea_ops_token); curl -s -X POST -H "Authorization: token $T" \
    http://127.0.0.1:3000/api/v1/orgs/<org>/actions/runners/registration-token
  Needed only for the first registration (data\.runner absent).

.PARAMETER Uninstall
  Stops and deletes the service, removes the deny ACEs, the account's rights, the user and D:\factory-hand. The
  runner entry in Gitea is left offline; delete it there (organisation settings > Actions > Runners).
#>
[CmdletBinding()]
param(
  [string]$TokenFile,
  [string]$GiteaUrl = 'https://factory-core.tailnet-name.ts.net:8443',
  [string]$RunnerName = 'windows-hand',
  [string]$Root = 'D:\factory-hand',
  [string[]]$DenyPath = @('D:\projects', 'D:\secrets', 'C:\Users\owner'),
  [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$User = 'factory-hand'
$Service = 'factory-hand'
$ActRunner = @{ Url = 'https://dl.gitea.com/act_runner/0.6.1/act_runner-0.6.1-windows-amd64.exe'
                Sha256 = 'f9eee934cfd03d14caabde65603881b9e59f732d89c934aa3e4333ba644a8270' }
$WinSW = @{ Url = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
            Sha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da' }

$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw 'Run this script elevated (Run as administrator).'
}
Start-Transcript -Path "$Root-install.log" -Append | Out-Null

# --- account rights (LSA) --------------------------------------------------------------------------------------
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class FactoryLsa {
  [StructLayout(LayoutKind.Sequential)]
  struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)]
  struct LSA_OBJECT_ATTRIBUTES { public int Length; public IntPtr RootDirectory; public IntPtr ObjectName;
    public int Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
  [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr systemName, ref LSA_OBJECT_ATTRIBUTES attrs,
    int access, out IntPtr handle);
  [DllImport("advapi32.dll")] static extern uint LsaAddAccountRights(IntPtr handle, byte[] sid,
    LSA_UNICODE_STRING[] rights, int count);
  [DllImport("advapi32.dll")] static extern uint LsaRemoveAccountRights(IntPtr handle, byte[] sid, bool all,
    LSA_UNICODE_STRING[] rights, int count);
  [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr handle);
  [DllImport("advapi32.dll")] static extern int LsaNtStatusToWinError(uint status);
  const uint STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034;
  static byte[] Sid(string sddl) {
    var sid = new SecurityIdentifier(sddl); var b = new byte[sid.BinaryLength]; sid.GetBinaryForm(b, 0); return b;
  }
  static void Check(uint status, string what) {
    if (status != 0) throw new System.ComponentModel.Win32Exception(LsaNtStatusToWinError(status), what);
  }
  // add: grant the rights; !add: remove them (all == true removes every right of the account).
  public static void Set(string sddl, string[] rights, bool add, bool all) {
    var attrs = new LSA_OBJECT_ATTRIBUTES(); IntPtr h;
    Check(LsaOpenPolicy(IntPtr.Zero, ref attrs, 0x000F0FFF, out h), "LsaOpenPolicy");
    var us = new LSA_UNICODE_STRING[rights.Length];
    try {
      for (int i = 0; i < rights.Length; i++) {
        us[i].Buffer = Marshal.StringToHGlobalUni(rights[i]);
        us[i].Length = (ushort)(rights[i].Length * 2); us[i].MaximumLength = (ushort)(us[i].Length + 2);
      }
      uint s = add ? LsaAddAccountRights(h, Sid(sddl), us, us.Length)
                   : LsaRemoveAccountRights(h, Sid(sddl), all, all ? null : us, all ? 0 : us.Length);
      if (!(s == STATUS_OBJECT_NAME_NOT_FOUND && !add)) Check(s, add ? "LsaAddAccountRights" : "LsaRemoveAccountRights");
    } finally {
      foreach (var u in us) if (u.Buffer != IntPtr.Zero) Marshal.FreeHGlobal(u.Buffer);
      LsaClose(h);
    }
  }
}
'@
$DenyRights = 'SeDenyInteractiveLogonRight', 'SeDenyRemoteInteractiveLogonRight', 'SeDenyNetworkLogonRight',
              'SeDenyBatchLogonRight'
$Groups = 'S-1-5-32-544', 'S-1-5-32-555', 'S-1-5-32-580', 'S-1-5-32-578'  # Administrators, RDP users, Remote Mgmt, Hyper-V Admins

function Remove-Hand {
  $u = Get-LocalUser $User -ErrorAction SilentlyContinue
  if ($u) {
    foreach ($p in $DenyPath) {
      if (Test-Path -LiteralPath $p) { & icacls.exe $p /remove:d "*$($u.SID.Value)" /C /Q | Out-Null }
    }
    [FactoryLsa]::Set($u.SID.Value, @(), $false, $true)
    Remove-LocalUser $User
  }
  if (Test-Path -LiteralPath $Root) { Remove-Item -LiteralPath $Root -Recurse -Force }
  Write-Host 'Windows hand removed. Delete the offline runner in Gitea (organisation > Settings > Actions > Runners).'
}

if ($Uninstall) { Remove-Hand; Stop-Transcript | Out-Null; return }

# --- user ------------------------------------------------------------------------------------------------------
Add-Type -AssemblyName System.Web
$password = [System.Web.Security.Membership]::GeneratePassword(40, 8)
$secure = ConvertTo-SecureString $password -AsPlainText -Force
$u = Get-LocalUser $User -ErrorAction SilentlyContinue
if ($u) {
  Set-LocalUser $User -Password $secure -PasswordNeverExpires $true
} else {
  $u = New-LocalUser $User -Password $secure -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires `
         -FullName 'Factory hand' -Description 'Software factory Windows hand (Gitea act_runner service); no logon'
}
if (-not (Get-LocalGroupMember -SID 'S-1-5-32-545' -Member $User -ErrorAction SilentlyContinue)) {
  Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $User   # Users: a standard user
}
foreach ($sid in $Groups) {
  if (Get-LocalGroup -SID $sid -ErrorAction SilentlyContinue) {
    Remove-LocalGroupMember -SID $sid -Member $User -ErrorAction SilentlyContinue
  }
}
if (Get-LocalGroup -Name 'docker-users' -ErrorAction SilentlyContinue) {
  Remove-LocalGroupMember -Name 'docker-users' -Member $User -ErrorAction SilentlyContinue
}
[FactoryLsa]::Set($u.SID.Value, @('SeServiceLogonRight'), $true, $false)
[FactoryLsa]::Set($u.SID.Value, $DenyRights, $true, $false)

# --- folders and ACLs ------------------------------------------------------------------------------------------
$acct = "$env:COMPUTERNAME\$User"
foreach ($d in 'bin', 'data', 'work', 'logs') { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
# Root: inheritance from D:\ cut; SYSTEM and Administrators full; the hand read/execute; nobody else.
& icacls.exe $Root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${acct}:(OI)(CI)RX" /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls on $Root failed ($LASTEXITCODE)" }
foreach ($d in 'data', 'work', 'logs') {
  & icacls.exe (Join-Path $Root $d) /grant:r "${acct}:(OI)(CI)M" /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "icacls on $Root\$d failed ($LASTEXITCODE)" }
}
foreach ($p in $DenyPath) {
  if (-not (Test-Path -LiteralPath $p)) { Write-Warning "deny path $p does not exist; skipped"; continue }
  Write-Host "Deny $User on $p (inherited by every child; can take a few minutes on large trees)"
  & icacls.exe $p /deny "${acct}:(OI)(CI)F" /C /Q | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Warning "icacls reported errors on $p (exit $LASTEXITCODE): some children kept their ACL" }
}

# --- binaries (pinned hashes) ----------------------------------------------------------------------------------
function Get-Pinned($spec, $dest) {
  if ((Test-Path $dest) -and (Get-FileHash $dest -Algorithm SHA256).Hash -eq $spec.Sha256) { return }
  $tmp = "$dest.download"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -UseBasicParsing -Uri $spec.Url -OutFile $tmp
  $hash = (Get-FileHash $tmp -Algorithm SHA256).Hash
  if ($hash -ne $spec.Sha256) { Remove-Item $tmp; throw "SHA-256 mismatch for $($spec.Url): $hash" }
  Move-Item -Force $tmp $dest
}
$bin = Join-Path $Root 'bin'
$data = Join-Path $Root 'data'
if (Get-Service $Service -ErrorAction SilentlyContinue) { Stop-Service $Service -ErrorAction SilentlyContinue }
Get-Pinned $ActRunner (Join-Path $bin 'act_runner.exe')
Get-Pinned $WinSW (Join-Path $bin 'factory-hand-service.exe')

# --- runner configuration and registration ---------------------------------------------------------------------
$work = Join-Path $Root 'work'
@"
# act_runner of the factory's Windows hand (written by infra/hands/windows/install-windows-hand.ps1).
log:
  level: info
runner:
  file: $data\.runner
  capacity: 1
  timeout: 1h
  labels:
    - "windows:host"
cache:
  enabled: false
host:
  workdir_parent: $work
"@ | Set-Content -Encoding ASCII (Join-Path $data 'config.yaml')
if (-not (Test-Path (Join-Path $data '.runner'))) {
  if (-not $TokenFile -or -not (Test-Path $TokenFile)) { throw 'First registration needs -TokenFile (see help).' }
  $token = (Get-Content -Raw $TokenFile).Trim()
  if ($token.StartsWith('{')) { $token = ($token | ConvertFrom-Json).token }
  Push-Location $data
  try {
    & (Join-Path $bin 'act_runner.exe') register --no-interactive --instance $GiteaUrl --token $token `
      --name $RunnerName --labels 'windows:host' -c (Join-Path $data 'config.yaml')
    if ($LASTEXITCODE -ne 0) { throw "act_runner register failed ($LASTEXITCODE)" }
  } finally { Pop-Location; Remove-Item -Force $TokenFile }
}

# --- service ---------------------------------------------------------------------------------------------------
@"
<service>
  <id>$Service</id>
  <name>Factory hand (Gitea act_runner, label windows)</name>
  <description>Software factory Windows hand: runs Gitea Actions jobs with runs-on: windows, as $User. Connects out to $GiteaUrl only.</description>
  <executable>$bin\act_runner.exe</executable>
  <arguments>daemon -c "$data\config.yaml"</arguments>
  <workingdirectory>$data</workingdirectory>
  <logpath>$Root\logs</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>5</keepFiles></log>
  <onfailure action="restart" delay="30 sec"/>
  <resetfailure>1 hour</resetfailure>
  <startmode>Automatic</startmode>
  <delayedAutoStart>true</delayedAutoStart>
  <stoptimeout>30 sec</stoptimeout>
</service>
"@ | Set-Content -Encoding UTF8 (Join-Path $bin 'factory-hand-service.xml')
if (-not (Get-Service $Service -ErrorAction SilentlyContinue)) {
  & (Join-Path $bin 'factory-hand-service.exe') install
  if ($LASTEXITCODE -ne 0) { throw "service install failed ($LASTEXITCODE)" }
}
# Account and password through WMI (not on a command line); the password is not stored anywhere else.
$r = Invoke-CimMethod -ClassName Win32_Service -Filter "Name='$Service'" -MethodName Change `
       -Arguments @{ StartName = ".\$User"; StartPassword = $password }
if ($r.ReturnValue -ne 0) { throw "setting the service account failed (Win32_Service.Change $($r.ReturnValue))" }
$password = $null; $secure = $null
Start-Service $Service
Start-Sleep -Seconds 5
Get-Service $Service | Format-Table Name, Status, StartType -AutoSize
Get-CimInstance Win32_Service -Filter "Name='$Service'" | Format-List Name, StartName, State, PathName
Write-Host "Windows hand installed. Check the runner '$RunnerName' is online in Gitea (organisation > Settings > Actions > Runners)."
Stop-Transcript | Out-Null
