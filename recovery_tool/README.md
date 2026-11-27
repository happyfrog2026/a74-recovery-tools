# Shared recovery backend

The macOS SwiftUI and Windows Electron apps both use `task_worker.py` for JSONL requests and progress events. `task_backend.py` coordinates board setup, connection checks, SD recovery, backup and snapshot restore.

Backup stores double-read verified segments and resumes the first incomplete pair. Restore creates a fresh task from a verified snapshot, compares current storage with the target, writes differing fragments, and records immediate readback results. Read interruptions restart the current read. Unconfirmed writes are read back before a retry decision. The NOR stage rebuilds matched startup data after eMMC restoration.

See [development](../docs/development.md), [resource preparation](../docs/resources.md), and the [top-level guide](../README.md).

（运行资源由本地准备；第三方组件遵循各自许可。）
