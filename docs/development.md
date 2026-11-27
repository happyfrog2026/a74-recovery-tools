# Development / 开发

The release baseline is 0.1.52. macOS uses SwiftUI; Windows uses Electron; both communicate with the Python backend through UTF-8 JSONL.

当前基线为 0.1.52。Mac 使用 SwiftUI，Windows 使用 Electron，两端通过 UTF-8 JSONL 调用 Python 后端。

## Dependencies / 依赖

Use Python 3.11 or 3.12. Initialize the fixed upstream source, create a virtual environment and install the runtime requirements.

使用 Python 3.11 或 3.12，初始化固定上游源码，再准备虚拟环境和运行依赖。

```sh
git submodule update --init --recursive
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-runtime.txt
```

On Windows, use `.venv\Scripts\python.exe` for the Python commands. USB operations use libusb on macOS and the matching WinUSB interface on Windows. Resource preparation is documented in [resources.md](resources.md).

Windows 上将上述 Python 路径替换为 `.venv\Scripts\python.exe`。Mac USB 通信使用 libusb，Windows 使用匹配的 WinUSB 接口。运行资源准备见 [resources.md](resources.md)。

## Offline checks / 离线检查

Run from the project root. Install resource-preparation dependencies as well to run the synthetic firmware-decoder tests.

在项目根目录运行。完整测试还需安装资源准备依赖，以运行合成固件解码测试。

```sh
.venv/bin/python -m pip install -r requirements-resources.txt
PYTHONPATH=tests:. .venv/bin/python -m unittest discover -s tests
.venv/bin/python -m unittest discover -s windows/tests -p 'test_worker_utf8.py'
node windows/tests/test_driver_maintenance.js
```

For PowerShell, set `$env:PYTHONPATH="tests;."` and run the same unittest command using the Windows interpreter path.

PowerShell 中先设置 `$env:PYTHONPATH="tests;."`，再使用 Windows 解释器路径运行相同 unittest 命令。

## macOS source frontend / Mac 源码界面

Install Xcode Command Line Tools and libusb. From the project root:

安装 Xcode Command Line Tools 和 libusb，然后在项目根目录运行：

```sh
mkdir -p .local
swiftc -parse-as-library -swift-version 5 -framework SwiftUI -framework AppKit   native/CameraRecoveryApp.swift -o .local/CameraRecoveryDev
CAMERA_RECOVERY_PYTHON="$PWD/.venv/bin/python" CAMERA_RECOVERY_SOURCE_ROOT="$PWD" .local/CameraRecoveryDev
```

The development frontend loads the local Python worker. Prepare the resource inputs before running camera operations.

开发界面调用本地 Python 后端，使用相机功能前请完成资源准备。

## Windows source frontend / Windows 源码界面

Install Node.js 20 or later. From the project root in PowerShell:

安装 Node.js 20 或更新版本，在项目根目录的 PowerShell 中运行：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
$env:CAMERA_RECOVERY_PYTHON = (Resolve-Path .venv\Scripts\python.exe).Path
cd windows
npm ci
npm start
```

The Electron development process launches `windows/worker_entry.py` with the selected interpreter. Its entry point configures UTF-8 streams before reading requests. The regular desktop release includes its own runtime.

Electron 开发进程使用指定的解释器启动 `windows/worker_entry.py`，在读取请求前固定 UTF-8 输入输出。桌面发布版本自带运行环境。

## Architecture / 结构

- `task_worker.py` handles one request and streams progress and final results.
- `task_backend.py` coordinates operation phases and selected model.
- `snapshot.py` and `resumable_region_read.py` manage verified backup reads.
- `scoped_restore_plan.py` and `write_transaction.py` plan differing ranges, persist write progress and reconcile interrupted writes.
- `stock_*.py` prepare the updater and SD recovery trigger.
- `nor_rebuild_flow.py` coordinates startup-data rebuilding.

（桌面安装包打包脚本保留在本地。离线测试使用合成数据和模拟后端，真机验收单独进行。）

(Desktop installer packaging is maintained locally. Offline checks use synthetic inputs and simulated backends; physical camera acceptance is performed separately.)
