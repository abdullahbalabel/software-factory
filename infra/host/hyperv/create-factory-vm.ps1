# Creates the factory-core Hyper-V VM on a Windows PC. Expects the Ubuntu 24.04 cloud
# image converted to D:\vm\factory-core\factory-core.vhdx and a cloud-init seed.iso (user factory, SSH key only).
# Then follow README "Move the factory to another server" with factory@<vm> instead of root@<server>.
$ErrorActionPreference = 'Stop'
$n = 'factory-core'
New-VM -Name $n -Generation 2 -MemoryStartupBytes 6GB -VHDPath 'D:\vm\factory-core\factory-core.vhdx' -Path 'D:\vm' -SwitchName 'Default Switch' | Out-Null
Set-VM -Name $n -ProcessorCount 8 -DynamicMemory -MemoryMinimumBytes 4GB -MemoryStartupBytes 6GB -MemoryMaximumBytes 12GB -AutomaticStartAction Start -AutomaticStartDelay 60 -AutomaticStopAction ShutDown -CheckpointType Disabled
Set-VMFirmware -VMName $n -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority
Add-VMDvdDrive -VMName $n -Path 'D:\vm\factory-core\seed.iso'
Set-VMFirmware -VMName $n -FirstBootDevice (Get-VMHardDiskDrive -VMName $n)
Set-VMNetworkAdapter -VMName $n -StaticMacAddress 00155D880A0A
Set-VMProcessor -VMName $n -ExposeVirtualizationExtensions $true
Enable-VMIntegrationService -VMName $n -Name 'Guest Service Interface'
Get-VM $n | Format-List Name,Path,Generation,ProcessorCount,DynamicMemoryEnabled,MemoryStartup,MemoryMinimum,MemoryMaximum,AutomaticStartAction,AutomaticStopAction
Get-VMFirmware $n | Format-List SecureBoot,SecureBootTemplate
