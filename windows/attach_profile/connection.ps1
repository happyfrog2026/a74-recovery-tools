$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

function Get-CameraDataConnection($devices) {
    if (!$devices.Count) { return @{mode='unknown'} }
    $fastboot = @($devices | Where-Object InstanceId -match '^USB\\VID_18D1&PID_4EE0(?:&[^\\]+)?\\')
    $sony = @($devices | Where-Object InstanceId -match '^USB\\VID_054C&PID_0DA5\\')
    if ($fastboot.Count) {
        if ($fastboot.Count -ne 1 -or $sony.Count -or
            $fastboot[0].InstanceId -ne 'USB\VID_18D1&PID_4EE0\ILCE7M4-fastboot') { return @{mode='unknown'} }
        $id = $fastboot[0].InstanceId
        $problem = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_ProblemCode').Data
        if ($problem -eq 28) { return @{mode='driver'; installable=$true} }
        $service = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_Service').Data
        if ($problem -eq 0 -and $service -eq 'WinUSB') { return @{mode='fastboot'} }
        return @{mode='driver'}
    }
    if ($sony.Count -ne 1) {
        if ($sony.Count -or @($devices | Where-Object InstanceId -match '^USB\\VID_054C&').Count) {
            return @{mode='unknown'}
        }
        return @{mode='absent'}
    }
    foreach ($disk in (Get-CimInstance Win32_DiskDrive | Where-Object PNPDeviceID -like 'USBSTOR\*')) {
        $id = $disk.PNPDeviceID
        for ($hop = 0; $hop -lt 8 -and $id; $hop++) {
            if ($id -eq $sony[0].InstanceId) {
                $partitions = Get-CimAssociatedInstance -InputObject $disk -Association Win32_DiskDriveToDiskPartition
                foreach ($partition in $partitions) {
                    $volumes = Get-CimAssociatedInstance -InputObject $partition -Association Win32_LogicalDiskToPartition
                    foreach ($volume in $volumes) {
                        if ($volume.DeviceID -match '^[A-Z]:$' -and $volume.VolumeSerialNumber -and
                            $volume.VolumeName -ne 'PMHOME') {
                            return @{mode='msc'}
                        }
                    }
                }
                break
            }
            $id = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_Parent').Data
        }
    }
    return @{mode='sony_usb'}
}

function Get-AdapterConnection($devices) {
    if (!$devices.Count) { return 'unknown' }
    $roots = @{}
    $ports = @{}
    foreach ($device in $devices) {
        if ($device.InstanceId -match '^USB\\VID_2E8A&PID_[0-9A-F]{4}\\') {
            $roots[$device.InstanceId] = $device
        }
    }
    foreach ($device in $devices) {
        if ($device.Class -ne 'Ports' -or
            $device.InstanceId -notmatch '^(USB\\VID_2E8A&PID_[0-9A-F]{4})(?:&MI_[0-9A-F]{2})?\\') { continue }
        $hardware = $Matches[1]
        $rootId = $device.InstanceId
        if (!$roots.ContainsKey($rootId)) {
            $rootId = (Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName 'DEVPKEY_Device_Parent').Data
            if (!$rootId -or !$rootId.StartsWith($hardware + '\', [StringComparison]::OrdinalIgnoreCase) -or
                !$roots.ContainsKey($rootId)) { return 'unknown' }
        }
        if (!$ports.ContainsKey($rootId)) { $ports[$rootId] = @() }
        $ports[$rootId] += $device
    }
    $boards = @($roots.Values | Where-Object {
        $ports.ContainsKey($_.InstanceId) -or $_.InstanceId -match '^USB\\VID_2E8A&PID_0003\\'
    })
    if (!$boards.Count) { return 'absent' }
    if ($boards.Count -gt 1) { return 'ambiguous' }
    $root = $boards[0]
    if ($ports.ContainsKey($root.InstanceId)) {
        if (@($ports[$root.InstanceId] | Where-Object Status -eq 'OK').Count) { return 'serial' }
        return 'unknown'
    }
    if ($root.Status -eq 'OK' -and $root.InstanceId -match '^USB\\VID_2E8A&PID_0003\\') {
        $key = 'DEVPKEY_Device_BusReportedDeviceDesc'
        $description = (Get-PnpDeviceProperty -InstanceId $root.InstanceId -KeyName $key).Data
        if ($description -eq 'RP2 Boot') { return 'bootsel' }
    }
    return 'unknown'
}

function Get-CameraConnection {
    $devices = @(Get-PnpDevice -PresentOnly)
    try { $adapter = Get-AdapterConnection $devices }
    catch { $adapter = 'unknown' }
    try { $result = Get-CameraDataConnection $devices }
    catch { $result = @{mode='unknown'} }
    $result.adapter = $adapter
    return $result
}
