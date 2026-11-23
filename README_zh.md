# A74 相机恢复工具

[English](README.md) | 简体中文

使用 Camera Recovery 桌面 App 备份和恢复 Sony α7 IV（A7M4 / ILCE-7M4）。

## 下载

请到 [GitHub Releases](https://github.com/happyfrog2026/a74-recovery-tools/releases) 获取 Windows（x64）安装包或 macOS（Apple 芯片）DMG。

## 使用前准备

- 一台电量充足的 Sony α7 IV、基于 RP2040 的 Multi 转接头，以及两条支持数据传输的 USB 线。
- **两种 Multi 接线方式任选其一。** [自制 Multi 工程线](https://github.com/DavidBuchanan314/ILCE-7M4-RE/blob/main/images/multi_breakout.jpg)适合有焊接经验的用户。一般用户可在拼多多或闲鱼搜索 **Multi 15-Pin Breakout (M15PB1)**（蓝色土豆），选用现成的工程板，接线更方便，信号也更稳定。
- 预留足够的磁盘空间保存整份相机备份。从 SD 卡恢复原厂固件时，另需准备存储卡及与机型匹配的索尼官方固件包。

## 使用步骤

1. 打开「准备 Multi 转接头」，将 **USB 1（RP2040）** 接到电脑，按 App 提示完成安装。
2. 相机关机时接好 Multi 插头，并将两条 USB 线接到电脑。打开相机后，立刻点击「检查连接」。Windows 如提示缺少 WinUSB 驱动，按页面提示安装。
3. 选择要执行的操作。

| 操作 | 怎么做 |
| --- | --- |
| **备份相机** | 选择保存位置并开始备份。等校验完成后，保留整个备份文件夹。 |
| **恢复已保存状态** | 选择已验证的 `snapshot` 文件夹，或它上一级的备份任务文件夹。确认后等待最终结果。 |
| **从 SD 卡恢复原厂** | 选择相机型号，使用匹配的官方固件包，按 App 提示准备 SD 卡并执行恢复。以相机显示完成为准。 |

备份和恢复期间，请保持相机供电及 USB 连接稳定。

```mermaid
flowchart TD
    A["准备 Multi 转接头"] --> B["连接相机并检查连接"]
    B --> C["备份相机"]
    B --> D["从已验证备份恢复"]
    B --> E["从 SD 卡恢复原厂"]
    C --> F["查看最终结果"]
    D --> F
    E --> F
```

## 许可

本软件免费提供，仅供非商业用途，采用 [PolyForm Noncommercial License 1.0.0](LICENSE)。第三方组件保留各自原有的许可证及声明。
