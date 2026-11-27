const { execFile } = require('node:child_process');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { promisify } = require('node:util');

const script = readFileSync(path.join(__dirname, 'connection.ps1'), 'utf8');
const probe = Buffer.from(script +
  '\ntry { Get-CameraConnection | ConvertTo-Json -Depth 4 -Compress } catch { @{mode="unknown"} | ConvertTo-Json -Compress }\n',
  'utf16le').toString('base64');
const states = new Set(['fastboot', 'msc', 'sony_usb', 'driver', 'absent', 'unknown']);
const adapterStates = new Set(['serial', 'bootsel', 'absent', 'ambiguous', 'unknown']);

async function cameraConnection(execute = promisify(execFile)) {
  const powershell = path.win32.join(process.env.SystemRoot || 'C:\\Windows',
    'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  try {
    const {stdout} = await execute(powershell, ['-NoProfile', '-NonInteractive', '-EncodedCommand', probe],
      {windowsHide: true, timeout: 20000, maxBuffer: 65536});
    const result = JSON.parse(stdout);
    if (!states.has(result.mode)) return {mode: 'unknown'};
    const base = {mode: result.mode};
    if (adapterStates.has(result.adapter)) base.adapter = result.adapter;
    if (result.mode === 'driver' && result.installable === true) base.installable = true;
    return base;
  } catch {
    return {mode: 'unknown'};
  }
}

module.exports = {cameraConnection};
