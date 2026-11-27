# Local firmware resources / 本地固件资源

The public source contains resource preparation code and hashes. Supply your own official firmware files. Resource preparation runs offline, never executes a firmware binary, and never connects to a camera.

公开源码包含资源准备程序及校验值。请自行提供官方固件文件。资源准备离线运行，不执行固件内的程序，也不连接相机。

## Prepare / 准备

Use Python 3.11 or 3.12 from the project directory.

在项目目录中使用 Python 3.11 或 3.12。

```sh
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1
python -m pip install -r requirements-resources.txt
python tools/prepare_resources.py \
  --firmware /path/to/BODYDATA_a7m4_v6.02.DAT \
  --firmware /path/to/BODYDATA_a7m4_v2.00.DAT \
  --firmware /path/to/BODYDATA_a7m4_v3.00.DAT \
  --check
```

In PowerShell, put the command on one line or use backticks for continuation. Filenames may differ. The tool identifies each input by its complete SHA-256 and byte count, verifies the container CRC and block checksums, and verifies every extracted resource against its expected size and SHA-256.

PowerShell 中请将命令写在同一行，或使用反引号换行。文件名可以不同。程序以完整文件的 SHA-256 和字节数识别版本，验证容器 CRC 和数据块校验，并核对每个提取资源的大小及 SHA-256。

The supported input versions are listed below. Obtain official packages through the [current official support page](https://www.sony.com/electronics/support/e-mount-body-ilce-7-series/ilce-7m4/downloads). That page may offer only the current version; the preparation program does not download historical packages.

支持的输入版本如下。官方获取入口为[当前官方支持页面](https://www.sony.com/electronics/support/e-mount-body-ilce-7-series/ilce-7m4/downloads)。该页面可能只提供当前版本；准备程序不下载历史版本固件。

| Input / 输入 | Bytes / 字节数 | Purpose / 用途 |
| --- | ---: | --- |
| ILCE-7M4 6.02 | 757,480,856 | Stock updater repair components, baseline NOR tools and NOR loader / 原厂更新环境修复组件、NOR 基础工具及 NOR loader |
| ILCE-7M4 2.00 | 896,712,088 | Matched CP updater for the 2.00 boot profile / 2.00 启动环境配套 CP updater |
| ILCE-7M4 3.00 | 849,447,320 | Matched CP updater for the 3.00 boot profile / 3.00 启动环境配套 CP updater |

The full list of SHA-256 values, source member names and resource paths is in [upstream_resources.json](../tools/upstream_resources.json). The upstream firmware decoder adaptation is MIT-licensed; its notice is in [host-toolkit-LICENSE](../third_party/licenses/host-toolkit-LICENSE). PySquashfsImage keeps its upstream LGPL terms.

完整 SHA-256、包内路径和生成资源路径见 [upstream_resources.json](../tools/upstream_resources.json)。固件解码部分改编自上游 MIT 许可源码，许可见 [host-toolkit-LICENSE](../third_party/licenses/host-toolkit-LICENSE)。PySquashfsImage 保留上游 LGPL 许可。

## Generated files / 生成文件

- `recovery_tool/stock_assets/` contains the 6.02 updater initrd and checksum file used to repair compatible modified updater environments.
- `recovery_tool/nor_assets/sony602/` contains the official helper programs, CP updater/configuration and `loader_nor.tar` used by the existing NOR workflow.
- `recovery_tool/nor_assets/<profile>/cp/udtrcp.bin` contains each older environment's matched CP updater.
- Generated `provenance.json` files record package and resource identities. Decoded package intermediates live temporarily under `.local/resource-work/` and are removed after extraction.

上述目录分别保存 6.02 更新环境修复组件、NOR 工具与 loader，以及旧版本环境配套的 CP updater。生成的 `provenance.json` 记录来源包和资源身份。临时解码文件位于 `.local/resource-work/`，提取后自动删除。

These generated resource directories are Git-ignored and remain local. Existing matching resources can be reused. The tool refuses to replace a file with different contents, including a symlink destination outside this project. Move a conflicting generated resource aside before preparing it again.

生成资源目录已加入 Git 忽略规则，保留在本地。已存在且一致的资源可重复使用。程序会拒绝覆盖内容不同的文件，也拒绝通过符号链接写到项目目录以外。重新准备前请先移走冲突的生成资源。

```sh
python tools/prepare_resources.py --check
```

This checks the updater/NOR resource set. Preparing 6.02 alone creates the baseline resources; `--check` also requires the two older matched CP updater variants used by the current full application.

此命令检查 updater/NOR 资源集。只提供 6.02 可生成基础资源；`--check` 还会要求当前完整应用所使用的两个旧版配套 CP updater。

## Separate runtime prerequisites / 单独的运行前置资源

RAM Fastboot entry also requires the RP2040 bridge firmware, RAM payload, Boot ROM and ESRAM inputs used by the pinned upstream implementation. These are separate from the updater/NOR extraction above. A normal `BODYDATA.DAT` does not provide every one of these inputs. Boot ROM and ESRAM are already tracked by that upstream submodule; initialize it with `git submodule update --init --recursive`. Build the bridge firmware and RAM payload locally using the toolchain versions recorded in `UPSTREAMS.json`. Follow the pinned upstream setup described in [UPSTREAMS.json](../UPSTREAMS.json) and the application's setup instructions.

RAM Fastboot 引导还需要固定上游版本使用的 RP2040 配套程序、RAM payload、Boot ROM 和 ESRAM 输入。这些资源与上面的 updater/NOR 提取分开准备，普通 `BODYDATA.DAT` 并不能提供全部输入。Boot ROM 和 ESRAM 已由该上游 submodule 跟踪，执行 `git submodule update --init --recursive` 即可获取。配套程序与 RAM payload 使用 `UPSTREAMS.json` 记录的工具链版本在本地构建。请依据 [UPSTREAMS.json](../UPSTREAMS.json) 中固定的上游版本及应用设置说明准备。

Creating desktop release packages is a local release-engineering process. The public extraction command prepares local runtime inputs; it does not create or publish a DMG or EXE.

桌面发布包构建属于本地发布流程。公开的资源准备命令只生成本地运行输入，不生成或发布 DMG、EXE。
