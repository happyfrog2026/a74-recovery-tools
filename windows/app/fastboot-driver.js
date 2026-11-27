const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const path = require('node:path');

const exec = promisify(execFile);
const probeScript = String.raw`
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$devices = @(Get-PnpDevice -PresentOnly | Where-Object {
    $_.InstanceId -match '^USB\\VID_18D1&PID_4EE0(?:&[^\\]+)?\\'
})
$items = @($devices | ForEach-Object {
    $device = $_
    $problem = (Get-PnpDeviceProperty -InstanceId $device.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode').Data
    [pscustomobject]@{ problem = [int]$problem }
})
ConvertTo-Json -InputObject $items -Compress
`;

function classifyFastbootDevices(devices) {
  if (devices.length === 0) return 'absent';
  if (devices.length !== 1) return 'ambiguous';
  return devices[0].problem === 28 ? 'driver_missing' : 'present';
}

async function fastbootDriverStatus() {
  const powershell = path.join(process.env.SystemRoot || 'C:\\Windows',
    'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const { stdout } = await exec(powershell, ['-NoProfile', '-NonInteractive',
    '-Command', probeScript], { windowsHide: true, timeout: 12000 });
  return classifyFastbootDevices(JSON.parse(stdout.trim()));
}

module.exports = { classifyFastbootDevices, fastbootDriverStatus };
