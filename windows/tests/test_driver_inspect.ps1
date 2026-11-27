# Dot-source driver-inspect.ps1 first. Every device query and mutation below is mocked.
$fixture = @'
[Version]
Provider    = "libwdi"
Class       = "USBDevice"
ClassGuid   = {88bae032-5a81-49f0-bc3d-a4ff138216d6}
[Strings]
DeviceID   = "VID_18D1&PID_4EE0"
[Manufacturer]
%VendorName% = libusbDevice_WinUSB,NTx86,NTamd64,NTarm64
[libusbDevice_WinUSB.NTx86]
%DeviceName% = USB_Install, USB\%DeviceID%
[libusbDevice_WinUSB.NTamd64]
%DeviceName% = USB_Install, USB\%DeviceID%
[libusbDevice_WinUSB.NTarm64]
%DeviceName% = USB_Install, USB\%DeviceID%
[USB_Install]
Include = winusb.inf
Needs   = WINUSB.NT
[USB_Install.Services]
AddService = WinUSB,0x00000002,WinUSB_ServiceInstall
[WinUSB_ServiceInstall]
ServiceBinary = %12%\WinUSB.sys
'@
function Get-PnpDevice { return @{InstanceId=$script:instance} }
function Get-PnpDeviceProperty {
    foreach ($key in $script:values.Keys) { @{KeyName="DEVPKEY_Device_$key"; Data=$script:values[$key]} }
}
function Read-OemInf { return ,([Text.Encoding]::UTF8.GetBytes($script:content)) }
function Get-InfUsers {
    if ($script:queryFails) { throw 'Mock registry access denied' }
    return $script:users
}
function Invoke-DriverRemoval([string]$inf) {
    $script:calls += $inf
    return $script:exitCode
}
function Assert-Equal($actual, $expected) {
    if ($actual -ne $expected) { throw "Expected $expected; got $actual" }
}
$instance = $target
$values = @{Service='WinUSB'; DriverInfPath='oem123.inf'; DriverProvider='libwdi'; MatchingDeviceId=$hardware; IsPresent=$false}
$users = @($target)
$content = $fixture
$queryFails = $false
$exitCode = 0
$calls = @()
$allowed = Inspect-Target
Assert-Equal $allowed.code 'eligible'
Assert-Equal $allowed.inf 'oem123.inf'
$values.DriverInfPath = 'winusb.inf'
Assert-Equal (Inspect-Target).code 'package'
$values.DriverInfPath = $allowed.inf
$values.Service = 'usbser'
Assert-Equal (Inspect-Target).code 'package'
$values.Service = 'WinUSB'
$instance = 'USB\VID_18D1&PID_4EE0\different-camera'
Assert-Equal (Inspect-Target).code 'target'
$instance = $target
$values.IsPresent = $true
Assert-Equal (Inspect-Target).code 'present'
$values.IsPresent = $false
$content = $fixture.Replace('USB\%DeviceID%', 'USB\%DeviceID%, USB\VID_1234&PID_5678')
Assert-Equal (Inspect-Target).code 'package'
$content = $fixture -replace '(?m)^Provider.*(?:\r?\n|$)', ''
Assert-Equal (Inspect-Target).code 'package'
$content = $fixture
$users = @($target, 'USB\VID_18D1&PID_4EE0\another-disconnected-instance')
Assert-Equal (Inspect-Target).code 'shared'
Assert-Equal (Remove-CheckedPackage $allowed.inf $allowed.digest) 21
$users = @($target)
$queryFails = $true
try { Inspect-Target | Out-Null; throw 'Missing expected registry error' }
catch { Assert-Equal $_.Exception.Message 'Mock registry access denied' }
$queryFails = $false
$content = $fixture + [Environment]::NewLine + '; changed after confirmation'
Assert-Equal (Remove-CheckedPackage $allowed.inf $allowed.digest) 21
Assert-Equal $calls.Count 0
$content = $fixture
$exitCode = 5
Assert-Equal (Remove-CheckedPackage $allowed.inf $allowed.digest) 22
$exitCode = 0
Assert-Equal (Remove-CheckedPackage $allowed.inf $allowed.digest) 0
Assert-Equal ($calls -join ',') 'oem123.inf,oem123.inf'
'FASTBOOT_DRIVER_WINDOWS_MOCK_OK'
