$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$target = 'USB\VID_18D1&PID_4EE0\ILCE7M4-fastboot'
$hardware = 'USB\VID_18D1&PID_4EE0'

function Test-DedicatedInf([string]$content) {
    $sections = @{}
    $name = ''
    foreach ($line in ($content -split '\r?\n')) {
        $line = $line.Trim()
        if (!$line -or $line.StartsWith(';')) { continue }
        if ($line -match '^\[([^\]]+)\]$') {
            $name = $Matches[1]
            if ($sections.ContainsKey($name)) { return $false }
            $sections[$name] = @()
        } else {
            if (!$name -or $line.EndsWith('\')) { return $false }
            $sections[$name] += $line
        }
    }
    $checks = @{
        Version = @('^Provider\s*=\s*"libwdi"$', '^Class\s*=\s*"USBDevice"$',
                    '^ClassGuid\s*=\s*\{88bae032-5a81-49f0-bc3d-a4ff138216d6\}$')
        Strings = @('^DeviceID\s*=\s*"VID_18D1&PID_4EE0"$')
        USB_Install = @('^Include\s*=\s*winusb\.inf$', '^Needs\s*=\s*WINUSB\.NT$')
        'USB_Install.Services' = @('^AddService\s*=\s*WinUSB,0x00000002,WinUSB_ServiceInstall$')
        WinUSB_ServiceInstall = @('^ServiceBinary\s*=\s*%12%\\WinUSB\.sys$')
    }
    foreach ($section in $checks.Keys) {
        if (!$sections.ContainsKey($section)) { return $false }
        foreach ($pattern in $checks[$section]) {
            if (@($sections[$section] -match $pattern).Count -ne 1) { return $false }
            $key = ($pattern -split '\\s', 2)[0]
            if (@($sections[$section] -match ($key + '\s*=')).Count -ne 1) { return $false }
        }
    }
    if (@($sections.Manufacturer).Count -ne 1 -or
        $sections.Manufacturer[0] -notmatch '^%VendorName%\s*=\s*libusbDevice_WinUSB,NTx86,NTamd64,NTarm64$') {
        return $false
    }
    foreach ($arch in @('NTx86', 'NTamd64', 'NTarm64')) {
        $model = @($sections["libusbDevice_WinUSB.$arch"])
        if ($model.Count -ne 1 -or $model[0] -notmatch '^%DeviceName%\s*=\s*USB_Install\s*,\s*USB\\%DeviceID%$') {
            return $false
        }
    }
    return $true
}

function Get-InfUsers([string]$inf) {
    # Enum retains Driver links for disconnected devices. Resolve every link
    # against the class registry so stale/offline users are included.
    $base = 'SYSTEM\CurrentControlSet'
    $enum = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("$base\Enum")
    $class = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey("$base\Control\Class")
    if (!$enum -or !$class) { throw 'Cannot read the PnP binding registry.' }
    $bindings = @{}
    try {
        foreach ($bus in $enum.GetSubKeyNames()) {
            $busKey = $enum.OpenSubKey($bus)
            try {
                foreach ($model in $busKey.GetSubKeyNames()) {
                    $modelKey = $busKey.OpenSubKey($model)
                    try {
                        foreach ($instance in $modelKey.GetSubKeyNames()) {
                            $device = $modelKey.OpenSubKey($instance)
                            try { $link = $device.GetValue('Driver') }
                            finally { if ($device) { $device.Dispose() } }
                            if (!$link) { continue }
                            if ($link -notmatch '^\{[a-f0-9-]{36}\}\\\d+$') { throw 'Invalid PnP driver link.' }
                            if (!$bindings.ContainsKey($link)) {
                                $driver = $class.OpenSubKey($link)
                                # Unrelated stale links whose class entry was removed
                                # cannot resolve to any installed driver package.
                                $bindings[$link] = $null
                                if ($driver) {
                                    try { $bindings[$link] = $driver.GetValue('InfPath') }
                                    finally { $driver.Dispose() }
                                }
                            }
                            if ($bindings[$link] -eq $inf) { "$bus\$model\$instance" }
                        }
                    } finally { if ($modelKey) { $modelKey.Dispose() } }
                }
            } finally { if ($busKey) { $busKey.Dispose() } }
        }
    } finally {
        $enum.Dispose()
        $class.Dispose()
    }
}

function Read-OemInf([string]$inf) {
    $file = Join-Path ([Environment]::GetFolderPath('Windows')) "INF\$inf"
    return ,([IO.File]::ReadAllBytes($file))
}

function Inspect-Target {
    $devices = @(Get-PnpDevice -InstanceId $target)
    if ($devices.Count -ne 1 -or $devices[0].InstanceId -ne $target) { return @{code='target'} }
    $keys = @('Service', 'DriverInfPath', 'DriverProvider', 'MatchingDeviceId', 'IsPresent')
    $props = @{}
    Get-PnpDeviceProperty -InstanceId $target -KeyName @($keys | ForEach-Object { "DEVPKEY_Device_$_" }) |
        ForEach-Object { $props[$_.KeyName] = $_.Data }
    if ($props['DEVPKEY_Device_IsPresent'] -isnot [bool]) { return @{code='query'} }
    if ($props['DEVPKEY_Device_IsPresent']) { return @{code='present'} }
    $inf = $props['DEVPKEY_Device_DriverInfPath']
    if ($inf -notmatch '^oem[0-9]+\.inf$' -or $props['DEVPKEY_Device_Service'] -ne 'WinUSB' -or
        $props['DEVPKEY_Device_DriverProvider'] -ne 'libwdi' -or
        $props['DEVPKEY_Device_MatchingDeviceId'] -ne $hardware) { return @{code='package'} }
    $bytes = Read-OemInf $inf
    $reader = [IO.StreamReader]::new([IO.MemoryStream]::new($bytes))
    try { $content = $reader.ReadToEnd() }
    finally { $reader.Dispose() }
    if (!(Test-DedicatedInf $content)) { return @{code='package'} }
    $users = @(Get-InfUsers $inf)
    if ($users.Count -ne 1 -or $users[0] -ne $target) { return @{code='shared'} }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = [BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '') }
    finally { $sha.Dispose() }
    return @{code='eligible'; inf=$inf; digest=$digest}
}

function Invoke-DriverRemoval([string]$inf) {
    $tool = Join-Path ([Environment]::SystemDirectory) 'pnputil.exe'
    & $tool /delete-driver $inf /uninstall | Out-Null
    return $LASTEXITCODE
}

function Remove-CheckedPackage([string]$inf, [string]$digest) {
    $current = Inspect-Target
    if ($current.code -ne 'eligible' -or $current.inf -ne $inf -or $current.digest -ne $digest) {
        return 21
    }
    if ((Invoke-DriverRemoval $inf) -ne 0) { return 22 }
    return 0
}
