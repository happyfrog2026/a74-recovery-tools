import AppKit
import CryptoKit
import Foundation
import SwiftUI

private enum AppLanguage {
    static let preference = "CameraRecoveryLanguage"
    static let chinese = "zh-Hans"
    static let english = "en"
}

private func t(_ text: String) -> String {
    translatedText(text, language: UserDefaults.standard.string(forKey: AppLanguage.preference) ?? "")
}

private func translatedText(_ text: String, language: String) -> String {
    guard language == AppLanguage.english else { return text }
    if text.hasPrefix("恢复流程已完成，"), text.hasSuffix("处写入校验未通过"),
       let count = Int(text.dropFirst("恢复流程已完成，".count).dropLast("处写入校验未通过".count)) {
        return "Recovery flow completed; write verification failed at \(count) location\(count == 1 ? "" : "s")"
    }
    if text == "没有可写的恢复片段；请查看跳过区域" {
        return "No writable restore fragments; review skipped regions"
    }
    if let exact = englishText[text] { return exact }
    var translated = text
    // Longest phrases first lets file paths, counters and model names remain verbatim.
    for phrase in englishText.keys.sorted(by: { $0.count > $1.count }) where translated.contains(phrase) {
        translated = translated.replacingOccurrences(of: phrase, with: englishText[phrase]!)
    }
    return translated
}

private func needsLanguageSelection(_ language: String) -> Bool {
    language != AppLanguage.chinese && language != AppLanguage.english
}

private func restoreSkippedText(_ result: [String: Any]) -> String? {
    guard ["VERIFIED", "PARTIAL"].contains(result["status"] as? String ?? ""),
          result["success_with_skips"] as? Bool == true else { return nil }
    let skipped = result["skipped_regions"] as? [[String: Any]] ?? []
    let names = skipped.compactMap { $0["name"] as? String }.joined(separator: ", ")
    guard !names.isEmpty else {
        return "跳过区域：详情缺失。请查看日志。"
    }
    return "跳过区域：\(names)"
}

private let englishText: [String: String] = [
    "存储恢复已验证，正在准备重建启动数据…": "Storage restore verified; preparing to rebuild startup data…",
    "正在准备相机启动数据重建…": "Preparing camera startup data rebuild…",
    "正在重建并校验启动数据，请保持供电和连接…": "Rebuilding and verifying startup data; keep power and cables connected…",
    "正在核对启动数据重建结果…": "Checking startup data rebuild results…",
    "正在清理临时恢复入口…": "Cleaning temporary recovery entry…",
    "正在返回普通相机模式…": "Returning to normal camera mode…",
    "启动数据重建预计约 3 分钟；请保持供电和连接。": "Startup data rebuild is estimated at about 3 minutes; keep power and cables connected.",
    "启动数据阶段已进行 ": "Startup data stage elapsed: ",
    "重建启动数据": "Rebuilding startup data",
    "存储恢复与启动数据重建已验证，相机已返回普通模式。请检查取景与菜单是否正常。": "Storage restore and startup data rebuild verified; the camera returned to normal mode. Check that live view and menus work.",
    "可恢复区域已验证；有区域未恢复": "Restorable regions verified; some regions were not restored",
    "本次未执行启动数据重建（NOR）；当前配置尚不支持。": "Startup data rebuild (NOR) was not performed; this configuration is not yet supported.",
    "相机型号": "Camera model",
    "A7R V 引导恢复": "A7R V guided recovery",
    "无法进入菜单时，A7R V 可使用 Multi 转接头启动恢复。": "If the A7R V cannot open its menu, use the Multi adapter to start recovery.",
    "我确认相机是 A7R V（ILCE-7RM5）": "I confirm this is an A7R V (ILCE-7RM5)",
    "A7 IV 引导恢复": "A7 IV guided recovery",
    "机内 SD 更新引导": "In-camera SD update guide",
    "相机能进入菜单时，请在机内启动官方更新。": "Start the official update on the camera if it can open its menu.",
    "无法进入菜单时，A7 IV 可使用 Multi 转接头启动恢复。": "If the A7 IV cannot open its menu, use the Multi adapter to start recovery.",
    "此机型的转接头恢复尚不支持。": "Adapter recovery is not yet supported for this model.",
    "请安全推出卡片。连接原装 AC 电源，将存有 BODYDATA.DAT 的 SDXC 或 CFexpress Type A 卡插入卡槽 B。打开 Maintenance（维护）→ Version（版本）→ Version Up（版本更新），核对版本后按屏幕提示执行。": "Eject the card safely. Connect the supplied AC adapter and insert the SDXC or CFexpress Type A card containing BODYDATA.DAT into slot B. Open Maintenance → Version → Version Up, check the target version, and follow the on-screen prompts.",
    "固件已写卡并复核，请按下方机内步骤操作。": "The firmware was copied and verified. Follow the in-camera steps below.",
    "卡上已有相同的固件，请按下方机内步骤操作。": "The same firmware is already on the card. Follow the in-camera steps below.",
    "查看 Sony 针对当前版本的升级要求": "See Sony’s update requirements for your current version",
    "准备存储卡，在相机菜单更新": "Prepare a card for an in-camera update",
    "已准备好存储卡，查看机内更新步骤": "My card is ready. Show in-camera update steps",
    "在相机上更新": "Update on the camera",
    "将卡插入卡槽 1，打开菜单 → 设置 → 设置选项 → 版本。如果有“机身”，先选“机身”，再选“软件更新”。请按相机提示核对目标版本并完成更新。": "Insert the card in slot 1. Open Menu → Setup → Setup Option → Version. Select Body if shown, then Software Update. Check the target version on the camera and follow its instructions.",
    "固件包适用于其他机型，请返回选择与相机匹配的 DAT。": "This package is for another camera model. Go back and select the DAT for your camera.",
    "此 DAT 的机型与版本尚未识别，请按 Sony 下载页核对。": "This DAT is not in the package catalog. Check its model and version on the Sony download page.",
    // Navigation, shared controls, and system dialogs.
    "准备 Multi 转接头": "Prepare the Multi adapter",
    "准备相机": "Prepare camera",
    "建议将相机设为连接电脑时自动进入 MSC。": "Set the camera to open in MSC automatically when connected to your computer.",
    "MENU → 设置 → USB → USB连接模式 → 海量存储器（MSC）": "MENU → Setup → USB → USB Connection Mode → MassStorage (MSC)",
    "我已准备好": "I have prepared",
    "检查连接": "Check connections",
    "从 SD 卡恢复原厂": "Restore factory firmware from SD card",
    "备份相机": "Back up camera",
    "恢复已保存状态": "Restore saved state",
    "从 SD 卡恢复原厂系统": "Restore factory firmware from SD card",
    "ILCE-7M4 恢复工具": "ILCE-7M4 Recovery Tool",
    "使用前准备": "Before you begin",
    "连接示意图": "Connection diagram",
    "导出日志": "Export logs",
    "运行日志": "Activity log",
    "跟随最新日志": "Follow latest log",
    "正在计算本次计划片段数…": "Calculating fragments for this restore…",
    "已验证片段": "Verified fragments",
    "未通过": "Failed",
    "恢复计划已计算，正在准备写入…": "Restore plan calculated; preparing to write…",
    "正在写入并校验片段…": "Writing and verifying fragments…",
    "正在核验本机已保存的片段读回证据…": "Checking saved local fragment readback evidence…",
    "正在重新检查恢复连接…": "Rechecking the recovery connection…",
    "正在核对备份的启动数据配置…": "Checking the startup data profile of the backup…",
    "正在校验本机备份…": "Checking the local backup…",
    "正在校验本机恢复文件…": "Checking local recovery files…",
    "本机备份校验完成，正在检查相机连接…": "Local backup checked; checking the camera connection…",
    "正在结束相机读取阶段，随后校验本机备份…": "Finishing the camera read phase, then checking the local backup…",
    "相机读取阶段已结束，正在校验本机备份…": "Camera read phase complete; checking the local backup…",
    "本机备份已核验，正在建立相机恢复连接…": "Local backup verified; establishing the camera recovery connection…",
    "正在确认相机恢复连接仍可读取…": "Checking that the camera recovery connection still responds…",
    "本机校验期间，正在安全地重新进入恢复模式…": "Safely reentering recovery mode during local checks…",
    "保存当前状态期间，正在安全地重新进入恢复模式…": "Safely reentering recovery mode while saving the current state…",
    "将先核对本机备份，再连接相机并恢复。": "The app checks the local backup before connecting to the camera and restoring.",
    "正在通过 Multi 转接头进入临时恢复模式…": "Entering temporary recovery mode through the Multi adapter…",
    "临时恢复程序已发送，正在等待相机就绪（最多 25 秒）…": "Temporary recovery program sent; waiting up to 25 seconds for camera readiness…",
    "相机未报告就绪，正在检查 USB 2 是否稍后出现…": "Camera readiness was not reported; checking whether USB 2 appears shortly…",
    "相机已报告就绪，正在等待 USB 2…": "Camera reported ready; waiting for USB 2…",
    "本次进入恢复模式未就绪，诊断已记录。": "This recovery-mode entry did not become ready; diagnostics were recorded.",
    "临时恢复模式已就绪，正在核对相机…": "Temporary recovery mode is ready; checking the camera…",
    "相机未报告就绪，正在第 ": "Camera did not report ready; retrying recovery mode entry ",
    " 次重新进入恢复模式…": "…",
    "Multi 转接头本次 PING 已验证。相机恢复 DATA 未在此步骤验证；恢复任务会单独检查。": "The Multi adapter passed PING in this check. Camera recovery DATA was not verified by this step; the recovery task checks it separately.",
    "Multi 转接头处于 BOOTSEL，尚未验证运行态。请先到“准备 Multi 转接头”完成安装。": "The Multi adapter is in BOOTSEL; runtime is unverified. Complete installation under ‘Prepare the Multi adapter’ first.",
    "任务未完成": "Task incomplete",
    "好": "OK",
    "取消": "Cancel",
    "返回": "Back",
    "确认安装": "Confirm installation",
    "安装配套程序": "Install adapter firmware",
    "将配套程序安装到当前 BOOTSEL 中的 RP2040。安装后转接头会重新连接。": "Install the adapter firmware on the RP2040 currently in BOOTSEL mode. The adapter will reconnect afterward.",
    "确认恢复相机": "Confirm camera recovery",
    "我已了解风险，开始恢复": "I understand the risks; start recovery",
    "将使用卡槽 1 存储卡中的固件包恢复相机。此操作可能导致数据丢失、相机无法启动，并可能影响原厂保修。请保持供电和连接。": "Recovery will use the firmware package on the card in slot 1. This may cause data loss, prevent the camera from starting, and affect the manufacturer's warranty. Keep power and cables connected.",
    "替换存储卡上的固件包？": "Replace the firmware file on the card?",
    "替换原文件": "Replace existing file",
    "所选存储卡 ": "Selected card ",
    " 根目录已有不同的 BODYDATA.DAT。确认后，原文件将被所选 DAT 替换，并核对复制完整性。请确认这是用于本次更新的存储卡。": " already has a different BODYDATA.DAT at its root. Confirming replaces it with the selected DAT and verifies the copy. Make sure this is the card you want to update from.",
    "确认上次更新已失败或停止": "Confirm the previous update failed or stopped",
    "确认观察到 5 长 6 短闪灯": "Confirm 5 long and 6 short flashes",
    "我确认未插卡且更新已失败": "I confirm no card was inserted and the update failed",
    "仅当相机持续出现 5 长 6 短闪灯，且未显示“更新完成”时确认。此操作只记录你看到的错误，不判断具体原因，也不会重启或继续备份。": "Confirm only if the camera continues to show 5 long and 6 short flashes and did not show ‘Update complete’. This records your observation; it does not determine the cause, restart the camera, or resume backup.",
    "仅当上次更新时没有插入存储卡，且相机没有显示“更新完成”时选择。此操作只记录你的观察；不会自动重启相机。": "Choose this only if no card was inserted during the previous update and the camera did not show ‘Update complete’. This records your observation and will not restart the camera.",
    "选择 DAT 文件": "Choose DAT file",
    "选择这张存储卡": "Choose this card",
    "只将 USB 1 RP2040 接到电脑，Multi 和 USB 2 暂不连接": "Connect only USB 1 (RP2040) to the computer. Leave Multi and USB 2 disconnected.",
    "Multi 接相机，USB 1 和 USB 2 接电脑": "Connect Multi to the camera and USB 1 and USB 2 to the computer.",
    "连接示意图暂不可用，请按照下方文字连接。": "The connection diagram is unavailable. Follow the written instructions below.",
    // Board and connection screens.
    "待检查": "Not checked",
    "正在检查": "Checking",
    "未安装": "Not installed",
    "安装中": "Installing",
    "已安装": "Installed",
    "安装状态不明": "Installation status unknown",
    "只把 USB 1（RP2040）接到电脑；USB 2 和 Multi 插头暂不连接。": "Connect only USB 1 (RP2040) to the computer; leave USB 2 and Multi disconnected.",
    "转接头已通过运行态应答验证。": "The adapter responded to its runtime check.",
    "已检测到 BOOTSEL。点击“安装配套程序”并确认。": "BOOTSEL detected. Click ‘Install adapter firmware’ and confirm.",
    "已检测到 RP2040，但配套程序尚未安装。": "RP2040 detected, but its firmware is not installed.",
    "检测到多个候选转接头。请只连接本次要准备的 RP2040。": "Multiple adapters detected. Connect only the RP2040 you want to prepare.",
    "未确认转接头状态。请连接 USB 1 后重新检查。": "Adapter status is unconfirmed. Connect USB 1 and check again.",
    "操作未完成，请查看错误后重新检查转接头。": "The operation did not finish. Review the error and check the adapter again.",
    "准备未完成，请查看提示后重新检查。": "Preparation did not finish. Review the message and check again.",
    "请断开 RP2040（USB 1）与电脑的连接，按住 RP2040 上的 BOOT 键，再将 USB 1 接回电脑。接好后松开 BOOT 键，然后点击“检查转接头”。": "Disconnect the RP2040 (USB 1) from the computer. Hold the BOOT button on the RP2040, reconnect USB 1 to the computer, then release BOOT and click “Check adapter”.",
    "检查转接头": "Check adapter",
    "尚未检查": "Not checked",
    "未验证": "Not verified",
    "本次 PING 已验证": "PING verified in this check",
    "PING 已验证": "PING verified",
    "BOOTSEL，尚未安装": "BOOTSEL; firmware not installed",
    "恢复通路未验证；另检测到相机普通 USB 模式": "Recovery path not verified; normal camera USB mode also detected",
    "尚未检测到相机恢复模式 USB": "Camera recovery USB not detected",
    "普通 USB 模式（当前已观察）": "Normal USB mode observed now",
    "未确认返回普通模式": "Return to normal mode unconfirmed",
    "先将 Multi 插头接相机、USB 1 和 USB 2 接电脑；再打开相机，立即点击“检查连接”。": "Connect Multi to the camera and USB 1 and USB 2 to the computer first. Then turn the camera on and immediately click ‘Check connections’.",
    "先将 Multi 插头接相机、USB 1 和 USB 2 的 USB-C 插头接电脑；再打开相机，立即点击“检查连接”进入临时恢复模式并读取 USB 2。": "First connect Multi to the camera and the USB-C plugs of USB 1 and USB 2 to the computer. Then turn the camera on and immediately click ‘Check connections’ to enter temporary recovery mode and read USB 2.",
    "USB 1（RP2040）：": "USB 1 (RP2040): ",
    "USB 2（DATA）：": "USB 2 (DATA): ",
    "相机当前模式：": "Current camera mode: ",
    "本次恢复模式读出已验证 · ": "Recovery-mode read verified in this check · ",
    "两路连接本次已验证；相机已返回普通模式。确认屏幕没有正在进行的更新后，可以拔线。": "Both connections passed this check and the camera returned to normal mode. If the screen shows no update in progress, you may disconnect the cables.",
    "两路连接本次已验证；返回普通模式状态不明。请勿断电或自动重试，查看相机屏幕和日志。": "Both connections passed this check, but return to normal mode is uncertain. Do not disconnect power or retry automatically; check the camera screen and log.",
    "两路连接本次已验证；尚未确认相机返回普通模式。请查看屏幕，暂勿拔线。": "Both connections passed this check, but return to normal mode is unconfirmed. Check the screen and keep cables connected.",
    "两路连接本次已验证；安全检查阻止返回普通模式。请查看日志，暂勿拔线或重试。": "Both connections passed this check, but a safety check blocked return to normal mode. Read the log; keep cables connected and do not retry.",
    "两路连接本次已验证；当前模式信息不完整。请查看日志，暂勿拔线。": "Both connections passed this check, but current mode information is incomplete. Read the log and keep cables connected.",
    "两路连接本次已验证。": "Both connections passed this check.",
    "相机未报告恢复模式就绪。检查电源与 Multi 插头。": "The camera did not report recovery mode ready. Check power and the Multi plug.",
    "工程端已就绪，但 USB 2 未出现。检查 USB 2 的 USB-C 连接。": "The adapter is ready, but USB 2 did not appear. Check its USB-C connection.",
    "先准备并验证 Multi 转接头。": "Prepare and verify the Multi adapter first.",
    "恢复模式 DATA 通路尚未验证。": "The recovery-mode DATA path has not been verified.",
    "连接检查未完成，请查看错误并重新检查。": "The connection check did not finish. Review the error and check again.",
    // SD card and stock recovery.
    "去下载固件包": "Download firmware package",
    "更多下载链接 🔗": "More download links 🔗",
    "使用提示": "Usage tips",
    "改过固件的相机，直接从 SD 卡恢复旧版本可能会失败。如果目的是让相机恢复正常，建议优先使用同一台相机的正常全量备份。": "Guided SD recovery to an earlier version may fail on cameras with modified firmware. To get the camera working again, start with a known-good full backup from the same camera.",
    "如果确实需要回退旧版本，但第一次失败：": "If you need to roll back and the first attempt fails:",
    "1. 用全量备份或 SD 卡引导恢复到能正常开机、进入菜单的版本。": "1. Use a full backup or guided SD recovery to reach a version that boots and opens the menus.",
    "2. 在相机上选择「设置 → 重置/保存设置 → 出厂重置 → 初始化」。": "2. On the camera, choose Setup → Reset/Save Settings → Setting Reset → Initialize.",
    "3. 初始化完成后，再用目标版本的固件包重试 SD 卡引导恢复。": "3. After initialization completes, retry guided SD recovery with the intended firmware package.",
    "我已放入相机卡槽 1": "The card is in camera slot 1",
    "帮我放进存储卡": "Help me copy it to a card",
    "选择要放入存储卡的 DAT 文件": "Choose the DAT file to copy to the card",
    "尚未选择文件": "No file selected",
    "选择 DAT 文件…": "Choose DAT file…",
    "选择电脑挂载的存储卡": "Choose a card mounted on this computer",
    "选择相机挂载的存储卡": "Choose a card mounted through the camera",
    "选择已检测到的存储卡…": "Choose a detected card…",
    "所选文件：": "Selected file: ",
    "目标存储卡：": "Target card: ",
    "未选择": "Not selected",
    "更换目标存储卡…": "Choose a different target card…",
    "我确认所选路径是相机的存储卡": "I confirm the selected volume is the camera's card",
    "写入前会校验所选 DAT 的复制完整性；不会格式化存储卡。": "The selected DAT will be checked for copy integrity; the card will not be formatted.",
    "复制回执已保存；源文件与卡上文件的字节数和摘要一致。": "Copy receipt saved; source and card file size and digest match.",
    "存储卡复制回执不完整。请查看日志并重新检查卡片。": "Card copy receipt is incomplete. Read the log and check the card again.",
    "写入存储卡": "Copy to card",
    "复制完整不代表所选固件适用于相机恢复。": "A complete copy does not mean the selected firmware is suitable for camera recovery.",
    "确认卡仍在槽 1，继续": "Confirm card is still in slot 1; continue",
    "已插入卡槽 1，继续": "Card is in slot 1; continue",
    "请等待相机更新，预计约 7 分钟。更新期间请保持供电，不要操作相机、拔线或取出存储卡。以相机屏幕显示“更新完成”为准。": "Wait for the camera update, estimated at about 7 minutes. Keep power and cables connected, and do not operate the camera or remove the card. The camera screen must show ‘Update complete’.",
    "已等待 ": "Elapsed: ",
    "恢复已进行 ": "Recovery elapsed: ",
    "本次恢复耗时 ": "Recovery elapsed: ",
    "所选目录不是有效的恢复任务。请选择 snapshot 或备份任务文件夹。": "Choose a verified snapshot folder or its backup task folder.",
    "旧恢复任务不能继续。请选择已验证的 snapshot 或备份任务文件夹，建立新的恢复任务。": "Old recovery tasks cannot be resumed. Select a verified snapshot or its backup task folder to create a new recovery task.",
    "旧恢复任务不能继续。请选择已验证的 snapshot 新建恢复任务。": "Old recovery tasks cannot be resumed. Select a verified snapshot to create a new recovery task.",
    "正在读取并保存相机当前状态…": "Reading and saving the camera's current state…",
    "正在核对相机当前状态…": "Checking the camera's current state…",
    "正在核对恢复前像…": "Checking the current state before writing…",
    "正在写入可恢复区域…": "Writing restorable regions…",
    "正在读取写入结果…": "Reading back written regions…",
    "读取当前状态中断：连续重连 ": "Current-state read interrupted: consecutive reconnects ",
    "正在重新连接以读取当前状态…": "Reconnecting to read the current state…",
    "当前状态新分段已核验：连续重连归零；累计重连 ": "New current-state segment verified: consecutive reconnects reset; total reconnects ",
    "读取并保存相机当前状态时连接中断。尚未开始恢复写入；检查连接后从所选备份新建恢复任务。": "Connection interrupted while reading and saving the camera's current state. No recovery write has started; check the connection, then create a new task from the selected backup.",
    "保存相机当前状态失败。尚未开始恢复写入；请检查保存位置和日志。": "Saving the camera's current state failed. No recovery write has started; check the destination and log.",
    "安全检查暂停了当前状态读取。尚未开始恢复写入；请查看日志后从所选备份新建恢复任务。": "A safety check paused reading the current state. No recovery write has started; read the log, then create a new task from the selected backup.",
    "等待计时暂不可用；请以相机屏幕为准。": "Elapsed time is unavailable; rely on the camera screen.",
    "相机已显示更新完成，继续": "Camera shows ‘Update complete’; continue",
    "我确认相机是 A7M4（ILCE-7M4）": "I confirm this is an A7M4 (ILCE-7M4)",
    "相机电量充足，电脑保持唤醒": "Camera has sufficient power; computer will stay awake",
    "Multi 转接头已验证": "Multi adapter verified",
    "请先检查 Multi 转接头": "Check the Multi adapter first",
    "只检查 Multi 转接头": "Check only the Multi adapter",
    "重新读取并制定方案": "Read again and create a new plan",
    "开始引导恢复": "Start guided recovery",
    "先前更新尚未确认结束。请根据相机屏幕上的实际结果选择：": "The previous update outcome is unconfirmed. Choose what the camera actually showed:",
    "上次已显示更新完成": "Previous update showed ‘Update complete’",
    "上次未插卡，更新已失败或停止": "No card was inserted; previous update failed or stopped",
    "相机持续 5 长 6 短闪灯，更新已失败": "Camera repeatedly flashed 5 long and 6 short; update failed",
    "请选择要复制的 DAT 文件和已挂载的存储卡。": "Choose a DAT file and a mounted card.",
    "请确认所选存储卷确实属于相机存储卡。": "Confirm that the selected volume is the camera's card.",
    "卡片状态已变化，请重新检查后再确认。": "Card state changed. Check it again before confirming.",
    "正在校验所选 DAT 的复制完整性…": "Verifying the selected DAT copy…",
    "卡片根目录已有不同的 BODYDATA.DAT，请确认是否替换。": "A different BODYDATA.DAT is already at the card root. Confirm whether to replace it.",
    "无法确认原文件和存储卡身份，请重新检查。": "Could not confirm the existing file and card identity. Check again.",
    "所选 DAT 已写入并复核。请安全推出存储卷，确认卡仍在相机卡槽 1。": "The selected DAT was copied and checked. Safely eject the volume and confirm the card is still in camera slot 1.",
    "所选 DAT 已写入并复核。请安全推出卡片，插入相机卡槽 1。": "The selected DAT was copied and checked. Safely eject the card and insert it in camera slot 1.",
    "所选存储卡已有相同的 DAT。请安全推出存储卷，确认卡仍在相机卡槽 1。": "The card already has the same DAT. Safely eject the volume and confirm the card is still in camera slot 1.",
    "所选存储卡已有相同的 DAT。请安全推出卡片，插入相机卡槽 1。": "The card already has the same DAT. Safely eject it and insert it in camera slot 1.",
    "存储卡尚未准备好。请查看日志中的具体原因；未确认替换前不会覆盖原文件。": "The card is not ready. Read the log for details; the existing file will not be overwritten without confirmation.",
    "已选择目标路径。写入前将核验它是否为可用存储卡。": "Target selected. It will be checked as a usable card before copying.",
    "尚未找到已挂载的存储卡。请检查连接后重试。": "No mounted card was found. Check the connection and try again.",
    "已找到挂载的存储卷。请选择并确认属于相机的存储卡。": "Mounted volumes were found. Choose and confirm which is the camera's card.",
    "已有未完成任务，暂不能切换相机模式。请保持连接并查看日志。": "An unfinished task prevents changing camera mode. Keep the cables connected and read the log.",
    "先前更新尚未确认完成。请保持供电和连接，等相机屏幕显示“更新完成”后，再点下方按钮。": "The previous update is unconfirmed. Keep power and cables connected. Use the button below only after the camera shows ‘Update complete’.",
    "相机模式切换结果不明。请保持连接，查看相机屏幕和日志；不要自动重试。": "Camera mode change is uncertain. Keep the cables connected, check the screen and log, and do not retry automatically.",
    "相机存储卡尚未挂载。请确认相机已开机并连接 USB 2，或选择电脑已挂载的卡。": "The camera card is not mounted. Make sure the camera is on and USB 2 is connected, or choose a card mounted directly on the computer.",
    "已记录前次相机更新完成。请再次点击“开始引导恢复”核对本次方案。": "The previous update completion was recorded. Click ‘Start guided recovery’ again to review a new plan.",
    "已记录相机屏幕显示更新完成。请重新点击“选择相机挂载的存储卡”。": "The camera's ‘Update complete’ display was recorded. Choose the camera-mounted card again.",
    "尚未记录更新完成。请查看日志并保持供电。": "Update completion was not recorded. Read the log and maintain power.",
    "已记录前次更新失败。请再次点击“开始引导恢复”核对本次方案。": "The previous update failure was recorded. Click ‘Start guided recovery’ again to review a new plan.",
    "已记录上次更新失败。请重新点击“选择相机挂载的存储卡”检查连接。": "The previous update failure was recorded. Choose the camera-mounted card again to check the connection.",
    "尚未记录上次更新失败。请保持连接并查看日志。": "The previous update failure was not recorded. Keep the cables connected and read the log.",
    "已记录你在相机屏幕看到更新完成。如需再次恢复，请点击“开始引导恢复”建立新任务。": "Your observation of ‘Update complete’ was recorded. To recover again, click ‘Start guided recovery’ to create a new task.",
    "等待相机屏幕显示更新完成。": "Wait until the camera screen shows ‘Update complete’.",
    "相机内容在前次读出后已变化，本次未写入。请点击“重新读取并制定方案”创建新任务。": "Camera content changed since the previous read. Nothing was written this time. Click ‘Read again and create a new plan’ to start a new task.",
    "前次更新结果尚未确认。本次方案已停止，请根据相机屏幕选择实际结果。": "The previous update outcome is unconfirmed. This plan was stopped; choose what the camera actually displayed.",
    "前次更新结果尚未确认，本次未进行新的写入或重启。请根据相机屏幕选择实际结果。": "The previous update outcome is unconfirmed. No new write or reboot occurred; choose what the camera actually displayed.",
    "已记录上次更新失败": "Previous update failure recorded",
    "请确认机型、供电和卡槽 1 的 BODYDATA.DAT。": "Confirm the camera model, power, and BODYDATA.DAT in slot 1.",
    // Backup, restore, progress, and errors.
    "备份已进行 ": "Backup elapsed: ",
    "本次备份耗时 ": "Backup duration: ",
    "估算进度 ": "Estimated progress: ",
    "备份进度按本机实测时间估算，验证完成后显示 100%。": "Progress is estimated from a measured backup. It reaches 100% after verification.",
    "尚未选择保存位置": "No save location selected",
    "选择保存位置…": "Choose save location…",
    "继续已有备份时，请选择原备份任务目录。": "To resume an existing backup, select its original task folder.",
    "最近任务目录：": "Recent task folder: ",
    "保存位置有足够空间": "The save location has enough free space",
    "开始备份": "Start backup",
    "尚未备份": "Not backed up",
    "尚未在此位置备份。": "No backup at this location yet.",
    "备份尚未开始。": "Backup has not started.",
    "正在建立备份任务…": "Creating backup task…",
    "正在备份并核验…": "Backing up and verifying…",
    "备份尚未完成；可在同一保存位置继续。": "Backup incomplete; you can continue at the same save location.",
    "备份核验尚未完成；可在同一保存位置继续。": "Backup verification incomplete; you can continue at the same save location.",
    "备份文件与收据已验证。": "Backup files and receipts verified.",
    "再次点击“开始备份”将建立新的备份。": "Click ‘Start backup’ again to create a new backup.",
    "备份尚未完成。当前任务目录已保留；请查看日志后再继续。": "Backup incomplete. The current task folder is preserved; read the log before continuing.",
    "备份中断。请查看日志后重试，已完成的文件仍保存在当前任务目录。": "Backup interrupted. Read the log before retrying; completed files remain in the current task folder.",
    "安全检查暂停了备份。当前任务目录已保留；请保持连接，查看相机状态和日志。": "A safety check paused the backup. The current task folder is preserved. Keep cables connected and check the camera and log.",
    "相机 USB 读取中断。当前任务目录已保留；确认连接稳定后，再点击“开始备份”继续。": "Camera USB read was interrupted. The task folder is preserved. Once the connection is stable, click ‘Start backup’ to continue.",
    "保存位置读写失败。当前任务目录已保留；请检查磁盘和日志后继续。": "Save location I/O failed. The task folder is preserved; check the disk and log before continuing.",
    "下次点击“开始备份”会在所选位置建立新任务；原备份文件保持不变。": "The next ‘Start backup’ will create a new task at the selected location; existing backup files remain unchanged.",
    "已选择已有备份任务，等待继续核验。": "Existing backup task selected; ready to continue verification.",
    "点击“开始备份”将继续此任务；原文件保留在所示目录。": "Click ‘Start backup’ to continue this task; its files remain in the shown folder.",
    "所选目录不是有效的相机备份任务。请选择保存位置或已有备份任务目录。": "The selected folder is not a valid camera backup task. Choose a save location or an existing backup task folder.",
    "该目录没有有效的备份任务记录。请选择保存位置或已有备份任务目录。": "This folder has no valid backup task record. Choose a save location or an existing backup task folder.",
    "尚未选择备份": "No backup selected",
    "选择要恢复的备份…": "Choose a backup to restore…",
    "可选择 snapshot 文件夹或备份任务文件夹。": "Choose a snapshot folder or its backup task folder.",
    "程序按当前可写范围恢复与备份不同的区域；如 CP NOR 无法写入且与备份不同，将明确列为未恢复。": "The tool restores regions that differ from the backup where a write route exists. If CP NOR differs but cannot be written, it will be clearly listed as not restored.",
    "程序会先保存当前状态，再核对可恢复范围。设备写入前仍会由后端执行严格检查。": "The tool first saves the current state and checks what can be restored. Strict backend checks run before any device write.",
    "开始恢复": "Start restore",
    "从所选备份重新开始": "Start fresh from selected backup",
    "原任务记录已保留；重新开始将读取相机当前状态并建立独立的新任务。": "The original task records remain. Starting fresh rereads the camera's current state and creates a separate task.",
    "阶段：": "Phase: ",
    "原因：": "Cause: ",
    "进度：": "Progress: ",
    "写入：": "Write: ",
    "重启：": "Reboot: ",
    "下一步：": "Next: ",
    "相机模式：": "Camera mode: ",
    "读取相机当前状态": "Reading the camera's current state",
    "核对恢复方案": "Checking the recovery plan",
    "核对原厂程序": "Checking factory program",
    "准备存储卡恢复": "Preparing SD card recovery",
    "等待相机更新": "Waiting for camera update",
    "执行恢复任务": "Running recovery task",
    "相机当前内容与本次方案不一致": "Current camera contents differ from this plan",
    "相机当前差异超出可恢复分区范围": "Current camera differences are outside the restorable partitions",
    "相机差异范围未能完整核对": "The camera difference range could not be checked completely",
    "相机固件结构与本次恢复方案不匹配": "Camera firmware structure does not match this recovery plan",
    "本次确认与已核对的恢复方案不一致": "This confirmation differs from the reviewed recovery plan",
    "本次任务记录无法核对": "This task record could not be checked",
    "相机连接或读取中断": "Camera connection or read interrupted",
    "本次安全核对未通过": "This task's safety check did not pass",
    "本次操作未完成；具体原因见日志": "This operation did not finish; see the log for the cause",
    "本次未写入相机": "No camera write in this task",
    "可能已写入相机": "Camera write may have occurred",
    "本次未发送重启": "No reboot sent in this task",
    "可能已发送重启；不要再次发送": "Reboot may have been sent; do not send it again",
    "重启状态无法核对": "Reboot status could not be checked",
    "重新读取相机当前状态并制定新方案。": "Reread the camera's current state and create a new plan.",
    "保留本次记录，先核对相机当前状态；不要自动重试。": "Keep this task record and check the camera's current state before retrying.",
    "写入与读回": "Writing and reading back",
    "相机 USB 读取中断": "Camera USB read interrupted",
    "相机恢复连接中断": "Camera recovery connection interrupted",
    "写入片段": "Writing a fragment",
    "写入片段时相机 USB 中断，结果待读回核对": "Camera USB interrupted during fragment write; readback is required",
    "待核对片段：": "Pending fragment: ",
    "，起始扇区": ", start sector ",
    "，扇区数": ", sectors ",
    "本机文件读写失败": "Local file I/O failed",
    "安全核对未通过": "Safety check did not pass",
    "任务未完成；具体原因见日志": "Task incomplete; see the log for details",
    "未开始写入相机": "No camera write started",
    "本次写入状态无法核对": "This task's write status could not be checked",
    "已确认写入片段：": "Confirmed written fragments: ",
    "已核验片段：": "Verified fragments: ",
    "未恢复片段：": "Unrestored fragments: ",
    "完整核验区域：": "Fully verified regions: ",
    "有一段写入结果待读取核对": "One write outcome requires readback",
    "已观察到普通 USB 模式": "Normal USB mode observed",
    "尚未确认返回普通模式": "Return to normal mode unconfirmed",
    "当前模式尚未核对": "Current mode not checked",
    "；": "; ",
    "。": ".",
    "请选择要恢复的备份。": "Choose a backup to restore.",
    "请确认相机供电。": "Confirm camera power.",
    "请确认相机供电和保存空间。": "Confirm camera power and free space at the save location.",
    "请先验证 Multi 转接头。": "Verify the Multi adapter first.",
    "请选择备份保存位置。": "Choose a backup save location.",
    "请选择有效的任务目录。": "Choose a valid task folder.",
    "不支持该任务类型。": "This task type is not supported.",
    "所选备份文件夹无效；请选择已验证的 snapshot 文件夹或其备份任务文件夹。": "Invalid backup folder. Choose a verified snapshot folder or its backup task folder.",
    "所选文件夹同时包含备份和 snapshot；请直接选择 snapshot 文件夹。": "Both the selected folder and its snapshot child contain backup records. Choose the snapshot folder directly.",
    "正在检查…": "Checking…",
    "正在准备存储卡连接…": "Preparing the card connection…",
    "正在准备存储卡…": "Preparing the card…",
    "正在记录相机更新完成确认…": "Recording camera update completion…",
    "正在验证 USB 1…": "Verifying USB 1…",
    "USB 1 已验证；正在进入临时恢复模式…": "USB 1 verified; entering temporary recovery mode…",
    "正在验证 USB 2…": "Verifying USB 2…",
    "正在准备任务…": "Preparing task…",
    "正在检查当前设备状态…": "Checking current device state…",
    "正在备份相机…": "Backing up camera…",
    "备份读取中断，正在重连…": "Backup read interrupted; reconnecting…",
    "备份读取中断：连续重连 ": "Backup read interrupted: consecutive reconnects ",
    "连续重连 ": "Consecutive reconnects ",
    "；累计重连 ": "; total reconnects ",
    " 次…": "…",
    " 次。": " times.",
    "新分段已核验：连续重连归零；累计重连 ": "New segment verified: consecutive reconnect count reset; total reconnects ",
    "正在等待相机重新连接…": "Waiting for camera to reconnect…",
    "恢复连接中断，正在重新连接并核对（第 ": "Restore connection interrupted; reconnecting and checking (attempt ",
    " 次）…": ")…",
    "恢复连接中断，正在重新连接并核对…": "Restore connection interrupted; reconnecting and checking…",
    "正在安全检查后重新进入恢复连接…": "Running safety checks before reentering recovery connection…",
    "正在检查存储卡连接方式…": "Checking card connection…",
    "正在等待存储卡挂载…": "Waiting for card to mount…",
    "正在返回正常模式…": "Returning to normal mode…",
    "正在执行任务…": "Running task…",
    "日志已导出": "Logs exported",
    "正在导出日志…": "Exporting logs…",
    "导出失败": "Export failed",
    "任务未完成。具体错误已记入日志。": "Task incomplete. Details are in the log.",
    "任务未完成。请查看日志中的具体错误，再决定下一步。": "Task incomplete. Read the logged error before deciding what to do next.",
    "存储卡准备未完成。请查看日志中的具体错误。": "Card preparation incomplete. Read the error in the log.",
    "相机存储卡连接未完成。请查看日志并保持相机供电。": "Camera card connection incomplete. Read the log and keep the camera powered.",
    "恢复方案检查未完成；具体原因见日志。": "Recovery plan check incomplete; see the log for the cause.",
    "恢复方案未通过：相机记录版本不在本工具已验证的 6.02 或 6.04 范围。本次未进入写入确认。": "Recovery plan rejected: the recorded camera version is outside this tool's verified 6.02 or 6.04 range. No write confirmation was reached.",
    "先前更新任务尚未确认完成，程序没有发送退出恢复模式命令。请保持连接，查看相机屏幕与日志；暂勿再次启动恢复。": "A previous update remains unconfirmed. No command to leave recovery mode was sent. Keep cables connected, inspect the camera screen and log, and do not start recovery again yet.",
    "安全检查阻止退出恢复模式。请保持连接，查看相机屏幕与日志；暂勿再次启动恢复。": "A safety check blocked leaving recovery mode. Keep cables connected, inspect the camera screen and log, and do not start recovery again yet.",
    "返回普通模式的结果不明。请保持连接，不要自动重试或断电。": "Return to normal mode is uncertain. Keep cables connected; do not retry automatically or remove power.",
    "返回普通模式尚未完成。请保持连接，查看相机屏幕与日志。": "Return to normal mode is incomplete. Keep cables connected and check the screen and log.",
    "请查看相机屏幕与日志，再决定下一步。": "Check the camera screen and log before deciding what to do next.",
    "本次操作已验证；相机当前为普通 USB 模式。确认屏幕没有正在进行的更新后，可以拔线。": "This operation was verified and the camera is now in normal USB mode. If the screen shows no update in progress, you may disconnect the cables.",
    "本次操作已验证，但当前相机模式信息不完整。请查看日志，暂勿拔线。": "This operation was verified, but current mode information is incomplete. Read the log and keep cables connected.",
    "本次操作已验证，但返回普通模式的状态不明。请勿自动重试或断电；查看相机屏幕和日志。": "This operation was verified, but return to normal mode is uncertain. Do not retry automatically or remove power; check the screen and log.",
    "本次操作已验证，但尚未确认相机返回普通模式。请查看相机屏幕和日志，暂勿拔线。": "This operation was verified, but return to normal mode is unconfirmed. Check the screen and log; keep cables connected.",
    "本次操作已验证，但安全检查阻止返回普通模式。请查看日志，暂勿拔线或重试。": "This operation was verified, but a safety check blocked return to normal mode. Read the log; keep cables connected and do not retry.",
    "写入状态不明。请保留收据并只读核查；不要自动重试写入。具体错误已记入日志。": "Write outcome uncertain. Preserve the receipt and check read-only; do not retry a write automatically. Details are in the log.",
    "重启状态不明。请查看相机屏幕与版本；不要再次发送重启。具体错误已记入日志。": "Reboot outcome uncertain. Check the camera screen and version; do not send another reboot. Details are in the log.",
    "相机可能已重启。请查看更新屏幕与版本；不要重新发送重启。": "The camera may have restarted. Check its update screen and version; do not send another reboot.",
    "需要查看相机当前状态后再继续；不要自动重试。具体步骤和原因已记入日志。": "Check the camera's current state before continuing; do not retry automatically. Steps and reasons are in the log.",
    "已停止，未写入相机。请查看日志中的具体原因，再重新检查。": "Stopped without writing to the camera. Read the log for the cause, then check again.",
    "任务尚未完成。请保留本次记录，查看日志后继续。": "Task incomplete. Preserve this record and read the log before continuing.",
    "已完成并验证。": "Completed and verified.",
    "检查已完成，可以继续。": "Check complete; you may continue.",
    "任务状态需要检查。请查看日志。": "Task status needs review. Read the log.",
    "相机已返回普通 USB 模式。": "Camera returned to normal USB mode.",
    "相机返回普通模式的状态不明；请保持连接并查看屏幕与日志。": "Return to normal mode is uncertain; keep cables connected and check the screen and log.",
    "安全检查阻止返回普通模式；请保持连接并查看日志。": "A safety check blocked return to normal mode; keep cables connected and read the log.",
    "尚未确认相机返回普通模式；请保持连接并查看屏幕与日志。": "Return to normal mode is unconfirmed; keep cables connected and check the screen and log.",
    "相机当前模式尚未验证；请查看屏幕与日志。": "The camera's current mode is unverified; check the screen and log.",
    "本次检查已完成": "This check is complete",
    "尚未记录更新完成。请查看日志并保持相机供电。": "Update completion was not recorded. Read the log and keep the camera powered.",
    "请先准备转接头。": "Prepare the adapter first.",
    "请先检查并验证 Multi 转接头。": "Check and verify the Multi adapter first.",
    "转接头已验证。": "Adapter verified.",
    "所选文件夹不是已验证的备份。请选择 snapshot 文件夹或其备份任务文件夹。": "The selected folder is not a verified backup. Choose a snapshot folder or its backup task folder.",
    "所选文件夹和其中的 snapshot 都含有备份记录。请直接选择 snapshot 文件夹。": "Both the selected folder and its snapshot contain backup records. Choose the snapshot folder directly.",
    "恢复成功": "Recovery succeeded",
    "跳过区域：": "Skipped regions: ",
    "跳过区域：详情缺失。请查看日志。": "Skipped region details are missing. Read the log.",
    "详情缺失。请查看日志。": "Details are missing. Read the log.",
]

private enum Page: String, CaseIterable, Identifiable {
    case board, prepareCamera, connection, stock, backup, restore
    var id: String { rawValue }
    var title: String {
        switch self {
        case .board: return "准备 Multi 转接头"
        case .prepareCamera: return "准备相机"
        case .connection: return "检查连接"
        case .stock: return "从 SD 卡恢复原厂"
        case .backup: return "备份相机"
        case .restore: return "恢复已保存状态"
        }
    }
}

private func navigationPageLines(_ page: Page, language: String) -> [String] {
    guard language == AppLanguage.english else { return [translatedText(page.title, language: language)] }
    switch page {
    case .board: return ["Prepare the Multi", "adapter"]
    case .prepareCamera: return ["Prepare", "camera"]
    case .connection: return ["Check", "connections"]
    case .stock: return ["Restore factory", "firmware from SD card"]
    case .backup: return ["Back up", "camera"]
    case .restore: return ["Restore saved", "state"]
    }
}

private struct SDCamera: Identifiable {
    let id: String
    let name: String

    static let models = [
        Self(id: "ILCE-7M4", name: "A7 IV"),
        Self(id: "ILCE-7RM5", name: "A7R V"),
        Self(id: "ILCE-7SM3", name: "A7S III"),
        Self(id: "ILCE-1", name: "A1"),
        Self(id: "ILCE-1M2", name: "A1 II"),
        Self(id: "ILCE-9M3", name: "A9 III"),
        Self(id: "ILME-FX2", name: "FX2"),
        Self(id: "ILME-FX3", name: "FX3"),
        Self(id: "ILME-FX3A", name: "FX3A"),
        Self(id: "ILME-FX6", name: "FX6"),
        Self(id: "ILME-FX30", name: "FX30"),
    ]
    static let fx6Instructions = "请安全推出卡片。连接原装 AC 电源，将存有 BODYDATA.DAT 的 SDXC 或 CFexpress Type A 卡插入卡槽 B。打开 Maintenance（维护）→ Version（版本）→ Version Up（版本更新），核对版本后按屏幕提示执行。"
    static let slot1Instructions = "将卡插入卡槽 1，打开菜单 → 设置 → 设置选项 → 版本。如果有“机身”，先选“机身”，再选“软件更新”。请按相机提示核对目标版本并完成更新。"

    var supportsAdapterRecovery: Bool { ["ILCE-7M4", "ILCE-7RM5"].contains(id) }
    var firmwareDownloadsURL: URL? {
        let links = [
            "ILCE-7M4": "https://bluepotato.shop/a74images.html",
            "ILCE-7RM5": "https://bluepotato.shop/a7r5images.html",
        ]
        return links[id].flatMap { URL(string: $0) }
    }
    var updateInstructions: String { id == "ILME-FX6" ? Self.fx6Instructions : Self.slot1Instructions }
    var supportURL: URL {
        let product = id == "ILME-FX6" ? "ilme-fx6v" : id.lowercased()
        return URL(string: "https://www.sony.com/electronics/support/product/\(product)/downloads")!
    }
}

private enum SDStep {
    case entry, source, target, slot, ready, officialUpdate
}

private func stockPlannerFailureMessage(_ error: NSError) -> String {
    let cause = error.localizedDescription.contains("recorded version outside approved settings range") ?
        "恢复方案未通过：相机记录版本不在本工具已验证的 6.02 或 6.04 范围。本次未进入写入确认。" :
        "恢复方案检查未完成；具体原因见日志。"
    let status = error.userInfo["normal_mode_status"] as? String ?? ""
    let mode = error.userInfo["current_mode"] as? String ?? ""
    let blockers = error.userInfo["blocking_tasks"] as? [[String: Any]] ?? []
    let earlierUpdate = blockers.contains {
        ($0["reason"] as? String ?? "").localizedCaseInsensitiveContains("REBOOT_ACKNOWLEDGED")
    }
    let next: String
    switch status {
    case "READY" where mode == "MSC" || mode == "NORMAL_USB":
        next = "相机已返回普通 USB 模式。"
    case "NO_WRITE" where earlierUpdate:
        next = "先前更新任务尚未确认完成，程序没有发送退出恢复模式命令。请保持连接，查看相机屏幕与日志；暂勿再次启动恢复。"
    case "NO_WRITE":
        next = "安全检查阻止退出恢复模式。请保持连接，查看相机屏幕与日志；暂勿再次启动恢复。"
    case "REBOOT_UNCERTAIN":
        next = "返回普通模式的结果不明。请保持连接，不要自动重试或断电。"
    case "NEEDS_USER_ACTION", "INCOMPLETE":
        next = "返回普通模式尚未完成。请保持连接，查看相机屏幕与日志。"
    default:
        next = "请查看相机屏幕与日志，再决定下一步。"
    }
    return cause + "\n\n" + next
}

private func currentFailureCause(_ result: [String: Any]) -> String {
    let original = (result["failure_cause"] as? String ?? "").lowercased()
    let failedCheck = (result["failed_check"] as? String ?? "").lowercased()
    let code = (result["error_code"] as? String ?? "").lowercased()
    if original.contains("difference outside mapped partitions") || failedCheck.contains("outside_mapped") {
        return "相机当前差异超出可恢复分区范围"
    }
    if original.contains("raw user-area range detail truncated") || failedCheck.contains("range_detail") {
        return "相机差异范围未能完整核对"
    }
    if original.contains("unrecognized live sectors") {
        return "相机当前内容与本次方案不一致"
    }
    if code == "version_structure_mismatch" || original.contains("recorded version outside approved settings range") {
        return "相机固件结构与本次恢复方案不匹配"
    }
    if original.contains("saved stock confirmation differs from pinned plan") || original.contains("plan digest") {
        return "本次确认与已核对的恢复方案不一致"
    }
    if failedCheck.contains("journal") || original.contains("journal unreadable") {
        return "本次任务记录无法核对"
    }
    if code.contains("usb") || code.contains("fastboot") || original.contains("usb") {
        return "相机连接或读取中断"
    }
    if code.contains("oserror") || code.contains("ioerror") || code == "local_io" || code == "destination_io" {
        return "本机文件读写失败"
    }
    if code.contains("valueerror") || code.contains("runtimeerror") || code.contains("safety") {
        return "本次安全核对未通过"
    }
    return "本次操作未完成；具体原因见日志"
}

private func stockCurrentFailureMessage(_ result: [String: Any]) -> String {
    let phase: String
    switch result["phase"] as? String {
    case "plan": phase = "核对恢复方案"
    case "updater": phase = "核对原厂程序"
    case "settings": phase = "准备存储卡恢复"
    case "reboot": phase = "等待相机更新"
    default: phase = "执行恢复任务"
    }
    let cause = currentFailureCause(result)
    let wrote = result["write_may_have_occurred"] as? Bool
    let rebooted = result["reboot_may_have_occurred"] as? Bool
    let writeText = wrote == true ? "可能已写入相机" :
        wrote == false ? "本次未写入相机" : "本次写入状态无法核对"
    let fragments = (result["confirmed_fragments"] as? Int).map { "；已确认片段：\($0)" } ?? ""
    let rebootText = rebooted == true ? "可能已发送重启；不要再次发送" :
        rebooted == false ? "本次未发送重启" : "重启状态无法核对"
    let next = wrote == false && rebooted == false ?
        "重新读取相机当前状态并制定新方案。" :
        "保留本次记录，先核对相机当前状态；不要自动重试。"
    return "阶段：\(phase)\n原因：\(cause)\n写入：\(writeText)\(fragments)\n重启：\(rebootText)\n下一步：\(next)"
}

private func parseISODate(_ stamp: String) -> Date? {
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let plain = ISO8601DateFormatter()
    plain.formatOptions = [.withInternetDateTime]
    return fractional.date(from: stamp) ?? plain.date(from: stamp)
}

// Measured from the 3.00 backup's read start to its final verification result.
private let backupEstimateSeconds: TimeInterval = (10 * 60 + 48) * 1.5

private func estimatedBackupProgress(elapsed: TimeInterval, verified: Bool) -> Double {
    if verified { return 1 }
    return 0.95 * min(1, max(0, elapsed / backupEstimateSeconds))
}

private func elapsedStockTime(since start: Date, now: Date) -> String {
    let seconds = max(0, Int(now.timeIntervalSince(start)))
    if seconds >= 3_600 {
        return String(format: "%02d:%02d:%02d", seconds / 3_600, (seconds / 60) % 60, seconds % 60)
    }
    return String(format: "%02d:%02d", seconds / 60, seconds % 60)
}

private func localLogLine(_ row: String, timeZone: TimeZone = .current) -> String {
    guard let separator = row.range(of: "  ") else { return row }
    let stamp = String(row[..<separator.lowerBound])
    guard let date = parseISODate(stamp) else { return row }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "en_US_POSIX")
    formatter.timeZone = timeZone
    formatter.dateFormat = "yyyy-MM-dd HH:mm:ss zzz"
    return formatter.string(from: date) + "  " + row[separator.upperBound...]
}

private func backupDestinationPath(_ url: URL) -> String {
    url.standardizedFileURL.resolvingSymlinksInPath().path
}

private enum WorkerLocation {
    static func command() -> (String, [String], String?) {
        let environment = ProcessInfo.processInfo.environment
        if let path = environment["CAMERA_RECOVERY_WORKER"] {
            return (path, ["--worker"], nil)
        }
        if let bundle = Bundle.main.resourceURL?.appendingPathComponent("worker/CameraRecoveryWorker"),
           FileManager.default.isExecutableFile(atPath: bundle.path) {
            return (bundle.path, ["--worker"], nil)
        }
        let root = environment["CAMERA_RECOVERY_SOURCE_ROOT"] ?? FileManager.default.currentDirectoryPath
        return (environment["CAMERA_RECOVERY_PYTHON"] ?? "/usr/local/bin/python3",
                ["-m", "recovery_tool.task_worker"], root)
    }
}

private func stageSupportLogs(from source: URL, into staging: URL,
                              taskDirectories: [URL] = []) throws {
    let manager = FileManager.default
    try manager.createDirectory(at: staging, withIntermediateDirectories: true)
    var hashes: [[String: String]] = []
    func copy(_ file: URL, as relative: String) throws {
        let attributes = try file.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .fileSizeKey])
        guard attributes.isRegularFile == true, attributes.isSymbolicLink != true,
              (attributes.fileSize ?? Int.max) <= 1_000_000 else { return }
        let target = staging.appendingPathComponent(relative)
        try manager.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try Data(contentsOf: file)
        try data.write(to: target)
        let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        hashes.append(["path": relative, "sha256": digest])
    }
    let rootFiles = try manager.contentsOfDirectory(at: source, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    for file in rootFiles where file.lastPathComponent == "native-session.log" ||
        (file.pathExtension == "jsonl" && file.lastPathComponent.hasPrefix("session-")) {
        try copy(file, as: file.lastPathComponent)
    }
    let taskLogs = source.appendingPathComponent("tasks", isDirectory: true)
    if let files = try? manager.contentsOfDirectory(at: taskLogs, includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey]) {
        for file in files where file.pathExtension == "jsonl" && file.lastPathComponent.hasPrefix("task-") {
            try copy(file, as: file.lastPathComponent)
        }
    }
    let receiptNames = ["task.json", "journal.json", "capture-receipt.json",
                        "restore-start.json", "stock-authorization.json", "stock-composite-plan.json",
                        "nor-flow.json", "nor-admission.json", "restore-result.json", "nor-rebuild/job.json",
                        "nor-rebuild/live-trial-receipt.json", "nor-rebuild/native-normal-usb.json",
                        "nor-rebuild/cleanup-plan.json", "nor-rebuild/cleanup-receipt.json",
                        "nor-rebuild/normal-return-receipt.json", "nor-rebuild/collection/collection.json",
                        "nor-rebuild/install-transaction/manifest.json", "nor-rebuild/install-transaction/journal.json",
                        "nor-rebuild/cleanup-transaction/manifest.json", "nor-rebuild/cleanup-transaction/journal.json"]
    for (index, task) in taskDirectories.enumerated() {
        for name in receiptNames {
            let file = task.appendingPathComponent(name)
            if manager.fileExists(atPath: file.path) {
                try copy(file, as: "receipts/task-\(index + 1)/\(name)")
            }
        }
    }
    let manifest = try JSONSerialization.data(withJSONObject: ["schema": "camera-support-archive-v1", "files": hashes], options: [.prettyPrinted, .sortedKeys])
    try manifest.write(to: staging.appendingPathComponent("manifest.json"))
}

private final class WorkerClient {
    func call(_ request: [String: Any], progress: @escaping ([String: Any]) -> Void,
              completion: @escaping (Result<[String: Any], Error>) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let (executable, arguments, directory) = WorkerLocation.command()
            let process = Process()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments
            if let directory { process.currentDirectoryURL = URL(fileURLWithPath: directory) }
            let input = Pipe(), output = Pipe(), errors = Pipe()
            process.standardInput = input
            process.standardOutput = output
            process.standardError = errors
            do {
                let encoded = try JSONSerialization.data(withJSONObject: request)
                try process.run()
                try? output.fileHandleForWriting.close()
                try? errors.fileHandleForWriting.close()
                try? input.fileHandleForReading.close()
                var errorText = ""
                let errorGroup = DispatchGroup()
                errorGroup.enter()
                DispatchQueue.global(qos: .utility).async {
                    let data = errors.fileHandleForReading.readDataToEndOfFile()
                    errorText = String(data: data, encoding: .utf8) ?? ""
                    errorGroup.leave()
                }
                input.fileHandleForWriting.write(encoded + Data([10]))
                try? input.fileHandleForWriting.close()
                var buffer = Data()
                var result: [String: Any]?
                var workerError: [String: Any]?
                while true {
                    let chunk = output.fileHandleForReading.availableData
                    if chunk.isEmpty { break }
                    buffer.append(chunk)
                    while let newline = buffer.firstIndex(of: 10) {
                        let line = Data(buffer[..<newline])
                        buffer.removeSubrange(...newline)
                        guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                              let type = event["type"] as? String else { continue }
                        if type == "progress", let detail = event["event"] as? [String: Any] {
                            DispatchQueue.main.async { progress(detail) }
                        } else if type == "result" {
                            result = event["result"] as? [String: Any]
                        } else if type == "error" {
                            workerError = event
                        }
                    }
                }
                process.waitUntilExit()
                errorGroup.wait()
                let terminationStatus = process.terminationStatus
                let failure = (workerError?["error"] as? String) ??
                    (terminationStatus == 0 ? nil : errorText)
                DispatchQueue.main.async {
                    if let failure {
                        var details: [String: Any] = [NSLocalizedDescriptionKey: failure,
                                                      "worker_termination_status": Int(terminationStatus)]
                        for key in ["normal_mode_status", "current_mode", "normal_mode_error", "blocking_tasks",
                                    "status", "phase", "error_code", "failure_cause", "write_may_have_occurred",
                                    "reboot_may_have_occurred", "confirmed_fragments",
                                    "validation_mode", "verified_fragments", "failed_fragments",
                                    "pending_fragment", "original_journal_error", "secondary_failure_cause",
                                    "camera_error_detail", "exception_chain",
                                    "inflight", "next_action",
                                    "failed_check", "journal_valid", "errno"] {
                            if let value = workerError?[key] { details[key] = value }
                        }
                        completion(.failure(NSError(domain: "CameraRecoveryWorker", code: 1,
                                                     userInfo: details)))
                    } else if var result {
                        result["_worker_termination_status"] = Int(terminationStatus)
                        completion(.success(result))
                    } else {
                        completion(.failure(NSError(domain: "CameraRecoveryWorker", code: 2,
                                                     userInfo: [NSLocalizedDescriptionKey: "Worker returned no result"])))
                    }
                }
            } catch {
                try? input.fileHandleForWriting.close()
                DispatchQueue.main.async { completion(.failure(error)) }
            }
        }
    }
}

@MainActor private final class RecoveryModel: ObservableObject {
    @Published var page: Page = .board
    @Published var busy = false
    @Published var stage = ""
    @Published var progressFraction: Double?
    @Published var restorePlannedFragments: Int?
    @Published var restoreVerifiedFragments = 0
    @Published var restoreFailedFragments = 0
    @Published var logs = ""
    @Published var errorText: String?
    @Published var boardState = "待检查"
    @Published var boardMessage = "只把 USB 1（RP2040）接到电脑；USB 2 和 Multi 插头暂不连接。"
    @Published var showBootSteps = false
    @Published var showBoardInstall = false
    @Published var boardReadyToInstall = false
    @Published var controlState = "尚未检查"
    @Published var dataState = "尚未检查"
    @Published var currentModeState = "尚未检查"
    @Published var controlVerified = false
    @Published var dataVerified = false
    @Published var connectionMessage = "先将 Multi 插头接相机、USB 1 和 USB 2 接电脑；再打开相机，立即点击“检查连接”。"
    @Published var stockModelConfirmed = false
    @Published var stockCardConfirmed = false
    @Published var stockPowerConfirmed = false
    @Published var sdStep: SDStep = .entry
    @Published var sdCameraModel = "ILCE-7M4" {
        didSet {
            guard oldValue != sdCameraModel else { return }
            invalidateCachedStockPlan()
            stockModelConfirmed = false
            stockCardConfirmed = false
            sdPreparationReceipt = nil
            sdStep = .entry
        }
    }
    var connectionCameraModel: String { sdCameraModel == "ILCE-7RM5" ? sdCameraModel : "ILCE-7M4" }
    var selectedSDCamera: SDCamera {
        SDCamera.models.first { $0.id == sdCameraModel } ?? SDCamera.models[0]
    }
    @Published var sdInCameraUpdate = false
    @Published var sdPackageLabel = ""
    @Published var sdArrivedFromHelper = false
    @Published var showSDReplacement = false
    @Published var sdUsingCameraRoute = false
    @Published var sdCameraVolumeConfirmed = false
    @Published var sdCameraCandidates: [String] = []
    @Published var sdBlockingStockJob: String?
    @Published var requestSDVolumePicker = false
    @Published var showPriorUpdateFailureConfirmation = false
    @Published var priorUpdateFailureReason = "no_sd_card"
    @Published var sdSourceDAT: URL?
    @Published var sdVolume: URL?
    @Published var sdPreparationMessage = ""
    @Published var sdCopyProofText = ""
    @Published var powerConfirmed = false
    @Published var spaceConfirmed = false
    @Published var outputDirectory: URL?
    @Published var backupJobPath: String?
    @Published var backupEvidenceState = "尚未备份"
    @Published var backupModeState = ""
    @Published var restoreTarget: URL?
    @Published var showStockConfirmation = false
    @Published var showStockCompletionButton = false
    @Published var stockRebootAcknowledgedAt: Date?
    @Published var showFreshStockPlanAction = false
    @Published private var taskMessages: [String: String] = [:]
    var taskMessage: String {
        get { taskMessages[page.rawValue] ?? "" }
        set { taskMessages[page.rawValue] = newValue }
    }
    @Published var restoreHasSkippedRegions = false
    @Published var restoreHasPartialOutcome = false
    @Published var restoreSkippedLabel: String? = nil
    @Published var restoreFailureObserved = false
    @Published private(set) var backupStartedAt: Date?
    @Published private(set) var backupFinishedAt: Date?
    @Published private(set) var backupVerified = false
    @Published var restoreStartedAt: Date?
    @Published var restoreFinishedAt: Date?
    @Published var restoreNORStartedAt: Date?
    @Published var restoreNORFinishedAt: Date?
    @Published var restoreNORStatus = ""
    @Published var restoreFullyVerified = false

    private let worker = WorkerClient()
    private var boardJob: String?
    private var stockJob: String?
    private var backupJob: String?
    private var previousBackupJobs: [String] = []
    private var restoreJob: String?
    private var restoreStartPinned = false
    private var stockPlanDigest: String?
    private var stockNoWriteFailure = false
    private var requireFreshStockReview = false
    private var sdExistingHash: String?
    private var sdVolumeDevice: String?
    private var sdPreparationReceipt: String?
    private let logQueue = DispatchQueue(label: "camera.recovery.native.log", qos: .utility)
    private let logDirectory: URL = {
        if let testPath = ProcessInfo.processInfo.environment["CAMERA_RECOVERY_TEST_LOG_DIRECTORY"] {
            return URL(fileURLWithPath: testPath, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/Camera Recovery Logs", isDirectory: true)
    }()
    private let taskDirectory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Camera Recovery/Tasks", isDirectory: true)

    init() {
        log("Native app opened.")
    }

    private func invalidateCachedStockPlan() {
        stockJob = nil
        stockPlanDigest = nil
        showStockConfirmation = false
    }

    private func setStockWaitingState(_ result: [String: Any]) {
        showStockCompletionButton = result["stock_reboot_acknowledged"] as? Bool == true
        stockRebootAcknowledgedAt = showStockCompletionButton ?
            parseISODate(value(result, "stock_reboot_acknowledged_utc")) : nil
    }

    private func value(_ result: [String: Any], _ key: String) -> String {
        result[key] as? String ?? ""
    }

    private func savedRestoreStart(_ job: String) -> Date? {
        let file = URL(fileURLWithPath: job).appendingPathComponent("restore-start.json")
        guard let data = try? Data(contentsOf: file),
              let receipt = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              receipt["schema"] as? String == "restore-start-v1",
              let stamp = receipt["started_utc"] as? String else { return nil }
        return parseISODate(stamp)
    }

    private func pinRestoreStartIfAvailable(_ job: String) {
        guard !restoreStartPinned, let started = savedRestoreStart(job) else { return }
        restoreStartedAt = started
        restoreStartPinned = true
    }

    @discardableResult private func capturePriorStockBlocker(_ result: [String: Any]) -> Bool {
        let blockers = result["blocking_tasks"] as? [[String: Any]] ?? []
        let previous = blockers.first {
            ($0["reason"] as? String ?? "").localizedCaseInsensitiveContains("REBOOT_ACKNOWLEDGED")
        }
        sdBlockingStockJob = previous?["job_dir"] as? String
        return sdBlockingStockJob != nil
    }

    private func backupRetryText(_ result: [String: Any]) -> String {
        let used = result["backup_retry_used"] as? Int ?? 0
        let total = result["backup_retry_total"] as? Int ?? 0
        let limit = result["backup_retry_limit"] as? Int ?? 0
        return total > 0 && limit > 0 ?
            "连续重连 \(used)/\(limit)；累计重连 \(total) 次。" : ""
    }

    private func backupIncompleteMessage(_ result: [String: Any]) -> String {
        let count = backupRetryText(result)
        let blockers = result["blocking_tasks"] as? [[String: Any]] ?? []
        if !blockers.isEmpty {
            return count + "安全检查暂停了备份。当前任务目录已保留；请保持连接，查看相机状态和日志。"
        }
        switch value(result, "backup_failure_origin") {
        case "camera_usb_read":
            return count + "相机 USB 读取中断。当前任务目录已保留；确认连接稳定后，再点击“开始备份”继续。"
        case "destination_io":
            return "保存位置读写失败。当前任务目录已保留；请检查磁盘和日志后继续。"
        default:
            return count + "备份尚未完成。当前任务目录已保留；请查看日志后再继续。"
        }
    }

    private func restoreIncompleteMessage(_ result: [String: Any]) -> String {
        if let phase = result["phase"] as? String {
            let phaseText: String
            switch phase {
            case "capture": phaseText = "读取相机当前状态"
            case "plan": phaseText = "核对恢复方案"
            case "flash": phaseText = "写入片段"
            case "transaction": phaseText = "写入与读回"
            case "nor", "nor_rebuild": phaseText = "重建启动数据"
            default: phaseText = "核对恢复方案"
            }
            let cause: String
            switch value(result, "error_code") {
            case "camera_usb_read": cause = "相机 USB 读取中断"
            case "camera_usb_flash": cause = "写入片段时相机 USB 中断，结果待读回核对"
            case "camera_transport": cause = "相机恢复连接中断"
            case "destination_io", "local_io": cause = "本机文件读写失败"
            case "safety_guard", "safety_or_plan_check": cause = currentFailureCause(result)
            default: cause = currentFailureCause(result)
            }
            let wrote = result["write_may_have_occurred"] as? Bool
            let fragments = result["confirmed_fragments"] as? Int
            let regions = result["verified_regions"] as? [String]
            let progress: String
            if result["validation_mode"] as? String == "fragment-readback-v1" {
                let verified = (result["verified_fragments"] as? Int).map(String.init) ?? "?"
                let failed = (result["failed_fragments"] as? Int).map(String.init) ?? "?"
                let pending = result["inflight"] is NSNull || result["inflight"] == nil ?
                    "" : "；有一段写入结果待读取核对"
                let noWrite = wrote == false ? "；未开始写入相机" : ""
                let fragment = result["pending_fragment"] as? [String: Any]
                let location: String
                if let fragment, let region = fragment["region"] as? String,
                   let index = fragment["index"] as? Int,
                   let start = fragment["start_sector"] as? Int,
                   let sectors = fragment["sectors"] as? Int {
                    location = "；待核对片段：\(region) #\(index + 1)，起始扇区\(start)，扇区数\(sectors)"
                } else {
                    location = ""
                }
                progress = "已核验片段：\(verified)；未恢复片段：\(failed)\(pending)\(location)\(noWrite)"
            } else if wrote == true, let fragments, let regions {
                progress = "已确认写入片段：\(fragments)；完整核验区域：\(regions.count)。" +
                    (result["inflight"] is NSNull || result["inflight"] == nil ? "" : "有一段写入结果待读取核对。")
            } else if wrote == false {
                progress = "未开始写入相机" +
                    (regions.map { "；完整核验区域：\($0.count)。" } ?? "")
            } else {
                progress = "本次写入状态无法核对"
            }
            let mode: String
            if value(result, "normal_mode_status") == "READY" &&
                ["MSC", "NORMAL_USB"].contains(value(result, "current_mode")) {
                mode = "已观察到普通 USB 模式"
            } else if result["normal_mode_status"] != nil {
                mode = "尚未确认返回普通模式"
            } else {
                mode = "当前模式尚未核对"
            }
            return "阶段：\(phaseText)\n原因：\(cause)\n进度：\(progress)\n相机模式：\(mode)"
        }
        let used = result["restore_retry_used"] as? Int ?? 0
        let total = result["restore_retry_total"] as? Int ?? 0
        let limit = result["restore_retry_limit"] as? Int ?? 0
        let retries = total > 0 && limit > 0 ?
            "连续重连 \(used)/\(limit)；累计重连 \(total) 次。" : ""
        switch value(result, "restore_failure_origin") {
        case "camera_usb_read":
            return retries + "读取并保存相机当前状态时连接中断。尚未开始恢复写入；检查连接后从所选备份新建恢复任务。"
        case "destination_io":
            return "保存相机当前状态失败。尚未开始恢复写入；请检查保存位置和日志。"
        case "safety_guard":
            return retries + "安全检查暂停了当前状态读取。尚未开始恢复写入；请查看日志后从所选备份新建恢复任务。"
        default:
            return taskResultText(result)
        }
    }

    private func taskResultText(_ result: [String: Any]) -> String {
        let status = value(result, "status")
        let normalStatus = value(result, "normal_mode_status")
        let currentMode = value(result, "current_mode")
        if status == "VERIFIED" || status == "READY" {
            switch normalStatus {
            case "READY" where currentMode == "MSC" || currentMode == "NORMAL_USB":
                return "本次操作已验证；相机当前为普通 USB 模式。确认屏幕没有正在进行的更新后，可以拔线。"
            case "READY": return "本次操作已验证，但当前相机模式信息不完整。请查看日志，暂勿拔线。"
            case "REBOOT_UNCERTAIN": return "本次操作已验证，但返回普通模式的状态不明。请勿自动重试或断电；查看相机屏幕和日志。"
            case "NEEDS_USER_ACTION": return "本次操作已验证，但尚未确认相机返回普通模式。请查看相机屏幕和日志，暂勿拔线。"
            case "NO_WRITE": return "本次操作已验证，但安全检查阻止返回普通模式。请查看日志，暂勿拔线或重试。"
            default: break
            }
        }
        switch status {
        case "UNKNOWN_AFTER_WRITE": return "写入状态不明。请保留收据并只读核查；不要自动重试写入。具体错误已记入日志。"
        case "REBOOT_UNCERTAIN": return "重启状态不明。请查看相机屏幕与版本；不要再次发送重启。具体错误已记入日志。"
        case "NEEDS_USER_ACTION":
            return value(result, "summary").localizedCaseInsensitiveContains("reboot") ?
                "相机可能已重启。请查看更新屏幕与版本；不要重新发送重启。" :
                "需要查看相机当前状态后再继续；不要自动重试。具体步骤和原因已记入日志。"
        case "NO_WRITE": return "已停止，未写入相机。请查看日志中的具体原因，再重新检查。"
        case "INCOMPLETE": return "任务尚未完成。请保留本次记录，查看日志后继续。"
        case "VERIFIED": return "已完成并验证。"
        case "READY": return "检查已完成，可以继续。"
        default: return "任务状态需要检查。请查看日志。"
        }
    }

    private func newJob(_ kind: String, in parent: URL? = nil) -> String {
        let base = parent ?? taskDirectory
        let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        return base.appendingPathComponent("\(kind)-\(stamp)-\(UUID().uuidString.prefix(6))").path
    }

    func log(_ message: String) {
        let row = "\(ISO8601DateFormatter().string(from: Date()))  \(message)\n"
        logs += localLogLine(row)
        let directory = logDirectory
        logQueue.async {
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let file = directory.appendingPathComponent("native-session.log")
            if !FileManager.default.fileExists(atPath: file.path) { FileManager.default.createFile(atPath: file.path, contents: nil) }
            if let handle = try? FileHandle(forWritingTo: file) {
                handle.seekToEndOfFile()
                handle.write(Data(row.utf8))
                try? handle.close()
            }
        }
    }

    private func run(_ request: [String: Any], onResult: @escaping ([String: Any]) -> Void) {
        guard !busy else { return }
        taskMessage = ""
        busy = true
        switch request["action"] as? String {
        case "prepare_card_connection": stage = "正在准备存储卡连接…"
        case "sd_card_prepare": stage = "正在准备存储卡…"
        case "confirm_stock_update_complete": stage = "正在记录相机更新完成确认…"
        case "restore_run": stage = "正在校验本机备份…"
        default: stage = "正在检查…"
        }
        if request["action"] as? String == "restore_run" {
            log("Restore run requested; checking local backup pins")
        }
        progressFraction = nil
        worker.call(request, progress: { [weak self] event in
            guard let self else { return }
            let restoring = request["action"] as? String == "restore_run"
            if restoring, let job = request["job_dir"] as? String {
                self.pinRestoreStartIfAvailable(job)
            }
            let name = event["stage"] as? String ?? "progress"
            let message = event["message"] as? String ?? ""
            self.log("\(name): \(message)")
            // The NOR flow also emits low-level transport/transaction events.
            // Keep its current task phase visible while retaining every log line.
            if restoring, self.restoreNORStartedAt != nil, !name.hasPrefix("restore_nor_") {
                self.progressFraction = nil
                return
            }
            if restoring, name.hasPrefix("restore_nor_") {
                if self.restoreNORStartedAt == nil {
                    let elapsed = max(0, event["elapsed_seconds"] as? Int ?? 0)
                    self.restoreNORStartedAt = Date().addingTimeInterval(-Double(elapsed))
                }
                self.restoreNORStatus = "RUNNING"
                self.progressFraction = nil
            }
            switch name {
            case "snapshot_nor_profile": self.stage = "正在核对备份的启动数据配置…"
            case "restore_nor_prepare": self.stage = "存储恢复已验证，正在准备重建启动数据…"
            case "restore_nor_install": self.stage = "正在准备相机启动数据重建…"
            case "restore_nor_wait": self.stage = "正在重建并校验启动数据，请保持供电和连接…"
            case "restore_nor_collect": self.stage = "正在核对启动数据重建结果…"
            case "restore_nor_cleanup": self.stage = "正在清理临时恢复入口…"
            case "restore_nor_normal": self.stage = "正在返回普通相机模式…"
            case "control": self.stage = "正在验证 USB 1…"
            case "bridge": self.stage = "正在通过 Multi 转接头进入临时恢复模式…"
            case "bridge_upload": self.stage = "临时恢复程序已发送，正在等待相机就绪（最多 25 秒）…"
            case "uart": self.stage = message.contains("absent") ?
                "相机未报告就绪，正在检查 USB 2 是否稍后出现…" :
                "相机已报告就绪，正在等待 USB 2…"
            case "bridge_diagnostic": self.stage =
                (message.contains("result=fastboot_verified") || message.contains("result=late_fastboot_verified")) ?
                "临时恢复模式已就绪，正在核对相机…" :
                "本次进入恢复模式未就绪，诊断已记录。"
            case "manual_retry": self.stage = "已记录本次手动重试，正在校验恢复文件…"
            case "restore_read_session_exit": self.stage = message.contains("acknowledged") ?
                "相机读取阶段已结束，正在校验本机备份…" :
                "正在结束相机读取阶段，随后校验本机备份…"
            case "restore_write_session_entry": self.stage = "本机备份已核验，正在建立相机恢复连接…"
            case "restore_idle_probe": self.stage = "正在确认相机恢复连接仍可读取…"
            case "restore_idle_reentry": self.stage = "本机校验期间，正在安全地重新进入恢复模式…"
            case "restore_capture_idle_reentry": self.stage = "保存当前状态期间，正在安全地重新进入恢复模式…"
            case "restore_local_verify":
                let phase = event["phase"] as? String ?? ""
                let file = event["file"] as? String ?? ""
                let done = event["bytes_done"] as? Int
                let total = event["bytes_total"] as? Int
                let amount: String
                if let done, let total {
                    amount = " \(ByteCountFormatter.string(fromByteCount: Int64(done), countStyle: .binary)) / \(ByteCountFormatter.string(fromByteCount: Int64(total), countStyle: .binary))"
                    if total > 0 { self.progressFraction = min(1, max(0, Double(done) / Double(total))) }
                } else if let total {
                    amount = " \(ByteCountFormatter.string(fromByteCount: Int64(total), countStyle: .binary))"
                } else {
                    amount = ""
                }
                self.stage = phase == "done" ?
                    "本机备份校验完成，正在检查相机连接…" :
                    "正在校验本机恢复文件…" + (file.isEmpty ? "" : " \(file)") + amount
            case "data": self.stage = "正在验证 USB 2…"
            case "prepare": self.stage = "正在准备任务…"
            case "reconcile": self.stage = "正在检查当前设备状态…"
            case "capture": self.stage = "正在备份相机…"
            case "snapshot" where restoring: self.stage = "正在读取并保存相机当前状态…"
            case "identity" where restoring: self.stage = "正在核对相机当前状态…"
            case "prewrite" where restoring: self.stage = "正在核对恢复前像…"
            case "write" where restoring: self.stage = "正在写入可恢复区域…"
            case "restore_plan_summary" where restoring:
                self.restorePlannedFragments = event["planned_fragments"] as? Int
                self.restoreVerifiedFragments = 0
                self.restoreFailedFragments = 0
                self.stage = "恢复计划已计算，正在准备写入…"
            case "fragment_write_verified" where restoring,
                 "fragment_write_failed" where restoring:
                self.restorePlannedFragments = event["planned_fragments"] as? Int ?? self.restorePlannedFragments
                self.restoreVerifiedFragments = event["verified_fragments"] as? Int ?? self.restoreVerifiedFragments
                self.restoreFailedFragments = event["failed_fragments"] as? Int ?? self.restoreFailedFragments
                self.stage = "正在写入并校验片段…"
            case "saved_fragment_evidence" where restoring:
                self.stage = "正在核验本机已保存的片段读回证据…"
            case "readback" where restoring: self.stage = "正在写入并校验片段…"
            case "restore_capture_retry":
                let attempt = event["retry"] as? Int ?? 0
                let limit = event["max_retries"] as? Int ?? 0
                let total = event["total_retries"] as? Int ?? 0
                self.stage = "读取当前状态中断：连续重连 \(attempt)/\(limit)；累计重连 \(total) 次…"
            case "restore_capture_retry_reset":
                let total = event["total_retries"] as? Int ?? 0
                self.stage = "当前状态新分段已核验：连续重连归零；累计重连 \(total) 次。"
            case "restore_capture_reconnect": self.stage = "正在重新连接以读取当前状态…"
            case "backup_retry":
                let attempt = event["retry"] as? Int ?? 0
                let limit = event["max_retries"] as? Int ?? 0
                let total = event["total_retries"] as? Int ?? 0
                self.stage = restoring ?
                    "读取当前状态中断：连续重连 \(attempt)/\(limit)；累计重连 \(total) 次…" :
                    attempt > 0 && limit > 0 ?
                    "备份读取中断：连续重连 \(attempt)/\(limit)；累计重连 \(total) 次…" :
                    "备份读取中断，正在重连…"
            case "backup_retry_reset":
                let total = event["total_retries"] as? Int ?? 0
                self.stage = restoring ?
                    "当前状态新分段已核验：连续重连归零；累计重连 \(total) 次。" :
                    "新分段已核验：连续重连归零；累计重连 \(total) 次。"
            case "backup_reconnect": self.stage = restoring ?
                "正在重新连接以读取当前状态…" : "正在等待相机重新连接…"
            case "restore_retry":
                let attempt = event["retry"] as? Int ?? 0
                let limit = event["max_retries"] as? Int ?? 0
                self.stage = attempt > 0 && limit > 0 ?
                    "恢复连接中断，正在重新连接并核对（第 \(attempt)/\(limit) 次）…" :
                    "恢复连接中断，正在重新连接并核对…"
            case "restore_reconnect": self.stage = "正在安全检查后重新进入恢复连接…"
            case "restore_reentry_retry":
                let attempt = event["retry"] as? Int ?? 0
                let limit = event["max_retries"] as? Int ?? 0
                self.stage = "相机未报告就绪，正在第 \(attempt)/\(limit) 次重新进入恢复模式…"
            case "mode": self.stage = "正在检查存储卡连接方式…"
            case "reboot": self.stage = "正在准备存储卡连接…"
            case "mount": self.stage = "正在等待存储卡挂载…"
            case "return_normal": self.stage = "正在返回正常模式…"
            default: self.stage = "正在执行任务…"
            }
            if let fraction = event["fraction"] as? Double, !name.hasPrefix("restore_nor_") {
                self.progressFraction = min(1, max(0, fraction))
            } else if ["manual_retry", "restore_read_session_exit", "restore_write_session_entry",
                       "restore_idle_probe", "restore_idle_reentry", "restore_capture_idle_reentry",
                       "bridge", "bridge_upload", "uart", "bridge_diagnostic",
                       "restore_reconnect", "restore_reentry_retry", "identity", "prewrite",
                       "readback"].contains(name) {
                self.progressFraction = nil
            }
        }, completion: { [weak self] outcome in
            guard let self else { return }
            if request["action"] as? String == "restore_run" {
                if let job = request["job_dir"] as? String { self.pinRestoreStartIfAvailable(job) }
                self.restoreFinishedAt = Date()
                if self.restoreNORStartedAt != nil { self.restoreNORFinishedAt = Date() }
            }
            self.busy = false
            self.progressFraction = nil
            self.stage = ""
            let action = request["action"] as? String ?? "unknown"
            let job = (request["job_dir"] ?? request["output_dir"] ?? request["prior_job_dir"]) as? String ?? "none"
            let exitStatus: String
            switch outcome {
            case .success(let result): exitStatus = (result["_worker_termination_status"] as? Int).map(String.init) ?? "unknown"
            case .failure(let error): exitStatus = ((error as NSError).userInfo["worker_termination_status"] as? Int).map(String.init) ?? "launch_or_protocol_error"
            }
            self.log("Worker completed: action=\(action); job=\(job); exit_status=\(exitStatus); busy=false")
            switch outcome {
            case .success(let result):
                self.log("Result: \(self.value(result, "status")); \(self.value(result, "summary"))")
                if action == "sd_card_prepare" {
                    let volume = result["volume_identity"] as? [String: Any] ?? [:]
                    let datMetadata = result["dat_metadata"] as? [String: Any] ?? [:]
                    let sourceBytes = (result["source_bytes"] as? Int).map(String.init) ?? "unknown"
                    let targetBytes = (result["target_bytes"] as? Int).map(String.init) ?? "unknown"
                    self.log("SD card copy receipt: status=\(self.value(result, "status")); receipt=\(self.value(result, "receipt_path")); source_path=\(self.value(result, "source_path")); source_sha256=\(self.value(result, "source_sha256")); source_bytes=\(sourceBytes); target_path=\(self.value(result, "target_path")); target_sha256=\(self.value(result, "target_sha256")); target_bytes=\(targetBytes); volume_device=\(self.value(volume, "device_identifier")); volume_mount=\(self.value(volume, "mount_point")); readback_verified=\((result["readback_verified"] as? Bool) == true); readback_utc=\(self.value(result, "readback_utc")); copy_replaced=\((result["copy_replaced"] as? Bool) == true); dat_metadata=\(self.value(datMetadata, "status")); slot_1_verified=\((result["sd_slot_1_presence_verified"] as? Bool) == true)")
                }
                if let cause = result["failure_cause"] as? String {
                    let confirmed = (result["confirmed_fragments"] as? Int).map(String.init) ?? "unknown"
                    let verified = (result["verified_fragments"] as? Int).map(String.init) ?? "unknown"
                    let failed = (result["failed_fragments"] as? Int).map(String.init) ?? "unknown"
                    let regions = (result["verified_regions"] as? [String]).map(String.init(describing:)) ?? "unknown"
                    let wrote = (result["write_may_have_occurred"] as? Bool).map(String.init) ?? "unknown"
                    self.log("Task failure: action=\(action); phase=\(self.value(result, "phase")); code=\(self.value(result, "error_code")); failed_check=\(self.value(result, "failed_check")); cause=\(cause); original_journal_error=\(self.value(result, "original_journal_error")); secondary_cause=\(self.value(result, "secondary_failure_cause")); camera_error_detail=\(self.value(result, "camera_error_detail")); exception_chain=\(self.value(result, "exception_chain")); pending_fragment=\(self.value(result, "pending_fragment")); confirmed_fragments=\(confirmed); verified_fragments=\(verified); failed_fragments=\(failed); verified_regions=\(regions); write_may_have_occurred=\(wrote); next_action=\(self.value(result, "next_action"))")
                }
                if ["connection_probe", "connection_enter_ram"].contains(request["action"] as? String ?? "") {
                    let control = result["control"] as? [String: Any] ?? [:]
                    let data = result["data"] as? [String: Any] ?? [:]
                    self.log("Connection details: control=\(self.value(control, "status")); control_message=\(self.value(control, "message")); data=\(self.value(data, "status")); data_message=\(self.value(data, "message")); next_action=\(self.value(result, "next_action"))")
                }
                if result["normal_mode_status"] != nil {
                    self.log("Normal mode: \(self.value(result, "normal_mode_status")); current_mode=\(self.value(result, "current_mode"))")
                }
                if request["action"] as? String == "connection_enter_ram" ||
                    (self.value(result, "normal_mode_status") == "READY" &&
                     (self.value(result, "current_mode") == "MSC" || self.value(result, "current_mode") == "NORMAL_USB")) {
                    self.invalidateCachedStockPlan()
                }
                onResult(result)
            case .failure(let error):
                self.stage = ""
                let details = (error as NSError).userInfo
                let normalStatus = details["normal_mode_status"] as? String ?? ""
                let currentMode = details["current_mode"] as? String ?? ""
                let normalModeError = details["normal_mode_error"] as? String ?? ""
                self.log("Worker failed: \(error.localizedDescription)")
                if let phase = details["phase"] as? String {
                    let confirmed = (details["confirmed_fragments"] as? Int).map(String.init) ?? "unknown"
                    let wrote = (details["write_may_have_occurred"] as? Bool).map(String.init) ?? "unknown"
                    self.log("Task error fields: action=\(action); phase=\(phase); code=\(details["error_code"] ?? ""); failed_check=\(details["failed_check"] ?? ""); cause=\(details["failure_cause"] ?? error.localizedDescription); confirmed_fragments=\(confirmed); write_may_have_occurred=\(wrote); next_action=\(details["next_action"] ?? "")")
                }
                if !normalStatus.isEmpty || !normalModeError.isEmpty {
                    self.log("Normal mode after worker error: status=\(normalStatus); current_mode=\(currentMode); error=\(normalModeError)")
                }
                if self.page == .board {
                    self.boardState = request["action"] as? String == "apply" ? "安装状态不明" : "待检查"
                    self.boardReadyToInstall = false
                    self.boardMessage = "操作未完成，请查看错误后重新检查转接头。"
                } else if self.page == .connection {
                    self.connectionMessage = "连接检查未完成，请查看错误并重新检查。"
                } else if request["action"] as? String == "sd_card_prepare" {
                    self.sdPreparationMessage = "存储卡准备未完成。请查看日志中的具体错误。"
                } else if request["action"] as? String == "prepare_card_connection" ||
                            ((request["action"] as? String == "confirm_stock_update_complete" ||
                              request["action"] as? String == "confirm_stock_update_failed") && self.sdStep == .source) {
                    self.sdPreparationMessage = "相机存储卡连接未完成。请查看日志并保持相机供电。"
                } else if ["stock_plan", "stock_run"].contains(request["action"] as? String ?? "") {
                    let details = (error as NSError).userInfo
                    self.taskMessage = details["phase"] != nil ?
                        stockCurrentFailureMessage(details) : stockPlannerFailureMessage(error as NSError)
                    self.stockNoWriteFailure = details["status"] as? String == "NO_WRITE" &&
                        details["write_may_have_occurred"] as? Bool == false &&
                        details["reboot_may_have_occurred"] as? Bool == false
                    self.showFreshStockPlanAction = self.stockNoWriteFailure
                } else if self.page == .restore {
                    let detail = error.localizedDescription
                    if detail.contains("Choose a verified snapshot folder or its immediate parent backup task folder") {
                        self.taskMessage = "所选文件夹不是已验证的备份。请选择 snapshot 文件夹或其备份任务文件夹。"
                    } else if detail.contains("Both selected folder and its snapshot child contain verified.json") {
                        self.taskMessage = "所选文件夹和其中的 snapshot 都含有备份记录。请直接选择 snapshot 文件夹。"
                    } else {
                        self.taskMessage = "任务未完成。具体错误已记入日志。"
                    }
                    if request["action"] as? String == "restore_run" {
                        self.restoreFailureObserved = true
                    }
                } else if self.page == .backup {
                    if self.backupStartedAt != nil && self.backupFinishedAt == nil {
                        self.backupFinishedAt = Date()
                    }
                    self.backupEvidenceState = "备份尚未完成；可在同一保存位置继续。"
                    self.taskMessage = "备份中断。请查看日志后重试，已完成的文件仍保存在当前任务目录。"
                } else {
                    self.taskMessage = "任务未完成。请查看日志中的具体错误，再决定下一步。"
                }
                self.errorText = self.page == .stock || self.page == .restore ?
                    self.taskMessage : "任务未完成。具体错误已记入日志。"
            }
        })
    }

    func select(_ page: Page) { if !busy { self.page = page } }

    func checkBoard() {
        guard !busy else { return }
        if boardReadyToInstall { showBoardInstall = true; return }
        boardState = "正在检查"
        boardMessage = ""
        showBootSteps = false
        if let boardJob {
            reconcileBoard(boardJob)
        } else {
            let job = newJob("board_install")
            run(["action": "prepare", "kind": "board_install", "inputs": [:], "output_dir": job]) { result in
                guard self.value(result, "status") == "READY" else {
                    self.boardState = "待检查"
                    self.boardMessage = "准备未完成，请查看提示后重新检查。"
                    return
                }
                self.boardJob = job
                self.reconcileBoard(job)
            }
        }
    }

    private func reconcileBoard(_ job: String) {
        run(["action": "reconcile", "job_dir": job]) { result in self.showBoardResult(result) }
    }

    private func showBoardResult(_ result: [String: Any]) {
        let status = value(result, "status")
        let summary = value(result, "summary")
        boardReadyToInstall = false
        showBootSteps = false
        if (status == "ALREADY_INSTALLED" || status == "VERIFIED") && result["control_ping_verified"] as? Bool == true {
            boardState = "已安装"
            boardMessage = "转接头已通过运行态应答验证。"
            controlVerified = true
        } else if status == "READY" {
            boardState = "未安装"
            boardReadyToInstall = true
            boardMessage = "已检测到 BOOTSEL。点击“安装配套程序”并确认。"
        } else if summary.contains("did not answer known bridge PING") {
            boardState = "未安装"
            showBootSteps = true
            boardMessage = "已检测到 RP2040，但配套程序尚未安装。"
        } else if summary.contains("Multiple") || summary.contains("both found") {
            boardState = "待检查"
            boardMessage = "检测到多个候选转接头。请只连接本次要准备的 RP2040。"
        } else {
            boardState = "待检查"
            showBootSteps = result["next_action"] as? String == "Connect the RP2040 in BOOTSEL mode"
            boardMessage = "未确认转接头状态。请连接 USB 1 后重新检查。"
        }
    }

    func installBoard() {
        guard boardReadyToInstall, let boardJob else { return }
        boardState = "安装中"
        boardMessage = ""
        run(["action": "apply", "job_dir": boardJob]) { result in self.showBoardResult(result) }
    }

    func checkConnection() {
        guard !busy else { return }
        controlVerified = false
        dataVerified = false
        controlState = "正在检查"
        dataState = "正在检查"
        currentModeState = "正在检查"
        connectionMessage = ""
        run(["action": "connection_enter_ram", "confirmed": true, "camera_model": connectionCameraModel]) { result in
            let control = result["control"] as? [String: Any] ?? [:]
            let data = result["data"] as? [String: Any] ?? [:]
            let controlStatus = self.value(control, "status")
            let dataStatus = self.value(data, "status")
            self.controlVerified = controlStatus == "READY"
            self.dataVerified = dataStatus == "READY"
            self.controlState = self.controlVerified ? "本次 PING 已验证" :
                controlStatus == "BOOTSEL" ? "BOOTSEL，尚未安装" : "未验证"
            self.dataState = self.dataVerified ? "本次恢复模式读出已验证 · \(self.value(data, "product"))" :
                dataStatus == "MSC" || dataStatus == "NORMAL_USB" ? "恢复通路未验证；另检测到相机普通 USB 模式" :
                "尚未检测到相机恢复模式 USB"
            let failure = self.value(result, "ram_entry_stage")
            let normalStatus = self.value(result, "normal_mode_status")
            let currentMode = self.value(result, "current_mode")
            let normalReady = normalStatus == "READY" && (currentMode == "MSC" || currentMode == "NORMAL_USB")
            self.currentModeState = normalReady ?
                "普通 USB 模式（当前已观察）" :
                normalStatus.isEmpty ? "尚未检查" : "未确认返回普通模式"
            self.connectionMessage = self.controlVerified && self.dataVerified && normalReady ?
                "两路连接本次已验证；相机已返回普通模式。确认屏幕没有正在进行的更新后，可以拔线。" :
                self.controlVerified && self.dataVerified && normalStatus == "REBOOT_UNCERTAIN" ?
                "两路连接本次已验证；返回普通模式状态不明。请勿断电或自动重试，查看相机屏幕和日志。" :
                self.controlVerified && self.dataVerified && normalStatus == "NEEDS_USER_ACTION" ?
                "两路连接本次已验证；尚未确认相机返回普通模式。请查看屏幕，暂勿拔线。" :
                self.controlVerified && self.dataVerified && normalStatus == "NO_WRITE" ?
                "两路连接本次已验证；安全检查阻止返回普通模式。请查看日志，暂勿拔线或重试。" :
                self.controlVerified && self.dataVerified && normalStatus == "READY" ?
                "两路连接本次已验证；当前模式信息不完整。请查看日志，暂勿拔线。" :
                self.controlVerified && self.dataVerified ? "两路连接本次已验证。" :
                failure == "UART_NOT_READY" ? "相机未报告恢复模式就绪。检查电源与 Multi 插头。" :
                failure == "FASTBOOT_USB_MISSING" ? "工程端已就绪，但 USB 2 未出现。检查 USB 2 的 USB-C 连接。" :
                !self.controlVerified ? "先准备并验证 Multi 转接头。" : "恢复模式 DATA 通路尚未验证。"
        }
    }

    func probeControlForTask() {
        run(["action": "connection_probe", "camera_model": connectionCameraModel]) { result in
            let control = result["control"] as? [String: Any] ?? [:]
            self.controlVerified = self.value(control, "status") == "READY"
            self.controlState = self.controlVerified ? "PING 已验证" : "未验证"
            self.taskMessage = self.controlVerified ?
                "Multi 转接头本次 PING 已验证。相机恢复 DATA 未在此步骤验证；恢复任务会单独检查。" :
                self.value(control, "status") == "BOOTSEL" ?
                "Multi 转接头处于 BOOTSEL，尚未验证运行态。请先到“准备 Multi 转接头”完成安装。" :
                "请先准备转接头。"
        }
    }

    func startStock() {
        guard selectedSDCamera.supportsAdapterRecovery else {
            taskMessage = "此机型的转接头恢复尚不支持。"
            return
        }
        guard stockModelConfirmed && stockCardConfirmed && stockPowerConfirmed else {
            taskMessage = "请确认机型、供电和卡槽 1 的 BODYDATA.DAT。"
            return
        }
        guard controlVerified else { taskMessage = "请先检查并验证 Multi 转接头。"; return }
        sdBlockingStockJob = nil // A fresh stock task does not inherit an old task's outcome controls.
        if stockNoWriteFailure {
            invalidateCachedStockPlan()
            stockNoWriteFailure = false
        }
        showFreshStockPlanAction = false
        if let stockJob {
            planStock(stockJob)
        } else {
            let job = newJob("stock_sd_recovery")
            var inputs: [String: Any] = ["sd_ready": true, "camera_model": sdCameraModel]
            if let sdPreparationReceipt { inputs["sd_preparation_receipt"] = sdPreparationReceipt }
            run(["action": "prepare", "kind": "stock_sd_recovery",
                 "inputs": inputs, "output_dir": job]) { result in
                guard self.value(result, "status") == "READY" else {
                    self.taskMessage = self.taskResultText(result)
                    return
                }
                self.stockJob = job
                self.planStock(job)
            }
        }
    }

    func choosePreparedSDCard() {
        guard !busy, selectedSDCamera.supportsAdapterRecovery else { return }
        stockCardConfirmed = true
        sdArrivedFromHelper = false
        sdCopyProofText = ""
        sdPreparationReceipt = nil
        sdStep = .ready
    }

    func beginSDHelper(inCamera: Bool = false) {
        guard !busy else { return }
        stockCardConfirmed = false
        sdInCameraUpdate = inCamera || !selectedSDCamera.supportsAdapterRecovery
        sdUsingCameraRoute = false
        sdCameraCandidates = []
        sdVolume = nil
        sdSourceDAT = nil
        sdPreparationMessage = ""
        sdPackageLabel = ""
        sdArrivedFromHelper = true
        sdCopyProofText = ""
        sdPreparationReceipt = nil
        sdStep = .source
    }

    func backSDStep() {
        guard !busy else { return }
        switch sdStep {
        case .entry: break
        case .source: sdStep = .entry
        case .target: sdStep = .source
        case .officialUpdate: sdStep = sdArrivedFromHelper ? .target : .entry
        case .slot: sdStep = .target
        case .ready: sdStep = sdArrivedFromHelper ? .slot : .entry
        }
    }

    func selectSDSource(_ url: URL) {
        sdSourceDAT = url
        stockCardConfirmed = false
        sdCopyProofText = ""
        sdPreparationReceipt = nil
        sdExistingHash = nil
        sdVolumeDevice = nil
        sdPreparationMessage = ""
    }

    func selectSDVolume(_ url: URL) {
        sdVolume = url
        stockCardConfirmed = false
        sdCopyProofText = ""
        sdPreparationReceipt = nil
        sdExistingHash = nil
        sdVolumeDevice = nil
        sdCameraVolumeConfirmed = false
        sdPreparationMessage = "已选择目标路径。写入前将核验它是否为可用存储卡。"
        sdStep = .target
    }

    func selectComputerSDVolume(_ url: URL) {
        sdUsingCameraRoute = false
        selectSDVolume(url)
    }

    func selectCameraSDVolume(_ url: URL) {
        sdUsingCameraRoute = true
        selectSDVolume(url)
    }

    func prepareCameraCardConnection() {
        guard !busy else { return }
        sdUsingCameraRoute = true
        sdBlockingStockJob = nil
        sdCameraCandidates = []
        sdPreparationMessage = "正在准备存储卡连接…"
        run(["action": "prepare_card_connection", "route": "camera", "camera_model": connectionCameraModel]) { result in
            switch self.value(result, "status") {
            case "READY":
                let volumes = (result["volumes"] as? [[String: Any]] ?? [])
                    .compactMap { $0["path"] as? String }
                guard !volumes.isEmpty else {
                    self.sdPreparationMessage = "尚未找到已挂载的存储卡。请检查连接后重试。"
                    return
                }
                self.sdCameraCandidates = volumes
                self.invalidateCachedStockPlan()
                self.sdPreparationMessage = "已找到挂载的存储卷。请选择并确认属于相机的存储卡。"
                self.requestSDVolumePicker = true
            case "NO_WRITE":
                self.capturePriorStockBlocker(result)
                self.sdPreparationMessage = self.sdBlockingStockJob == nil ?
                    "已有未完成任务，暂不能切换相机模式。请保持连接并查看日志。" :
                    "先前更新尚未确认完成。请保持供电和连接，等相机屏幕显示“更新完成”后，再点下方按钮。"
            case "REBOOT_UNCERTAIN":
                self.sdPreparationMessage = "相机模式切换结果不明。请保持连接，查看相机屏幕和日志；不要自动重试。"
            default:
                self.sdPreparationMessage = "相机存储卡尚未挂载。请确认相机已开机并连接 USB 2，或选择电脑已挂载的卡。"
            }
        }
    }

    func confirmPreviousStockUpdateComplete() {
        guard let sdBlockingStockJob, !busy else { return }
        run(["action": "confirm_stock_update_complete", "job_dir": sdBlockingStockJob,
             "confirmed": true]) { result in
            if self.value(result, "status") == "VERIFIED" &&
                result["camera_update_observed_by_user"] as? Bool == true {
                self.sdBlockingStockJob = nil
                self.invalidateCachedStockPlan()
                self.requireFreshStockReview = true
                self.showFreshStockPlanAction = true
                if self.sdStep == .ready {
                    self.taskMessage = "已记录前次相机更新完成。请再次点击“开始引导恢复”核对本次方案。"
                } else {
                    self.sdPreparationMessage = "已记录相机屏幕显示更新完成。请重新点击“选择相机挂载的存储卡”。"
                }
            } else {
                self.sdPreparationMessage = "尚未记录更新完成。请查看日志并保持供电。"
            }
        }
    }

    func confirmPreviousStockUpdateFailed() {
        guard let sdBlockingStockJob, !busy else { return }
        run(["action": "confirm_stock_update_failed", "job_dir": sdBlockingStockJob,
             "confirmed": true, "reason": priorUpdateFailureReason]) { result in
            if self.value(result, "status") == "FAILURE_RECORDED" {
                self.sdBlockingStockJob = nil
                self.invalidateCachedStockPlan()
                self.requireFreshStockReview = true
                self.showFreshStockPlanAction = true
                if self.sdStep == .ready {
                    self.taskMessage = "已记录前次更新失败。请再次点击“开始引导恢复”核对本次方案。"
                } else {
                    self.sdPreparationMessage = "已记录上次更新失败。请重新点击“选择相机挂载的存储卡”检查连接。"
                }
            } else {
                self.sdPreparationMessage = "尚未记录上次更新失败。请保持连接并查看日志。"
            }
        }
    }

    func prepareSDCard(confirmReplace: Bool = false) {
        guard let sdSourceDAT, let sdVolume else {
            sdPreparationMessage = "请选择要复制的 DAT 文件和已挂载的存储卡。"
            return
        }
        guard !sdUsingCameraRoute || sdCameraVolumeConfirmed else {
            sdPreparationMessage = "请确认所选存储卷确实属于相机存储卡。"
            return
        }
        var request: [String: Any] = ["action": "sd_card_prepare",
                                      "source_dat": sdSourceDAT.path,
                                      "camera_model": sdCameraModel,
                                      "volume": sdVolume.path,
                                      "confirm_replace": confirmReplace]
        if confirmReplace {
            guard let sdExistingHash, let sdVolumeDevice else {
                sdPreparationMessage = "卡片状态已变化，请重新检查后再确认。"
                return
            }
            request["expected_existing_sha256"] = sdExistingHash
            request["expected_volume_device_identifier"] = sdVolumeDevice
        }
        sdPreparationMessage = "正在校验所选 DAT 的复制完整性…"
        run(request) { result in
            let copied = self.value(result, "status") == "VERIFIED" ||
                self.value(result, "status") == "ALREADY_PRESENT"
            if copied {
                let sourceHash = self.value(result, "source_sha256")
                let targetHash = self.value(result, "target_sha256")
                let sourceBytes = result["source_bytes"] as? Int
                let targetBytes = result["target_bytes"] as? Int
                let volume = result["volume_identity"] as? [String: Any] ?? [:]
                guard !sourceHash.isEmpty, sourceHash == targetHash,
                      sourceBytes != nil, sourceBytes == targetBytes,
                      !(self.value(result, "receipt_path")).isEmpty,
                      !(self.value(volume, "device_identifier")).isEmpty,
                      result["readback_verified"] as? Bool == true,
                      !(self.value(result, "readback_utc")).isEmpty else {
                    self.sdCopyProofText = ""
                    self.sdPreparationReceipt = nil
                    self.sdPreparationMessage = "存储卡复制回执不完整。请查看日志并重新检查卡片。"
                    return
                }
                self.sdPreparationReceipt = self.value(result, "receipt_path")
                self.sdCopyProofText = "复制回执已保存；源文件与卡上文件的字节数和摘要一致。"
            }
            let metadata = result["dat_metadata"] as? [String: Any] ?? [:]
            if let name = metadata["model"] as? String, let version = metadata["version"] as? String {
                self.sdPackageLabel = "\(name) · \(version)"
            } else {
                self.sdPackageLabel = ""
            }
            switch self.value(result, "status") {
            case "MODEL_MISMATCH":
                self.sdPreparationMessage = "固件包适用于其他机型，请返回选择与相机匹配的 DAT。"
            case "NEEDS_CONFIRMATION":
                self.sdExistingHash = self.value(result, "existing_sha256")
                self.sdVolumeDevice = self.value(result, "volume_device_identifier")
                if self.sdExistingHash?.isEmpty == false && self.sdVolumeDevice?.isEmpty == false {
                    self.showSDReplacement = true
                    self.sdPreparationMessage = "卡片根目录已有不同的 BODYDATA.DAT，请确认是否替换。"
                } else {
                    self.sdPreparationMessage = "无法确认原文件和存储卡身份，请重新检查。"
                }
            case "VERIFIED":
                if !self.sdInCameraUpdate { self.invalidateCachedStockPlan() }
                self.sdPreparationMessage = self.sdInCameraUpdate ? "固件已写卡并复核，请按下方机内步骤操作。" : self.sdUsingCameraRoute ?
                    "所选 DAT 已写入并复核。请安全推出存储卷，确认卡仍在相机卡槽 1。" :
                    "所选 DAT 已写入并复核。请安全推出卡片，插入相机卡槽 1。"
                self.sdStep = self.sdInCameraUpdate ? .officialUpdate : .slot
            case "ALREADY_PRESENT":
                if !self.sdInCameraUpdate { self.invalidateCachedStockPlan() }
                self.sdPreparationMessage = self.sdInCameraUpdate ? "卡上已有相同的固件，请按下方机内步骤操作。" : self.sdUsingCameraRoute ?
                    "所选存储卡已有相同的 DAT。请安全推出存储卷，确认卡仍在相机卡槽 1。" :
                    "所选存储卡已有相同的 DAT。请安全推出卡片，插入相机卡槽 1。"
                self.sdStep = self.sdInCameraUpdate ? .officialUpdate : .slot
            default:
                self.sdPreparationMessage = "存储卡尚未准备好。请查看日志中的具体原因；未确认替换前不会覆盖原文件。"
            }
        }
    }

    private func planStock(_ job: String) {
        run(["action": "stock_plan", "job_dir": job]) { result in
            guard self.value(result, "status") == "READY" else {
                self.setStockWaitingState(result)
                self.stockNoWriteFailure = self.value(result, "status") == "NO_WRITE" &&
                    result["write_may_have_occurred"] as? Bool != true &&
                    result["reboot_may_have_occurred"] as? Bool != true
                self.showFreshStockPlanAction = self.stockNoWriteFailure
                self.taskMessage = self.showStockCompletionButton ?
                    "等待相机屏幕显示更新完成。" :
                    result["phase"] != nil ? stockCurrentFailureMessage(result) : self.taskResultText(result)
                return
            }
            self.stockPlanDigest = result["plan_digest"] as? String
            self.stockNoWriteFailure = false
            self.log("Stock recovery plan: \(self.value(result, "summary")); plan_digest=\(self.stockPlanDigest ?? "missing")")
            if self.requireFreshStockReview ||
                (result["requires_confirmation"] as? Bool == true && result["confirmation_recorded"] as? Bool != true) {
                self.showStockConfirmation = true
            } else {
                self.runStock()
            }
        }
    }

    func runStock() {
        guard let stockJob else { return }
        requireFreshStockReview = false
        var request: [String: Any] = ["action": "stock_run", "job_dir": stockJob]
        if let stockPlanDigest { request["confirmed_plan_digest"] = stockPlanDigest }
        run(request) { result in
            if self.value(result, "status") == "NO_WRITE" && result["stale_plan"] as? Bool == true {
                self.invalidateCachedStockPlan()
                self.stockNoWriteFailure = true
                self.requireFreshStockReview = true
                self.showFreshStockPlanAction = true
                self.taskMessage = "相机内容在前次读出后已变化，本次未写入。请点击“重新读取并制定方案”创建新任务。"
                return
            }
            self.setStockWaitingState(result)
            self.stockNoWriteFailure = self.value(result, "status") == "NO_WRITE" &&
                result["write_may_have_occurred"] as? Bool == false &&
                result["reboot_may_have_occurred"] as? Bool == false
            self.showFreshStockPlanAction = self.stockNoWriteFailure
            self.taskMessage = self.showStockCompletionButton ?
                "等待相机屏幕显示更新完成。" :
                result["phase"] != nil ? stockCurrentFailureMessage(result) : self.taskResultText(result)
        }
    }

    func confirmStockUpdateComplete() {
        guard showStockCompletionButton, let stockJob else { return }
        run(["action": "confirm_stock_update_complete", "job_dir": stockJob, "confirmed": true]) { result in
            if self.value(result, "status") == "VERIFIED" &&
                result["camera_update_observed_by_user"] as? Bool == true {
                self.showStockCompletionButton = false
                self.stockRebootAcknowledgedAt = nil
                self.invalidateCachedStockPlan()
                self.stockNoWriteFailure = false
                self.sdBlockingStockJob = nil
                self.sdPreparationReceipt = nil
                self.sdCopyProofText = ""
                self.requireFreshStockReview = false
                self.showFreshStockPlanAction = false
                self.taskMessage = "已记录你在相机屏幕看到更新完成。如需再次恢复，请点击“开始引导恢复”建立新任务。"
            } else {
                self.taskMessage = "尚未记录更新完成。请查看日志并保持相机供电。"
            }
        }
    }

    func startBackup() {
        guard !busy else { return }
        guard powerConfirmed && spaceConfirmed else { taskMessage = "请确认相机供电和保存空间。"; return }
        guard controlVerified else { taskMessage = "请先验证 Multi 转接头。"; return }
        guard let outputDirectory else { taskMessage = "请选择备份保存位置。"; return }
        backupStartedAt = Date()
        backupFinishedAt = nil
        backupVerified = false
        if let backupJob { reconcileBackup(backupJob); return }
        let job = newJob("full_backup", in: outputDirectory)
        taskMessage = ""
        backupEvidenceState = "正在建立备份任务…"
        backupModeState = ""
        run(["action": "prepare", "kind": "full_backup", "inputs": [:], "output_dir": job]) { result in
            guard self.value(result, "status") == "READY" else {
                self.backupFinishedAt = Date()
                self.backupEvidenceState = "备份尚未开始。"
                self.taskMessage = self.taskResultText(result)
                return
            }
            self.backupJob = job
            self.backupJobPath = job
            self.reconcileBackup(job)
        }
    }

    private func reconcileBackup(_ job: String) {
        backupEvidenceState = "正在备份并核验…"
        backupModeState = ""
        run(["action": "reconcile", "job_dir": job]) { result in
            guard self.value(result, "status") == "READY" else {
                self.backupFinishedAt = Date()
                self.backupEvidenceState = "备份尚未完成；可在同一保存位置继续。"
                self.taskMessage = self.backupIncompleteMessage(result)
                return
            }
            self.run(["action": "apply", "job_dir": job]) { result in
                guard self.value(result, "status") == "VERIFIED" else {
                    self.backupFinishedAt = Date()
                    self.backupEvidenceState = "备份尚未完成；可在同一保存位置继续。"
                    self.taskMessage = self.backupIncompleteMessage(result)
                    return
                }
                if result["normal_mode_status"] != nil {
                    self.backupModeState = self.backupCurrentModeText(result)
                }
                let retryText = self.backupRetryText(result)
                self.run(["action": "verify", "job_dir": job]) { result in
                    self.backupFinishedAt = Date()
                    self.backupVerified = self.value(result, "status") == "VERIFIED"
                    if self.backupVerified {
                        self.backupEvidenceState = "备份文件与收据已验证。"
                        if result["normal_mode_status"] != nil {
                            self.backupModeState = self.backupCurrentModeText(result)
                        } else if self.backupModeState.isEmpty {
                            self.backupModeState = "相机当前模式尚未验证；请查看屏幕与日志。"
                        }
                        self.backupJob = nil
                        self.taskMessage = retryText + "再次点击“开始备份”将建立新的备份。"
                    } else {
                        self.backupEvidenceState = "备份核验尚未完成；可在同一保存位置继续。"
                        self.taskMessage = self.taskResultText(result)
                    }
                }
            }
        }
    }

    private func backupCurrentModeText(_ result: [String: Any]) -> String {
        let status = value(result, "normal_mode_status")
        let mode = value(result, "current_mode")
        switch status {
        case "READY" where mode == "MSC" || mode == "NORMAL_USB":
            return "相机已返回普通 USB 模式。"
        case "REBOOT_UNCERTAIN":
            return "相机返回普通模式的状态不明；请保持连接并查看屏幕与日志。"
        case "NO_WRITE":
            return "安全检查阻止返回普通模式；请保持连接并查看日志。"
        case "NEEDS_USER_ACTION":
            return "尚未确认相机返回普通模式；请保持连接并查看屏幕与日志。"
        default:
            return "相机当前模式尚未验证；请查看屏幕与日志。"
        }
    }

    func selectBackupDestination(_ directory: URL) {
        guard !busy else { return }
        let selected = directory.standardizedFileURL
        let taskFile = selected.appendingPathComponent("task.json")
        if FileManager.default.fileExists(atPath: taskFile.path) {
            guard let data = try? Data(contentsOf: taskFile),
                  let task = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  task["kind"] as? String == "full_backup" else {
                errorText = "所选目录不是有效的相机备份任务。请选择保存位置或已有备份任务目录。"
                return
            }
            clearBackupTiming()
            outputDirectory = selected.deletingLastPathComponent()
            backupJob = selected.path
            backupJobPath = selected.path
            backupEvidenceState = "已选择已有备份任务，等待继续核验。"
            backupModeState = ""
            taskMessage = "点击“开始备份”将继续此任务；原文件保留在所示目录。"
            return
        }
        if selected.lastPathComponent.hasPrefix("full_backup-") {
            errorText = "该目录没有有效的备份任务记录。请选择保存位置或已有备份任务目录。"
            return
        }
        if let current = outputDirectory,
           backupDestinationPath(current) == backupDestinationPath(selected) { return }
        clearBackupTiming()
        outputDirectory = selected
        if let backupJobPath { previousBackupJobs.append(backupJobPath) }
        backupJob = nil
        backupJobPath = nil
        backupEvidenceState = "尚未在此位置备份。"
        backupModeState = ""
        taskMessage = "下次点击“开始备份”会在所选位置建立新任务；原备份文件保持不变。"
    }

    private func clearBackupTiming() {
        backupStartedAt = nil
        backupFinishedAt = nil
        backupVerified = false
    }

#if NATIVE_WORKER_TEST
    func testSeedBackupJob(_ path: String) { backupJob = path; backupJobPath = path }
    var testBackupJob: String? { backupJob }
    var testRestoreJob: String? { restoreJob }
#endif

    func startRestore() {
        guard powerConfirmed else { taskMessage = "请确认相机供电。"; return }
        guard controlVerified else { taskMessage = "请先验证 Multi 转接头。"; return }
        guard let restoreTarget else { taskMessage = "请选择要恢复的备份。"; return }
        restoreNORStartedAt = nil
        restoreNORFinishedAt = nil
        restoreNORStatus = ""
        restoreFullyVerified = false
        restoreHasSkippedRegions = false
        restoreHasPartialOutcome = false
        restoreSkippedLabel = nil
        restoreJob = nil
        restoreStartedAt = nil
        restoreFinishedAt = nil
        restoreStartPinned = false
        restoreFailureObserved = false
        restorePlannedFragments = nil
        restoreVerifiedFragments = 0
        restoreFailedFragments = 0
        let job = newJob("scoped_restore", in: restoreTarget.deletingLastPathComponent())
        run(["action": "prepare", "kind": "scoped_restore",
             "inputs": ["target_snapshot": restoreTarget.path,
                        "restore_profile": "capability"], "output_dir": job]) { result in
            guard self.value(result, "status") == "READY" else { self.taskMessage = self.taskResultText(result); return }
            self.restoreJob = job
            self.runRestore(job)
        }
    }

    private func runRestore(_ job: String) {
        guard !busy else { return }
        let savedStart = savedRestoreStart(job)
        restoreStartedAt = savedStart ?? Date()
        restoreStartPinned = savedStart != nil
        restoreFinishedAt = nil
        run(["action": "restore_run", "job_dir": job]) { result in
            if let planned = result["planned_fragments"] as? Int {
                self.restorePlannedFragments = planned
            }
            if let verified = result["verified_fragments"] as? Int {
                self.restoreVerifiedFragments = verified
            }
            if let failed = result["failed_fragments"] as? Int {
                self.restoreFailedFragments = failed
            }
            let restoreStatus = self.value(result, "status")
            self.restoreNORStatus = self.value(result, "nor_status")
            self.restoreFullyVerified = restoreStatus == "VERIFIED" && self.restoreNORStatus == "VERIFIED"
            self.restoreFailureObserved = restoreStatus != "VERIFIED" && restoreStatus != "PARTIAL"
            let warning = restoreSkippedText(result)
            self.restoreHasSkippedRegions = warning != nil
            self.restoreSkippedLabel = warning
            self.restoreHasPartialOutcome = restoreStatus == "PARTIAL"
            if restoreStatus == "PARTIAL" {
                let failed = result["failed_fragments"] as? Int ?? 0
                self.taskMessage = failed > 0 ?
                    "恢复流程已完成，\(failed)处写入校验未通过" :
                    "没有可写的恢复片段；请查看跳过区域"
            } else if self.restoreNORStatus == "UNSUPPORTED" {
                self.taskMessage = "本次未执行启动数据重建（NOR）；当前配置尚不支持。" +
                    (warning.map { "\n" + $0 } ?? "")
            } else if self.restoreFullyVerified {
                self.taskMessage = "存储恢复与启动数据重建已验证，相机已返回普通模式。请检查取景与菜单是否正常。"
            } else {
                self.taskMessage = warning ?? self.restoreIncompleteMessage(result)
            }
        }
    }

    func selectRestoreTarget(_ target: URL) {
        guard !busy else { return }
        let selected = target.standardizedFileURL
        let taskFile = selected.appendingPathComponent("task.json")
        if FileManager.default.fileExists(atPath: taskFile.path) {
            guard let data = try? Data(contentsOf: taskFile),
                  let task = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let kind = task["kind"] as? String else {
                errorText = "所选目录不是有效的恢复任务。请选择 snapshot 或备份任务文件夹。"
                return
            }
            if kind == "scoped_restore" {
                errorText = "旧恢复任务不能继续。请选择已验证的 snapshot 或备份任务文件夹，建立新的恢复任务。"
                return
            }
            if kind != "full_backup" {
                errorText = "所选目录不是有效的恢复任务。请选择 snapshot 或备份任务文件夹。"
                return
            }
        }
        if selected.lastPathComponent.hasPrefix("scoped_restore-") {
            errorText = "所选目录不是有效的恢复任务。请选择 snapshot 或备份任务文件夹。"
            return
        }
        restoreTarget = selected
        restoreJob = nil
        restorePlannedFragments = nil
        restoreVerifiedFragments = 0
        restoreFailedFragments = 0
        restoreStartedAt = nil
        restoreFinishedAt = nil
        restoreStartPinned = false
        restoreHasSkippedRegions = false
        restoreHasPartialOutcome = false
        restoreSkippedLabel = nil
        restoreFailureObserved = false
        restoreNORStartedAt = nil
        restoreNORFinishedAt = nil
        restoreNORStatus = ""
        restoreFullyVerified = false
        taskMessage = ""
    }

    func resume(_ directory: URL) {
        let task = directory.appendingPathComponent("task.json")
        guard let data = try? Data(contentsOf: task),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let kind = json["kind"] as? String else { errorText = "请选择有效的任务目录。"; return }
        switch kind {
        case "board_install": page = .board; boardJob = directory.path; reconcileBoard(directory.path)
        case "stock_sd_recovery": page = .stock; sdStep = .ready; stockCardConfirmed = true; stockJob = directory.path; planStock(directory.path)
        case "full_backup":
            page = .backup
            outputDirectory = directory.deletingLastPathComponent()
            backupJob = directory.path
            backupJobPath = directory.path
            reconcileBackup(directory.path)
        case "scoped_restore":
            errorText = "旧恢复任务不能继续。请选择已验证的 snapshot 新建恢复任务。"
        default: errorText = "不支持该任务类型。"
        }
    }

    func exportLogs(_ destination: URL) {
        guard !busy else { return }
        busy = true
        stage = "正在导出日志…"
        let source = logDirectory
        let tasks = ([boardJob, stockJob, backupJob, backupJobPath, restoreJob].compactMap { $0 } +
                     previousBackupJobs).map { URL(fileURLWithPath: $0) }
        DispatchQueue.global(qos: .utility).async {
            let manager = FileManager.default
            let staging = manager.temporaryDirectory.appendingPathComponent("Camera-Recovery-Logs-\(UUID().uuidString)")
            defer { try? manager.removeItem(at: staging) }
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
            let outcome: String?
            do {
                try stageSupportLogs(from: source, into: staging, taskDirectories: tasks)
                process.arguments = ["-c", "-k", "--keepParent", staging.path, destination.path]
                try process.run()
                process.waitUntilExit()
                outcome = process.terminationStatus == 0 ? nil : "导出失败"
            }
            catch { outcome = error.localizedDescription }
            DispatchQueue.main.async {
                self.busy = false
                self.stage = outcome == nil ? "日志已导出" : "导出失败"
                if let outcome { self.errorText = outcome }
                else { self.log("Exported native app logs to \(destination.path).") }
            }
        }
    }
}

private struct CableIllustration: View {
    let firstRun: Bool
    @AppStorage(AppLanguage.preference) private var language = AppLanguage.chinese
    private var name: String {
        (firstRun ? "compact-prep" : "compact-full") +
            (language == AppLanguage.english ? "-en" : "")
    }
    private var image: NSImage? {
        let packaged = Bundle.main.resourceURL?.appendingPathComponent("artwork/\(name).png")
        let source = ProcessInfo.processInfo.environment["CAMERA_RECOVERY_SOURCE_ROOT"]
            .map { URL(fileURLWithPath: $0).appendingPathComponent("docs/design/wiring-options/\(name).png") }
        return [packaged, source].compactMap { $0 }.compactMap { NSImage(contentsOf: $0) }.first
    }
    var body: some View {
        Group {
            if let image {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fit)
                    .accessibilityLabel(t(firstRun ? "只将 USB 1 RP2040 接到电脑，Multi 和 USB 2 暂不连接" : "Multi 接相机，USB 1 和 USB 2 接电脑"))
            } else {
                Text(t("连接示意图暂不可用，请按照下方文字连接。"))
                    .foregroundStyle(.secondary)
            }
        }
        .frame(width: 560, height: 136)
        .background(Color.white, in: RoundedRectangle(cornerRadius: 12))
    }
}

// Keep the log in the native text system. A long session can make one SwiftUI
// Text more than 100,000 points tall; a separate scroll marker then supplies no
// guarantee that the visible viewport contains rendered text.
private final class ActivityLogScrollView: NSScrollView {
    let logTextView = NSTextView(frame: .zero)
    private(set) var followsLatest = true
    private var laidOutWidth: CGFloat = -1

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        hasVerticalScroller = true
        hasHorizontalScroller = false
        autohidesScrollers = true
        borderType = .noBorder
        drawsBackground = true
        backgroundColor = .textBackgroundColor
        logTextView.isEditable = false
        logTextView.isSelectable = true
        logTextView.isRichText = false
        logTextView.allowsUndo = false
        logTextView.usesFindBar = true
        logTextView.font = .monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
        logTextView.textColor = .textColor
        logTextView.backgroundColor = .textBackgroundColor
        logTextView.textContainerInset = NSSize(width: 4, height: 4)
        logTextView.isVerticallyResizable = true
        logTextView.isHorizontallyResizable = false
        logTextView.autoresizingMask = [.width]
        logTextView.minSize = .zero
        logTextView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        logTextView.textContainer?.widthTracksTextView = true
        logTextView.textContainer?.containerSize.height = .greatestFiniteMagnitude
        documentView = logTextView
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    func update(text: String, followLatest: Bool) {
        let origin = contentView.bounds.origin
        let selection = logTextView.selectedRanges
        let old = logTextView.string
        followsLatest = followLatest
        logTextView.setAccessibilityLabel(t("运行日志"))
        if old != text {
            // Appending preserves selection and avoids replacing the entire
            // document on every progress event. No log lines are discarded.
            if text.hasPrefix(old) {
                let suffix = String(text.dropFirst(old.count))
                let attributes: [NSAttributedString.Key: Any] = [
                    .font: logTextView.font!, .foregroundColor: NSColor.textColor]
                logTextView.textStorage?.append(NSAttributedString(string: suffix, attributes: attributes))
            } else {
                logTextView.string = text
            }
            let length = (text as NSString).length
            logTextView.selectedRanges = selection.map {
                let range = $0.rangeValue
                let start = min(range.location, length)
                return NSValue(range: NSRange(location: start, length: min(range.length, length - start)))
            }
        }
        // SwiftUI first mounts representable views at zero width. Do not lay
        // out a whole session one character per line before its viewport exists.
        guard contentSize.width > 1 else { return }
        layoutText()
        if followsLatest {
            scrollToLatest()
        } else {
            contentView.scroll(to: origin)
            reflectScrolledClipView(contentView)
        }
    }

    override func layout() {
        super.layout()
        if contentSize.width > 1 && abs(laidOutWidth - contentSize.width) > 0.5 {
            layoutText()
            if followsLatest { scrollToLatest() }
        }
    }

    private func layoutText() {
        laidOutWidth = contentSize.width
        logTextView.setFrameSize(NSSize(width: max(1, contentSize.width), height: logTextView.frame.height))
        if let container = logTextView.textContainer, let manager = logTextView.layoutManager {
            manager.ensureLayout(for: container)
            let height = manager.usedRect(for: container).height + 2 * logTextView.textContainerInset.height
            logTextView.setFrameSize(NSSize(width: max(1, contentSize.width), height: max(contentSize.height, height)))
        }
    }

    private func scrollToLatest() {
        logTextView.scrollRangeToVisible(NSRange(location: (logTextView.string as NSString).length, length: 0))
    }
}

private struct ActivityLogView: NSViewRepresentable {
    let text: String
    let followLatest: Bool
    func makeNSView(context: Context) -> ActivityLogScrollView {
        ActivityLogScrollView(frame: .zero)
    }
    func updateNSView(_ view: ActivityLogScrollView, context: Context) {
        view.update(text: text, followLatest: followLatest)
    }
}

private struct ContentView: View {
    @ObservedObject var model: RecoveryModel
    @AppStorage(AppLanguage.preference) private var language = AppLanguage.chinese
    @State private var followLatestLog = true
    @State private var logExpanded = false
    init(model: RecoveryModel, logExpanded: Bool = false) {
        self.model = model
        _logExpanded = State(initialValue: logExpanded)
    }
    private func chooseDirectory(_ completion: (URL) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { completion(url) }
    }

    private func chooseDAT(_ completion: (URL) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.prompt = t("选择 DAT 文件")
        if panel.runModal() == .OK, let url = panel.url { completion(url) }
    }

    private func chooseMountedCard(_ completion: (URL) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: "/Volumes", isDirectory: true)
        panel.prompt = t("选择这张存储卡")
        if panel.runModal() == .OK, let url = panel.url { completion(url) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(t("ILCE-7M4 恢复工具  0.1.52"))
                    .font(.title2.bold())
                Spacer()
                Picker("语言 / Language", selection: $language) {
                    Text("简体中文").tag(AppLanguage.chinese)
                    Text("English").tag(AppLanguage.english)
                }
                .pickerStyle(.menu)
                .fixedSize()
            }
            HStack(alignment: .center, spacing: 8) {
                ForEach(Page.allCases) { page in
                    Button { model.select(page) } label: {
                        VStack(spacing: 2) {
                            ForEach(navigationPageLines(page, language: language), id: \.self) { line in
                                Text(line).lineLimit(1).minimumScaleFactor(0.9)
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .padding(.horizontal, 5)
                        .background(RoundedRectangle(cornerRadius: 6)
                            .fill(model.page == page ? Color.teal : Color.gray.opacity(0.15)))
                        .foregroundStyle(model.page == page ? Color.white : Color.primary)
                    }
                        .buttonStyle(.plain)
                        .disabled(model.busy)
                }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    switch model.page {
                    case .board: boardPage
                    case .prepareCamera: prepareCameraPage
                    case .connection: connectionPage
                    case .stock: stockPage
                    case .backup: backupPage
                    case .restore: restorePage
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            if model.busy {
                if model.page != .backup || model.backupStartedAt == nil || model.backupFinishedAt != nil {
                    if let fraction = model.progressFraction { ProgressView(value: fraction) }
                    else { ProgressView() }
                }
                Text(t(model.stage)).foregroundStyle(.secondary)
            }
            HStack {
                Button(t("导出日志")) {
                    let panel = NSSavePanel()
                    panel.nameFieldStringValue = "Camera-Recovery-Logs.zip"
                    if panel.runModal() == .OK, let url = panel.url { model.exportLogs(url) }
                }.disabled(model.busy)
            }
            DisclosureGroup(t("运行日志"), isExpanded: $logExpanded) {
                VStack(alignment: .leading, spacing: 4) {
                    Toggle(t("跟随最新日志"), isOn: $followLatestLog)
                        .toggleStyle(.checkbox)
                    ActivityLogView(text: model.logs, followLatest: followLatestLog)
                        .frame(height: 145)
                }
            }
        }
        .padding(18)
        .frame(minWidth: 900, minHeight: 640)
        .alert(t("任务未完成"), isPresented: Binding(get: { model.errorText != nil }, set: { if !$0 { model.errorText = nil } })) {
            Button(t("好"), role: .cancel) { model.errorText = nil }
        } message: { Text(t(model.errorText ?? "")) }
        .alert(t("确认安装"), isPresented: $model.showBoardInstall) {
            Button(t("取消"), role: .cancel) { }
            Button(t("安装配套程序")) { model.installBoard() }
        } message: { Text(t("将配套程序安装到当前 BOOTSEL 中的 RP2040。安装后转接头会重新连接。")) }
        .alert(t("确认恢复相机"), isPresented: $model.showStockConfirmation) {
            Button(t("取消"), role: .cancel) { }
            Button(t("我已了解风险，开始恢复")) { model.runStock() }
        } message: {
            Text(t("将使用卡槽 1 存储卡中的固件包恢复相机。此操作可能导致数据丢失、相机无法启动，并可能影响原厂保修。请保持供电和连接。"))
        }
        .alert(t("替换存储卡上的固件包？"), isPresented: $model.showSDReplacement) {
            Button(t("取消"), role: .cancel) { }
            Button(t("替换原文件")) { model.prepareSDCard(confirmReplace: true) }
        } message: {
            Text(t("所选存储卡 ") + (model.sdVolume?.path ?? "") +
                 t(" 根目录已有不同的 BODYDATA.DAT。确认后，原文件将被所选 DAT 替换，并核对复制完整性。请确认这是用于本次更新的存储卡。"))
        }
        .alert(t("确认上次更新已失败或停止"), isPresented: $model.showPriorUpdateFailureConfirmation) {
            Button(t("取消"), role: .cancel) { }
            Button(t(model.priorUpdateFailureReason == "blink_5_long_6_short" ?
                   "确认观察到 5 长 6 短闪灯" : "我确认未插卡且更新已失败")) {
                model.confirmPreviousStockUpdateFailed()
            }
        } message: {
            Text(t(model.priorUpdateFailureReason == "blink_5_long_6_short" ?
                 "仅当相机持续出现 5 长 6 短闪灯，且未显示“更新完成”时确认。此操作只记录你看到的错误，不判断具体原因，也不会重启或继续备份。" :
                 "仅当上次更新时没有插入存储卡，且相机没有显示“更新完成”时选择。此操作只记录你的观察；不会自动重启相机。"))
        }
        .onChange(of: model.requestSDVolumePicker) { shouldOpen in
            guard shouldOpen else { return }
            model.requestSDVolumePicker = false
            chooseMountedCard { model.selectCameraSDVolume($0) }
        }
    }

    private var boardPage: some View {
        GroupBox(t("使用前准备")) {
            VStack(alignment: .leading, spacing: 12) {
                CableIllustration(firstRun: true)
                Text(t(model.boardState)).font(.headline)
                    .foregroundStyle(model.boardState == "已安装" ? .green : .primary)
                Text(t(model.boardMessage))
                if model.showBootSteps {
                    Text(t("请断开 RP2040（USB 1）与电脑的连接，按住 RP2040 上的 BOOT 键，再将 USB 1 接回电脑。接好后松开 BOOT 键，然后点击“检查转接头”。"))
                        .foregroundStyle(.red)
                        .fontWeight(.bold)
                }
                Button(t(model.boardReadyToInstall ? "安装配套程序" : "检查转接头")) { model.checkBoard() }
                    .buttonStyle(.borderedProminent).disabled(model.busy)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var prepareCameraPage: some View {
        GroupBox(t("准备相机")) {
            VStack(alignment: .leading, spacing: 12) {
                Text(t("建议将相机设为连接电脑时自动进入 MSC。"))
                Text(t("MENU → 设置 → USB → USB连接模式 → 海量存储器（MSC）"))
                    .font(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                Button(t("我已准备好")) { model.select(.connection) }
                    .buttonStyle(.borderedProminent)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var connectionPage: some View {
        GroupBox(t("连接示意图")) {
            VStack(alignment: .leading, spacing: 12) {
                CableIllustration(firstRun: false)
                Text(t("先将 Multi 插头接相机、USB 1 和 USB 2 的 USB-C 插头接电脑；再打开相机，立即点击“检查连接”进入临时恢复模式并读取 USB 2。"))
                Text(t("USB 1（RP2040）：\(model.controlState)"))
                    .foregroundStyle(model.controlVerified ? .green : .primary)
                Text(t("USB 2（DATA）：\(model.dataState)"))
                    .foregroundStyle(model.dataVerified ? .green : .primary)
                Text(t("相机当前模式：\(model.currentModeState)"))
                Text(t(model.connectionMessage)).foregroundStyle(.secondary)
                Button(t("检查连接")) { model.checkConnection() }.buttonStyle(.borderedProminent).disabled(model.busy)
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var priorStockOutcomeControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(t("先前更新尚未确认结束。请根据相机屏幕上的实际结果选择："))
                .font(.headline)
            Button(t("上次已显示更新完成")) { model.confirmPreviousStockUpdateComplete() }
                .disabled(model.busy)
            Button(t("上次未插卡，更新已失败或停止")) {
                model.priorUpdateFailureReason = "no_sd_card"
                model.showPriorUpdateFailureConfirmation = true
            }.disabled(model.busy)
            Button(t("相机持续 5 长 6 短闪灯，更新已失败")) {
                model.priorUpdateFailureReason = "blink_5_long_6_short"
                model.showPriorUpdateFailureConfirmation = true
            }.disabled(model.busy)
        }
    }

    private var stockPage: some View {
        GroupBox(t("从 SD 卡恢复原厂系统")) {
            VStack(alignment: .leading, spacing: 12) {
                switch model.sdStep {
                case .entry:
                    Picker(t("相机型号"), selection: $model.sdCameraModel) {
                        ForEach(SDCamera.models) { camera in
                            Text("\(camera.name) (\(camera.id))").tag(camera.id)
                        }
                    }.fixedSize()
                    HStack {
                        Button(t("去下载固件包")) {
                            NSWorkspace.shared.open(model.selectedSDCamera.supportURL)
                        }
                        if let firmwareDownloads = model.selectedSDCamera.firmwareDownloadsURL {
                            Button(t("更多下载链接 🔗")) { NSWorkspace.shared.open(firmwareDownloads) }
                        }
                    }
                    Text(t("机内 SD 更新引导")).font(.headline)
                    Text(t("相机能进入菜单时，请在机内启动官方更新。"))
                    Button(t("准备存储卡，在相机菜单更新")) { model.beginSDHelper(inCamera: true) }
                    Button(t("已准备好存储卡，查看机内更新步骤")) {
                        model.sdArrivedFromHelper = false
                        model.sdPackageLabel = ""
                        model.sdStep = .officialUpdate
                    }
                    if model.selectedSDCamera.supportsAdapterRecovery {
                        Divider()
                        Text(t(model.sdCameraModel == "ILCE-7RM5" ? "A7R V 引导恢复" : "A7 IV 引导恢复")).font(.headline)
                        Text(t(model.sdCameraModel == "ILCE-7RM5" ? "无法进入菜单时，A7R V 可使用 Multi 转接头启动恢复。" : "无法进入菜单时，A7 IV 可使用 Multi 转接头启动恢复。"))
                        DisclosureGroup(t("使用提示")) {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(t("改过固件的相机，直接从 SD 卡恢复旧版本可能会失败。如果目的是让相机恢复正常，建议优先使用同一台相机的正常全量备份。"))
                                Text(t("如果确实需要回退旧版本，但第一次失败：")).fontWeight(.medium)
                                Text(t("1. 用全量备份或 SD 卡引导恢复到能正常开机、进入菜单的版本。"))
                                Text(t("2. 在相机上选择「设置 → 重置/保存设置 → 出厂重置 → 初始化」。"))
                                Text(t("3. 初始化完成后，再用目标版本的固件包重试 SD 卡引导恢复。"))
                            }
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.vertical, 6)
                        }
                        Button(t("我已放入相机卡槽 1")) { model.choosePreparedSDCard() }
                            .buttonStyle(.borderedProminent).disabled(model.busy)
                        Button(t("帮我放进存储卡")) { model.beginSDHelper() }.disabled(model.busy)
                    } else {
                        Text(t("此机型的转接头恢复尚不支持。")).foregroundStyle(.secondary)
                    }
                case .source:
                    Text(t("选择要放入存储卡的 DAT 文件"))
                    Text(model.sdSourceDAT?.path ?? t("尚未选择文件")).lineLimit(2).foregroundStyle(.secondary)
                    Button(t("选择 DAT 文件…")) { chooseDAT { model.selectSDSource($0) } }.disabled(model.busy)
                    Button(t("选择电脑挂载的存储卡")) {
                        chooseMountedCard { model.selectComputerSDVolume($0) }
                    }.buttonStyle(.borderedProminent).disabled(model.busy || model.sdSourceDAT == nil)
                    if !model.sdInCameraUpdate {
                        Button(t("选择相机挂载的存储卡")) { model.prepareCameraCardConnection() }
                            .disabled(model.busy || model.sdSourceDAT == nil)
                        if !model.sdCameraCandidates.isEmpty && model.sdUsingCameraRoute && model.sdVolume == nil {
                            Button(t("选择已检测到的存储卡…")) {
                                chooseMountedCard { model.selectCameraSDVolume($0) }
                            }.disabled(model.busy)
                        }
                        if model.sdBlockingStockJob != nil {
                            priorStockOutcomeControls
                        }
                    }
                    if !model.sdPreparationMessage.isEmpty {
                        Text(t(model.sdPreparationMessage)).foregroundStyle(.secondary)
                    }
                case .target:
                    Text(t("所选文件：") + (model.sdSourceDAT?.lastPathComponent ?? t("未选择")))
                    Text(t("目标存储卡：") + (model.sdVolume?.path ?? t("未选择")))
                    Button(t("更换目标存储卡…")) {
                        chooseMountedCard { model.selectSDVolume($0) }
                    }.disabled(model.busy)
                    if model.sdUsingCameraRoute {
                        Toggle(t("我确认所选路径是相机的存储卡"), isOn: $model.sdCameraVolumeConfirmed)
                            .disabled(model.busy || model.sdVolume == nil)
                    }
                    Text(t("写入前会校验所选 DAT 的复制完整性；不会格式化存储卡。"))
                        .foregroundStyle(.secondary)
                    Button(t("写入存储卡")) { model.prepareSDCard() }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.busy || model.sdSourceDAT == nil || model.sdVolume == nil ||
                                  (model.sdUsingCameraRoute && !model.sdCameraVolumeConfirmed))
                    if !model.sdPreparationMessage.isEmpty {
                        Text(t(model.sdPreparationMessage)).foregroundStyle(.secondary)
                    }
                case .officialUpdate:
                    Text(t("在相机上更新")).font(.headline)
                    if model.sdArrivedFromHelper {
                        Text(t(model.sdPreparationMessage))
                        Text(model.sdPackageLabel.isEmpty ? t("此 DAT 的机型与版本尚未识别，请按 Sony 下载页核对。") : model.sdPackageLabel)
                    }
                    Text(model.selectedSDCamera.name).foregroundStyle(.secondary)
                    Text(t(model.selectedSDCamera.updateInstructions))
                    Link(t("查看 Sony 针对当前版本的升级要求"), destination: model.selectedSDCamera.supportURL)
                case .slot:
                    Text(t(model.sdPreparationMessage))
                    if !model.sdCopyProofText.isEmpty {
                        Text(t(model.sdCopyProofText)).font(.caption).foregroundStyle(.secondary)
                    }
                    Text(t("复制完整不代表所选固件适用于相机恢复。"))
                        .foregroundStyle(.secondary)
                    Button(t(model.sdUsingCameraRoute ? "确认卡仍在槽 1，继续" : "已插入卡槽 1，继续")) {
                        model.stockCardConfirmed = true
                        model.sdStep = .ready
                    }.buttonStyle(.borderedProminent).disabled(model.busy)
                case .ready:
                    if model.showStockCompletionButton {
                        Text(t("请等待相机更新，预计约 7 分钟。更新期间请保持供电，不要操作相机、拔线或取出存储卡。以相机屏幕显示“更新完成”为准。"))
                            .font(.headline)
                        if let started = model.stockRebootAcknowledgedAt {
                            TimelineView(.periodic(from: .now, by: 1)) { context in
                                Text(t("已等待 \(elapsedStockTime(since: started, now: context.date))"))
                                    .font(.system(size: 24, weight: .bold, design: .monospaced))
                            }
                        } else {
                            Text(t("等待计时暂不可用；请以相机屏幕为准。"))
                                .foregroundStyle(.secondary)
                        }
                        Button(t("相机已显示更新完成，继续")) { model.confirmStockUpdateComplete() }
                            .buttonStyle(.borderedProminent).disabled(model.busy)
                    } else if model.sdBlockingStockJob != nil {
                        priorStockOutcomeControls
                        if !model.taskMessage.isEmpty { Text(t(model.taskMessage)).foregroundStyle(.secondary) }
                    } else {
                        Toggle(t(model.sdCameraModel == "ILCE-7RM5" ? "我确认相机是 A7R V（ILCE-7RM5）" : "我确认相机是 A7M4（ILCE-7M4）"), isOn: $model.stockModelConfirmed)
                        Toggle(t("相机电量充足，电脑保持唤醒"), isOn: $model.stockPowerConfirmed)
                        Text(t(model.controlVerified ? "Multi 转接头已验证" : "请先检查 Multi 转接头"))
                            .foregroundStyle(.secondary)
                        Button(t("只检查 Multi 转接头")) { model.probeControlForTask() }.disabled(model.busy)
                        Button(t(model.showFreshStockPlanAction ? "重新读取并制定方案" : "开始引导恢复")) {
                            model.startStock()
                        }.buttonStyle(.borderedProminent)
                            .disabled(model.busy)
                        if !model.taskMessage.isEmpty { Text(t(model.taskMessage)).foregroundStyle(.secondary) }
                    }
                }
                if model.sdStep != .entry && !model.showStockCompletionButton {
                    Button(t("返回")) { model.backSDStep() }.disabled(model.busy)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var backupPage: some View {
        GroupBox(t("备份相机")) {
            VStack(alignment: .leading, spacing: 12) {
                Text(t("备份进度按本机实测时间估算，验证完成后显示 100%。"))
                HStack {
                    Text(model.outputDirectory?.path ?? t("尚未选择保存位置")).lineLimit(1)
                    Button(t("选择保存位置…")) { chooseDirectory { model.selectBackupDestination($0) } }.disabled(model.busy)
                }
                Text(t("继续已有备份时，请选择原备份任务目录。"))
                    .font(.caption).foregroundStyle(.secondary)
                if let job = model.backupJobPath { Text(t("最近任务目录：") + job).font(.caption).textSelection(.enabled) }
                Toggle(t("相机电量充足，电脑保持唤醒"), isOn: $model.powerConfirmed)
                Toggle(t("保存位置有足够空间"), isOn: $model.spaceConfirmed)
                Button(t("只检查 Multi 转接头")) { model.probeControlForTask() }.disabled(model.busy)
                Button(t("开始备份")) { model.startBackup() }.buttonStyle(.borderedProminent).disabled(model.busy)
                if let started = model.backupStartedAt {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        let stop = model.backupFinishedAt ?? context.date
                        let elapsed = elapsedStockTime(since: started, now: stop)
                        let progress = estimatedBackupProgress(elapsed: stop.timeIntervalSince(started),
                                                               verified: model.backupVerified)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(t(model.backupFinishedAt == nil ? "备份已进行 " : "本次备份耗时 ") + elapsed)
                                .font(.headline.monospacedDigit())
                            ProgressView(value: progress) {
                                Text(t("估算进度 ") + "\(Int(progress * 100))%")
                            }
                        }
                    }
                }
                Text(t(model.backupEvidenceState)).foregroundStyle(model.backupEvidenceState.contains("已验证") ? .green : .secondary)
                if !model.backupModeState.isEmpty { Text(t(model.backupModeState)).foregroundStyle(.secondary) }
                if !model.taskMessage.isEmpty { Text(t(model.taskMessage)).foregroundStyle(.secondary) }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var restorePage: some View {
        GroupBox(t("恢复已保存状态")) {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(model.restoreTarget?.path ?? t("尚未选择备份")).lineLimit(1)
                    Button(t("选择要恢复的备份…")) { chooseDirectory { model.selectRestoreTarget($0) } }
                }
                Text(t("可选择 snapshot 文件夹或备份任务文件夹。"))
                    .font(.caption).foregroundStyle(.secondary)
                Toggle(t("相机电量充足，电脑保持唤醒"), isOn: $model.powerConfirmed)
                Text(t("将先核对本机备份，再连接相机并恢复。"))
                Button(t("只检查 Multi 转接头")) { model.probeControlForTask() }.disabled(model.busy)
                if model.restoreFailureObserved {
                    Text(t("原任务记录已保留；重新开始将读取相机当前状态并建立独立的新任务。"))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button(t(model.restoreFailureObserved ? "从所选备份重新开始" : "开始恢复")) {
                    model.startRestore()
                }.buttonStyle(.borderedProminent).disabled(model.busy)
                if let planned = model.restorePlannedFragments {
                    Text("\(t("已验证片段")) \(model.restoreVerifiedFragments)/\(planned) · \(t("未通过")) \(model.restoreFailedFragments)")
                        .font(.system(.body, design: .monospaced))
                } else if model.busy {
                    Text(t("正在计算本次计划片段数…"))
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let started = model.restoreStartedAt {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        let stop = model.restoreFinishedAt ?? context.date
                        Text(t(model.restoreFinishedAt == nil ?
                               "恢复已进行 \(elapsedStockTime(since: started, now: stop))" :
                               "本次恢复耗时 \(elapsedStockTime(since: started, now: stop))"))
                            .font(.system(size: 20, weight: .semibold, design: .monospaced))
                    }
                }
                if let started = model.restoreNORStartedAt {
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        let stop = model.restoreNORFinishedAt ?? context.date
                        VStack(alignment: .leading, spacing: 4) {
                            if model.restoreNORFinishedAt == nil {
                                Text(t("启动数据重建预计约 3 分钟；请保持供电和连接。"))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                            Text(t("启动数据阶段已进行 \(elapsedStockTime(since: started, now: stop))"))
                                .font(.system(.body, design: .monospaced))
                        }
                    }
                }
                if model.restoreHasPartialOutcome {
                    Text(t(model.taskMessage)).foregroundStyle(.orange)
                    if let skipped = model.restoreSkippedLabel {
                        Text(t(skipped)).foregroundStyle(.secondary)
                    }
                } else if model.restoreHasSkippedRegions {
                    Text(t("可恢复区域已验证；有区域未恢复")).foregroundStyle(.orange)
                    Text(t(model.taskMessage)).foregroundStyle(.secondary)
                } else {
                    Text(t(model.taskMessage)).foregroundStyle(model.restoreFullyVerified ? .green : .secondary)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

#if NATIVE_VIEW_TEST
@main private struct NativeViewSmoke {
    @MainActor static func main() throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("native-view-smoke-\(UUID().uuidString)")
        setenv("CAMERA_RECOVERY_TEST_LOG_DIRECTORY", directory.path, 1)
        defer { try? FileManager.default.removeItem(at: directory) }
        func require(_ passed: @autoclosure () -> Bool, _ label: String) {
            if !passed() { fputs("Native view smoke failed: \(label)\n", stderr); exit(1) }
        }
        func descendants<T: NSView>(_ view: NSView, as type: T.Type) -> [T] {
            (view as? T).map { [$0] } ?? view.subviews.flatMap { descendants($0, as: type) }
        }
        func visibleCharacters(_ scroll: ActivityLogScrollView) -> NSRange {
            let text = scroll.logTextView
            var rect = text.visibleRect
            rect.origin.x -= text.textContainerOrigin.x
            rect.origin.y -= text.textContainerOrigin.y
            let glyphs = text.layoutManager!.glyphRange(forBoundingRect: rect, in: text.textContainer!)
            return text.layoutManager!.characterRange(forGlyphRange: glyphs, actualGlyphRange: nil)
        }
        let row = "2026-11-17 03:52:58 UTC  readback: fragment verified; 文件路径 /Volumes/Offline/相机 snapshot/结果 🙂 "
        let input = ProcessInfo.processInfo.environment["CAMERA_RECOVERY_LOG_FIXTURE"]
        let history = try input.map { try String(contentsOfFile: $0, encoding: .utf8) } ??
            (0..<10000).map { "\(row)\($0)\n" }.joined()
        let longLog = history + "LATEST_RESULT: NEEDS_USER_ACTION; reboot acknowledged once\n"
        let log = ActivityLogScrollView(frame: NSRect(x: 0, y: 0, width: 864, height: 145))
        log.update(text: longLog, followLatest: true)
        log.layoutSubtreeIfNeeded()
        require(log.logTextView.frame.height > 100000, "long-session document exceeds old 100000pt geometry")
        let marker = (longLog as NSString).range(of: "LATEST_RESULT")
        require(NSIntersectionRange(visibleCharacters(log), marker).length > 0, "latest line has visible glyphs in 145pt viewport")
        require(log.logTextView.string == longLog && log.logTextView.isSelectable && !log.logTextView.isEditable,
                "complete selectable read-only log")
        log.logTextView.setSelectedRange(marker)
        let clipboard = NSPasteboard(name: NSPasteboard.Name("native-log-smoke-\(UUID().uuidString)"))
        defer { clipboard.releaseGlobally() }
        let copyTypes = log.logTextView.writablePasteboardTypes
        clipboard.declareTypes(copyTypes, owner: nil)
        require(log.logTextView.writeSelection(to: clipboard, types: copyTypes), "copy selected text")
        require(copyTypes.contains { clipboard.string(forType: $0) == "LATEST_RESULT" }, "copied exact selected text")
        log.update(text: longLog, followLatest: false)
        log.contentView.scroll(to: NSPoint(x: 0, y: 1000))
        log.reflectScrolledClipView(log.contentView)
        let manualOrigin = log.contentView.bounds.origin
        let appended = longLog + "APPENDED 🙂: new progress\n"
        log.update(text: appended, followLatest: false)
        require(abs(log.contentView.bounds.origin.y - manualOrigin.y) < 1, "append preserves manual scroll")
        require(log.logTextView.selectedRange() == marker, "append preserves Unicode selection")
        log.update(text: appended, followLatest: true)
        require(NSIntersectionRange(visibleCharacters(log), (appended as NSString).range(of: "APPENDED")).length > 0,
                "reenabling follow shows newest line")
        let wideHeight = log.logTextView.frame.height
        log.setFrameSize(NSSize(width: 320, height: 145))
        log.layoutSubtreeIfNeeded()
        log.update(text: appended, followLatest: true)
        require(NSIntersectionRange(visibleCharacters(log), (appended as NSString).range(of: "APPENDED")).length > 0,
                "narrow resize keeps newest line visible")
        require(log.logTextView.frame.height > wideHeight && !log.hasHorizontalScroller,
                "long bilingual lines wrap at narrow width")
        log.update(text: "short replacement\n", followLatest: true)
        require(log.logTextView.string == "short replacement\n" && visibleCharacters(log).length > 0, "short replacement resets safely")

        // Use a process-local volatile domain, never the running App's preferences.
        let defaults = UserDefaults.standard
        let priorArguments = defaults.volatileDomain(forName: UserDefaults.argumentDomain)
        defer { defaults.setVolatileDomain(priorArguments, forName: UserDefaults.argumentDomain) }
        let window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: 900, height: 640),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        let pageLog = "LATEST_RESULT: simulated page task result\n"
        var cases = 0
        func checkLayout(_ model: RecoveryModel, _ size: NSSize, _ label: String) {
            let host = NSHostingView(rootView: ContentView(model: model, logExpanded: true))
            window.contentView = host
            window.setContentSize(size)
            host.frame = NSRect(origin: .zero, size: size)
            RunLoop.main.run(until: Date().addingTimeInterval(0.01))
            host.layoutSubtreeIfNeeded()
            let found = descendants(host, as: ActivityLogScrollView.self)
            require(found.count == 1, "\(label): shared log is mounted")
            let mounted = found[0]
            mounted.update(text: model.logs, followLatest: true)
            require(abs(mounted.frame.height - 145) < 1 && mounted.visibleRect.height >= 144,
                    "\(label): log viewport is not clipped at \(size)")
            require(NSIntersectionRange(visibleCharacters(mounted), (model.logs as NSString).range(of: "LATEST_RESULT")).length > 0,
                    "\(label): latest line glyphs are visible")
            cases += 1
        }
        let longSession = RecoveryModel()
        longSession.page = .stock
        longSession.sdStep = .ready
        longSession.logs = longLog
        checkLayout(longSession, NSSize(width: 900, height: 640), "full long session in SwiftUI host")
        for language in [AppLanguage.chinese, AppLanguage.english] {
            defaults.setVolatileDomain([AppLanguage.preference: language], forName: UserDefaults.argumentDomain)
            for message in [
                "请断开 RP2040（USB 1）与电脑的连接，按住 RP2040 上的 BOOT 键，再将 USB 1 接回电脑。接好后松开 BOOT 键，然后点击“检查转接头”。",
                "先将 Multi 插头接相机、USB 1 和 USB 2 的 USB-C 插头接电脑；再打开相机，立即点击“检查连接”进入临时恢复模式并读取 USB 2。",
                "请等待相机更新，预计约 7 分钟。更新期间请保持供电，不要操作相机、拔线或取出存储卡。以相机屏幕显示“更新完成”为准。",
                "备份进度按本机实测时间估算，验证完成后显示 100%。",
                "原任务记录已保留；重新开始将读取相机当前状态并建立独立的新任务。"
            ] {
                let paragraph = NSHostingView(rootView: Text(translatedText(message, language: language))
                    .frame(width: 300, alignment: .leading))
                require(paragraph.fittingSize.width <= 301 && paragraph.fittingSize.height > 20,
                        "\(language): long workflow instructions wrap")
            }
            for page in Page.allCases {
                for state in ["idle", "busy", "busy_indeterminate", "busy_nor", "success", "success_nor", "failure"] {
                    if state == "success_nor" && page != .restore { continue }
                    let model = RecoveryModel()
                    model.page = page
                    model.logs = pageLog
                    model.sdStep = .ready
                    model.busy = state.hasPrefix("busy")
                    model.stage = "正在通过 Multi 转接头进入临时恢复模式…"
                    model.progressFraction = state == "busy" ? 0.42 : nil
                    model.taskMessage = state == "failure" ? "已停止，未写入相机。请查看日志中的具体原因，再重新检查。" :
                        state == "success" ? "已完成并验证。" : ""
                    model.boardMessage = model.taskMessage
                    model.showBootSteps = page == .board && state == "failure"
                    model.boardState = model.busy ? "正在检查" : state == "success" ? "已安装" : "待检查"
                    model.connectionMessage = model.taskMessage
                    model.controlVerified = state == "success"
                    model.dataVerified = state == "success"
                    model.backupEvidenceState = model.busy ? "正在备份并核验…" :
                        state == "success" ? "备份文件与收据已验证。" : "备份尚未完成；可在同一保存位置继续。"
                    model.restoreStartedAt = state == "idle" ? nil : Date().addingTimeInterval(-754)
                    model.restoreFinishedAt = model.busy || state == "idle" ? nil : Date()
                    if page == .restore && state == "busy_nor" {
                        model.restoreNORStartedAt = Date().addingTimeInterval(-91)
                        model.restoreNORStatus = "RUNNING"
                        model.stage = "正在重建并校验启动数据，请保持供电和连接…"
                    }
                    model.restorePlannedFragments = state == "busy_indeterminate" ? nil : 12000
                    model.restoreVerifiedFragments = 10567
                    model.restoreFailedFragments = state == "failure" ? 1 : 0
                    if state == "success_nor" {
                        model.restoreNORStartedAt = Date().addingTimeInterval(-120)
                        model.restoreNORFinishedAt = Date()
                        model.restoreNORStatus = "VERIFIED"
                        model.restoreFullyVerified = true
                        model.taskMessage = "存储恢复与启动数据重建已验证，相机已返回普通模式。请检查取景与菜单是否正常。"
                    }
                    model.restoreHasSkippedRegions = state == "success"
                    model.restoreHasPartialOutcome = state == "failure"
                    model.restoreSkippedLabel = state == "failure" ? "跳过区域：cp-nor" : nil
                    model.showStockCompletionButton = page == .stock && state == "success"
                    model.stockRebootAcknowledgedAt = Date().addingTimeInterval(-754)
                    for size in [NSSize(width: 900, height: 640), NSSize(width: 1200, height: 900)] {
                        checkLayout(model, size, "\(page)/\(language)/\(state)")
                    }
                }
            }
            for step in [SDStep.entry, .source, .target, .slot, .officialUpdate] {
                let model = RecoveryModel()
                model.page = .stock
                model.sdStep = step
                model.sdSourceDAT = URL(fileURLWithPath: "/tmp/Offline selected firmware with a long descriptive name/BODYDATA.DAT")
                model.sdVolume = URL(fileURLWithPath: "/Volumes/Offline camera memory card")
                model.sdPreparationMessage = "存储卡尚未准备好。请查看日志中的具体原因；未确认替换前不会覆盖原文件。"
                model.sdCopyProofText = "复制回执已保存；源文件与卡上文件的字节数和摘要一致。"
                model.sdUsingCameraRoute = true
                model.sdCameraCandidates = ["/Volumes/OfflineCard"]
                model.sdBlockingStockJob = "/tmp/offline-unconfirmed-stock-job"
                model.logs = pageLog
                for size in [NSSize(width: 900, height: 640), NSSize(width: 1200, height: 900)] {
                    checkLayout(model, size, "stock/\(language)/\(step)")
                }
            }
        }
        print("Native log selection/copy/follow/manual-scroll/Unicode/resize checks and \(cases) bilingual page/state/size layouts passed")
    }
}
#elseif NATIVE_WORKER_TEST
@main private struct WorkerProtocolSmoke {
    static func main() {
        let testLogDirectory = FileManager.default.temporaryDirectory
            .appendingPathComponent("camera-recovery-worker-smoke-\(ProcessInfo.processInfo.processIdentifier)")
        setenv("CAMERA_RECOVERY_TEST_LOG_DIRECTORY", testLogDirectory.path, 1)
        defer { try? FileManager.default.removeItem(at: testLogDirectory) }
        do {
            let model = RecoveryModel()
            var messages: [String: String] = [:]
            for page in Page.allCases {
                model.select(page)
                model.taskMessage = "Old result for \(page.rawValue)"
                messages[page.rawValue] = model.taskMessage
            }
            for page in Page.allCases {
                model.select(page)
                model.probeControlForTask()
                guard model.busy, model.taskMessage.isEmpty, !model.stage.isEmpty else {
                    throw NSError(domain: "FreshTaskStatus", code: 1)
                }
                model.select(page == .board ? .stock : .board)
                guard model.page == page else { throw NSError(domain: "BusyNavigation", code: 1) }
                let deadline = Date().addingTimeInterval(8)
                while model.busy && Date() < deadline {
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
                }
                guard !model.busy, model.stage.isEmpty, model.progressFraction == nil,
                      model.taskMessage.contains("本次 PING 已验证") else {
                    throw NSError(domain: "FreshTaskCompletion", code: 1)
                }
                messages[page.rawValue] = model.taskMessage
                for other in Page.allCases {
                    model.select(other)
                    guard model.taskMessage == messages[other.rawValue] else {
                        throw NSError(domain: "PageStatusLeak", code: 1)
                    }
                }
            }
        } catch {
            fputs("Page-specific current-task status smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            func finish(_ model: RecoveryModel) {
                let deadline = Date().addingTimeInterval(8)
                while model.busy && Date() < deadline {
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
                }
            }
            let board = RecoveryModel()
            board.page = .board
            board.boardMessage = "old failure"
            board.showBootSteps = true
            board.checkBoard()
            guard board.busy, board.boardState == "正在检查", board.boardMessage.isEmpty, !board.showBootSteps else {
                throw NSError(domain: "BoardFreshPresentation", code: 1)
            }
            finish(board)
            guard !board.busy, board.boardReadyToInstall, board.boardState == "未安装" else {
                throw NSError(domain: "BoardReadyPresentation", code: 1)
            }
            board.installBoard(); finish(board)
            guard !board.busy, board.boardState == "已安装", board.controlVerified else {
                throw NSError(domain: "BoardInstalledPresentation", code: 1)
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "board_needs_boot", 1)
            let unpreparedBoard = RecoveryModel()
            unpreparedBoard.checkBoard(); finish(unpreparedBoard)
            guard unpreparedBoard.showBootSteps, !unpreparedBoard.boardReadyToInstall else {
                throw NSError(domain: "BoardBootInstructions", code: 1)
            }
            unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
            let connection = RecoveryModel()
            connection.page = .connection
            for scenario in ["normal_ready", "normal_uncertain"] {
                setenv("CAMERA_RECOVERY_FAKE_SCENARIO", scenario, 1)
                connection.controlVerified = true
                connection.dataVerified = true
                connection.connectionMessage = "old connection result"
                connection.checkConnection()
                guard connection.busy, !connection.controlVerified, !connection.dataVerified,
                      connection.controlState == "正在检查", connection.dataState == "正在检查",
                      connection.currentModeState == "正在检查", connection.connectionMessage.isEmpty else {
                    throw NSError(domain: "ConnectionFreshPresentation", code: 1)
                }
                finish(connection)
                guard !connection.busy, connection.controlVerified, connection.dataVerified,
                      connection.currentModeState == (scenario == "normal_ready" ? "普通 USB 模式（当前已观察）" : "未确认返回普通模式"),
                      !connection.connectionMessage.isEmpty else {
                    throw NSError(domain: "ConnectionResultPresentation", code: 1)
                }
            }
            unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
        } catch {
            fputs("Board/connection current-result smoke failed: \(error)\n", stderr)
            exit(1)
        }
        guard needsLanguageSelection(""), needsLanguageSelection("fr"),
              !needsLanguageSelection(AppLanguage.chinese),
              !needsLanguageSelection(AppLanguage.english),
              translatedText("检查连接", language: AppLanguage.chinese) == "检查连接",
              translatedText("检查连接", language: AppLanguage.english) == "Check connections",
              translatedText("USB 1（RP2040）：本次 PING 已验证", language: AppLanguage.english) ==
                "USB 1 (RP2040): PING verified in this check",
              translatedText("已等待 12:34", language: AppLanguage.english) == "Elapsed: 12:34",
              translatedText("恢复已进行 12:34", language: AppLanguage.english) == "Recovery elapsed: 12:34",
              translatedText("本次恢复耗时 12:34", language: AppLanguage.english) == "Recovery elapsed: 12:34",
              translatedText("读取当前状态中断：连续重连 2/5；累计重连 3 次…", language: AppLanguage.english) ==
                "Current-state read interrupted: consecutive reconnects 2/5; total reconnects 3…",
              translatedText("恢复连接中断，正在重新连接并核对（第 2/5 次）…", language: AppLanguage.english) ==
                "Restore connection interrupted; reconnecting and checking (attempt 2/5)…",
              translatedText("所选存储卡 /Volumes/CARD 根目录已有不同的 BODYDATA.DAT。确认后，原文件将被所选 DAT 替换，并核对复制完整性。请确认这是用于本次更新的存储卡。", language: AppLanguage.english) ==
                "Selected card /Volumes/CARD already has a different BODYDATA.DAT at its root. Confirming replaces it with the selected DAT and verifies the copy. Make sure this is the card you want to update from.",
              let skipped = restoreSkippedText(["status": "VERIFIED", "success_with_skips": true,
                                                "skipped_regions": [["name": "cp-nor", "changed": true]]]),
              skipped == "跳过区域：cp-nor",
              translatedText("恢复成功", language: AppLanguage.english) == "Recovery succeeded",
              translatedText(skipped, language: AppLanguage.english) == "Skipped regions: cp-nor",
              let unknownNOR = restoreSkippedText(["status": "VERIFIED", "success_with_skips": true,
                          "skipped_regions": [["name": "cp-nor", "reason": "not_captured_read_only",
                                               "changed": NSNull()]]]),
              unknownNOR == "跳过区域：cp-nor",
              translatedText(unknownNOR, language: AppLanguage.english) == "Skipped regions: cp-nor",
              restoreSkippedText(["status": "INCOMPLETE", "success_with_skips": true,
                                  "skipped_regions": [["name": "cp-nor"]]]) == nil else {
            fputs("Language selection/localization smoke failed\n", stderr)
            exit(1)
        }
        guard let rebootAck = parseISODate("2026-11-15T01:01:05.679051+00:00"),
              elapsedStockTime(since: rebootAck, now: rebootAck.addingTimeInterval(754)) == "12:34",
              elapsedStockTime(since: rebootAck, now: rebootAck.addingTimeInterval(3_661)) == "01:01:01",
              elapsedStockTime(since: rebootAck, now: rebootAck.addingTimeInterval(-5)) == "00:00" else {
            fputs("Stock elapsed timer smoke failed\n", stderr)
            exit(1)
        }
        let chicago = TimeZone(identifier: "America/Chicago")!
        guard localLogLine("2026-11-15T00:28:53Z  stock: stopped\n", timeZone: chicago) ==
                "2026-11-14 18:28:53 CST  stock: stopped\n",
              localLogLine("2026-11-15T00:28:53.123Z  stock: stopped\n", timeZone: chicago) ==
                "2026-11-14 18:28:53 CST  stock: stopped\n",
              localLogLine("2026-11-14 18:28:53 CST  stock: stopped\n", timeZone: chicago) ==
                "2026-11-14 18:28:53 CST  stock: stopped\n" else {
            fputs("Local log time conversion smoke failed\n", stderr)
            exit(1)
        }
        let plannerError = NSError(domain: "CameraRecoveryWorker", code: 1, userInfo: [
            NSLocalizedDescriptionKey: "recorded version outside approved settings range",
            "normal_mode_status": "NO_WRITE", "current_mode": "FASTBOOT_OR_UNKNOWN",
            "blocking_tasks": [["reason": "REBOOT_ACKNOWLEDGED", "job_dir": "/tmp/fixture-prior"]]
        ])
        let displayed = stockPlannerFailureMessage(plannerError)
        guard displayed.hasPrefix("恢复方案未通过：相机记录版本不在本工具已验证"),
              displayed.contains("先前更新任务尚未确认完成"),
              !displayed.contains("当前模式未确认") else {
            fputs("Stock planner error presentation smoke failed\n", stderr)
            exit(1)
        }
        let uncertainStock = stockCurrentFailureMessage([
            "phase": "settings", "error_code": "ValueError", "failure_cause": "journal unreadable",
            "write_may_have_occurred": NSNull(), "reboot_may_have_occurred": NSNull(),
            "confirmed_fragments": NSNull()])
        guard uncertainStock.contains("写入：本次写入状态无法核对"),
              uncertainStock.contains("重启：重启状态无法核对"),
              !uncertainStock.contains("本次未写入相机"),
              translatedText(uncertainStock, language: AppLanguage.english)
                .contains("Write: This task's write status could not be checked") else {
            fputs("Stock uncertain journal presentation smoke failed\n", stderr)
            exit(1)
        }
        guard currentFailureCause(["error_code": "safety_or_plan_check",
                                   "failure_cause": "raw user-area difference outside mapped partitions at sector 5000"]) ==
                "相机当前差异超出可恢复分区范围",
              currentFailureCause(["error_code": "version_structure_mismatch",
                                   "failure_cause": "recorded version 4.00 structurally mismatches profile"]) ==
                "相机固件结构与本次恢复方案不匹配",
              translatedText("相机当前差异超出可恢复分区范围", language: AppLanguage.english) ==
                "Current camera differences are outside the restorable partitions" else {
            fputs("Specific failure cause presentation smoke failed\n", stderr)
            exit(1)
        }
        do {
            let requestLog = FileManager.default.temporaryDirectory
                .appendingPathComponent("native-stock-fresh-\(UUID().uuidString).jsonl")
            defer {
                try? FileManager.default.removeItem(at: requestLog)
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "stock_fresh_failure", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            let model = RecoveryModel()
            model.page = .stock
            model.sdStep = .ready
            model.stockModelConfirmed = true
            model.stockCardConfirmed = true
            model.stockPowerConfirmed = true
            model.controlVerified = true
            func finish() {
                let deadline = Date().addingTimeInterval(8)
                while model.busy && Date() < deadline {
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
                }
            }
            model.startStock(); finish()
            guard model.showStockConfirmation else { throw NSError(domain: "StockFresh", code: 1) }
            model.showStockConfirmation = false // Simulate confirming the reviewed plan.
            model.runStock(); finish()
            guard model.showFreshStockPlanAction,
                  model.taskMessage.contains("阶段：准备存储卡恢复"),
                  model.taskMessage.contains("写入：本次未写入相机"),
                  translatedText(model.taskMessage, language: AppLanguage.english)
                    .contains("Write: No camera write in this task"),
                  !model.busy, model.stage.isEmpty else {
                throw NSError(domain: "StockFresh", code: 2)
            }
            model.startStock(); finish()
            guard model.showStockConfirmation else { throw NSError(domain: "StockFresh", code: 3) }
            model.showStockConfirmation = false
            model.runStock(); finish()
            let rows = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            let actions = rows.map { $0["action"] as? String ?? "" }
            let jobs = rows.filter { $0["action"] as? String == "prepare" }
                .compactMap { $0["output_dir"] as? String }
            guard actions == ["prepare", "stock_plan", "stock_run", "prepare", "stock_plan", "stock_run"],
                  jobs.count == 2, jobs[0] != jobs[1],
                  model.showStockCompletionButton, !model.showFreshStockPlanAction,
                  !model.busy, model.stage.isEmpty else {
                throw NSError(domain: "StockFresh", code: 4)
            }
        } catch {
            fputs("Stock fresh failure flow smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let requestLog = FileManager.default.temporaryDirectory
                .appendingPathComponent("native-stock-complete-\(UUID().uuidString).jsonl")
            defer {
                try? FileManager.default.removeItem(at: requestLog)
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "stock_ack", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            let model = RecoveryModel()
            model.page = .stock
            model.choosePreparedSDCard() // No DAT picker for a card already in slot 1.
            model.stockModelConfirmed = true
            model.stockPowerConfirmed = true
            model.controlVerified = true
            func finish() {
                let deadline = Date().addingTimeInterval(8)
                while model.busy && Date() < deadline {
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
                }
            }
            model.startStock(); finish()
            guard model.showStockConfirmation else { throw NSError(domain: "StockComplete", code: 1) }
            model.showStockConfirmation = false
            model.runStock(); finish()
            guard model.showStockCompletionButton else { throw NSError(domain: "StockComplete", code: 2) }
            model.confirmStockUpdateComplete(); finish()
            guard !model.showStockCompletionButton,
                  model.taskMessage.contains("建立新任务"),
                  translatedText(model.taskMessage, language: AppLanguage.english)
                    .contains("create a new task") else {
                throw NSError(domain: "StockComplete", code: 3)
            }
            model.startStock(); finish()
            let rows = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            let actions = rows.map { $0["action"] as? String ?? "" }
            let prepares = rows.filter { $0["action"] as? String == "prepare" }
            let jobs = prepares.compactMap { $0["output_dir"] as? String }
            let readyCardOnly = prepares.allSatisfy { request in
                let inputs = request["inputs"] as? [String: Any] ?? [:]
                return inputs["sd_ready"] as? Bool == true && inputs["dat"] == nil
            }
            guard actions == ["prepare", "stock_plan", "stock_run", "confirm_stock_update_complete",
                              "prepare", "stock_plan"],
                  jobs.count == 2, jobs[0] != jobs[1],
                  readyCardOnly,
                  model.showStockConfirmation, !model.showStockCompletionButton,
                  !model.busy, model.stage.isEmpty else {
                throw NSError(domain: "StockComplete", code: 4)
            }
        } catch {
            fputs("Stock completion to fresh task smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let requestLog = FileManager.default.temporaryDirectory
                .appendingPathComponent("native-sd-copy-proof-\(UUID().uuidString).jsonl")
            defer {
                try? FileManager.default.removeItem(at: requestLog)
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "sd_copy_verified", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            let model = RecoveryModel()
            model.page = .stock
            model.beginSDHelper()
            model.selectSDSource(URL(fileURLWithPath: "/tmp/offline-selected.DAT"))
            model.selectComputerSDVolume(URL(fileURLWithPath: "/Volumes/OfflineCard"))
            model.prepareSDCard()
            let deadline = Date().addingTimeInterval(8)
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.sdStep == .slot,
                  model.sdCopyProofText.contains("复制回执已保存"),
                  model.logs.contains("source_sha256=fake-source-sha256"),
                  model.logs.contains("target_sha256=fake-source-sha256"),
                  model.logs.contains("readback_verified=true"),
                  model.logs.contains("dat_metadata=not_parsed"),
                  model.logs.contains("slot_1_verified=false") else {
                throw NSError(domain: "SDCopyProof", code: 1)
            }
            model.stockCardConfirmed = true // Simulate the explicit physical slot-1 handoff.
            model.sdStep = .ready
            model.stockModelConfirmed = true
            model.stockPowerConfirmed = true
            model.controlVerified = true
            model.startStock()
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let rows = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            let prepare = rows.first { $0["action"] as? String == "prepare" }
            let inputs = prepare?["inputs"] as? [String: Any] ?? [:]
            guard rows.map({ $0["action"] as? String ?? "" }) ==
                    ["sd_card_prepare", "prepare", "stock_plan"],
                  inputs["sd_ready"] as? Bool == true,
                  inputs["sd_preparation_receipt"] as? String == "/tmp/fixture-sd-copy-receipt.json",
                  inputs["dat"] == nil,
                  model.showStockConfirmation else {
                throw NSError(domain: "SDCopyProof", code: 2)
            }
            model.sdCameraModel = "ILCE-7RM5"
            model.beginSDHelper(inCamera: true)
            model.selectSDSource(URL(fileURLWithPath: "/tmp/renamed.DAT"))
            model.selectComputerSDVolume(URL(fileURLWithPath: "/Volumes/OfflineCard"))
            model.prepareSDCard()
            let officialDeadline = Date().addingTimeInterval(8)
            while model.busy && Date() < officialDeadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let requests = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
            let last = try JSONSerialization.jsonObject(with: Data(requests.last!.utf8)) as? [String: Any]
            guard model.sdStep == .officialUpdate, requests.count == rows.count + 1,
                  last?["action"] as? String == "sd_card_prepare",
                  last?["camera_model"] as? String == "ILCE-7RM5" else {
                throw NSError(domain: "SDCopyProof", code: 3)
            }
            model.backSDStep()
            guard model.sdStep == .target else { throw NSError(domain: "SDCopyProof", code: 4) }
            model.choosePreparedSDCard()
            model.stockModelConfirmed = true
            model.stockPowerConfirmed = true
            model.controlVerified = true
            model.startStock()
            let r5Deadline = Date().addingTimeInterval(8)
            while model.busy && Date() < r5Deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let r5Requests = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            let r5Prepare = r5Requests.last { $0["action"] as? String == "prepare" }
            let r5Inputs = r5Prepare?["inputs"] as? [String: Any] ?? [:]
            guard r5Inputs["camera_model"] as? String == "ILCE-7RM5",
                  model.showStockConfirmation else {
                throw NSError(domain: "SDCopyProof", code: 8)
            }
            guard SDCamera.models.first(where: { $0.id == "ILCE-7RM5" })?.firmwareDownloadsURL?.absoluteString ==
                    "https://bluepotato.shop/a7r5images.html",
                  SDCamera.models.first(where: { $0.id == "ILCE-7M4" })?.firmwareDownloadsURL?.absoluteString ==
                    "https://bluepotato.shop/a74images.html",
                  SDCamera.models.count == 11,
                  Set(SDCamera.models.map(\.id)).count == 11 else {
                throw NSError(domain: "SDCopyProof", code: 5)
            }
            for camera in SDCamera.models where !camera.supportsAdapterRecovery {
                model.sdStep = .entry
                model.sdCameraModel = camera.id
                model.choosePreparedSDCard()
                model.startStock()
                guard model.sdStep == .entry, !model.busy else {
                    throw NSError(domain: "SDCopyProof", code: 6)
                }
                model.beginSDHelper()
                guard model.sdInCameraUpdate, model.sdStep == .source,
                      (camera.updateInstructions == SDCamera.fx6Instructions) == (camera.id == "ILME-FX6") else {
                    throw NSError(domain: "SDCopyProof", code: 7)
                }
            }
        } catch {
            fputs("SD copy proof propagation smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let requestLog = FileManager.default.temporaryDirectory
                .appendingPathComponent("native-stock-plan-error-\(UUID().uuidString).jsonl")
            defer {
                try? FileManager.default.removeItem(at: requestLog)
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "stock_plan_error", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            let model = RecoveryModel()
            model.page = .stock
            model.sdStep = .ready
            model.stockModelConfirmed = true
            model.stockCardConfirmed = true
            model.stockPowerConfirmed = true
            model.controlVerified = true
            model.startStock()
            let deadline = Date().addingTimeInterval(8)
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.taskMessage.contains("阶段：核对恢复方案"),
                  model.taskMessage.contains("写入：本次未写入相机"),
                  model.logs.contains("failed_check=settings_profile"),
                  model.showFreshStockPlanAction, model.sdBlockingStockJob == nil,
                  !model.busy, model.stage.isEmpty else {
                throw NSError(domain: "StockPlanError", code: 1)
            }
        } catch {
            fputs("Stock plan worker-error smoke failed: \(error)\n", stderr)
            exit(1)
        }
        let worker = WorkerClient()
        var stages: [String] = []
        var outcome: Result<[String: Any], Error>?
        worker.call(["action": "smoke"], progress: { event in
            stages.append(event["stage"] as? String ?? "")
        }, completion: { result in outcome = result })
        let deadline = Date().addingTimeInterval(5)
        while outcome == nil && Date() < deadline {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        guard case .success(let result) = outcome,
              result["status"] as? String == "READY", stages == ["probe"] else {
            fputs("JSONL bridge smoke failed\n", stderr)
            exit(1)
        }
        do {
            let model = RecoveryModel()
            model.page = .restore
            model.probeControlForTask()
            let deadline = Date().addingTimeInterval(5)
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let logFile = testLogDirectory.appendingPathComponent("native-session.log")
            var saved = ""
            while Date() < deadline {
                saved = (try? String(contentsOf: logFile, encoding: .utf8)) ?? ""
                if saved.contains("control=READY") && saved.contains("data=UNAVAILABLE") { break }
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard !model.busy, model.stage.isEmpty, model.controlVerified,
                  model.taskMessage.contains("相机恢复 DATA 未在此步骤验证"),
                  saved.contains("control_message=RP2040 PING verified"),
                  saved.contains("data_message=Camera RAM Fastboot unavailable"),
                  saved.contains("next_action=Enter RAM Fastboot through an explicit camera task") else {
                throw NSError(domain: "ConnectionProbe", code: 1)
            }
        } catch {
            fputs("Connection probe status/log smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-restore-language-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
            let requestLog = base.appendingPathComponent("requests.jsonl")
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_skip_nor", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            defer {
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            let model = RecoveryModel()
            model.page = .restore
            model.controlVerified = true
            model.powerConfirmed = true
            model.restoreTarget = base.appendingPathComponent("snapshot")
            model.startRestore()
            let deadline = Date().addingTimeInterval(10)
            while !model.restoreHasSkippedRegions && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let requests = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            let inputs = requests.first?["inputs"] as? [String: Any] ?? [:]
            guard model.restoreHasSkippedRegions,
                  model.taskMessage == "跳过区域：cp-nor",
                  translatedText(model.taskMessage, language: AppLanguage.english) == "Skipped regions: cp-nor",
                  requests.map({ $0["action"] as? String ?? "" }) == ["prepare", "restore_run"],
                  inputs["restore_profile"] as? String == "capability" else {
                throw NSError(domain: "RestoreLanguage", code: 1)
            }
            model.select(.stock)
            guard model.taskMessage.isEmpty else {
                throw NSError(domain: "RestoreMessageLeakedIntoStock", code: 1)
            }
            model.taskMessage = "SD 本次方案已准备"
            model.select(.restore)
            guard model.taskMessage == "跳过区域：cp-nor" else {
                throw NSError(domain: "RestoreMessageWasCleared", code: 1)
            }
            model.select(.stock)
            guard model.taskMessage == "SD 本次方案已准备" else {
                throw NSError(domain: "StockMessageWasReplaced", code: 1)
            }
        } catch {
            fputs("Restore language/partial-result smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-restore-fragment-partial-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_partial_fragments", 1)
            defer { unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO") }
            let model = RecoveryModel()
            model.page = .restore
            model.controlVerified = true
            model.powerConfirmed = true
            model.restoreTarget = base.appendingPathComponent("snapshot")
            model.startRestore()
            let deadline = Date().addingTimeInterval(10)
            while !model.restoreHasPartialOutcome && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.restoreHasPartialOutcome,
                  !model.restoreFailureObserved,
                  model.taskMessage == "恢复流程已完成，1处写入校验未通过",
                  translatedText(model.taskMessage, language: AppLanguage.english) ==
                    "Recovery flow completed; write verification failed at 1 location",
                  model.restoreSkippedLabel == "跳过区域：cp-nor" else {
                throw NSError(domain: "RestoreFragmentPartial", code: 1)
            }
        } catch {
            fputs("Restore fragment partial-result smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-restore-no-writable-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_no_writable_fragments", 1)
            defer { unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO") }
            let model = RecoveryModel()
            model.page = .restore
            model.controlVerified = true
            model.powerConfirmed = true
            model.restoreTarget = base.appendingPathComponent("snapshot")
            model.startRestore()
            let deadline = Date().addingTimeInterval(10)
            while !model.restoreHasPartialOutcome && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.restoreHasPartialOutcome,
                  model.taskMessage == "没有可写的恢复片段；请查看跳过区域",
                  translatedText(model.taskMessage, language: AppLanguage.english) ==
                    "No writable restore fragments; review skipped regions",
                  model.restoreSkippedLabel == "跳过区域：nflasha" else {
                throw NSError(domain: "RestoreNoWritable", code: 1)
            }
        } catch {
            fputs("Restore no-writable-result smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-fresh-snapshot-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            let old = base.appendingPathComponent("scoped_restore-historical", isDirectory: true)
            try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
            try Data("{\"status\":\"UNKNOWN_AFTER_WRITE\"}".utf8)
                .write(to: old.appendingPathComponent("historical-receipt.json"))
            let snapshot = base.appendingPathComponent("snapshot", isDirectory: true)
            let log = base.appendingPathComponent("requests.jsonl")
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_failure_then_fresh", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", log.path, 1)
            defer {
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            let model = RecoveryModel()
            model.page = .restore
            model.controlVerified = true
            model.powerConfirmed = true
            model.selectRestoreTarget(snapshot)
            model.startRestore()
            let deadline = Date().addingTimeInterval(10)
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.restoreFailureObserved,
                  model.taskMessage.contains("阶段：读取相机当前状态"),
                  model.taskMessage.contains("原因：相机 USB 读取中断"),
                  model.taskMessage.contains("进度：未开始写入相机"),
                  model.taskMessage.contains("相机模式：尚未确认返回普通模式"),
                  translatedText(model.taskMessage, language: AppLanguage.english)
                    .contains("Progress: No camera write started; Fully verified regions: 0."),
                  let firstJob = model.testRestoreJob else {
                throw NSError(domain: "FreshSnapshot", code: 1)
            }
            model.selectRestoreTarget(snapshot) // Selecting the same snapshot clears the old task association.
            guard model.testRestoreJob == nil, !model.restoreFailureObserved else {
                throw NSError(domain: "FreshSnapshot", code: 2)
            }
            model.startRestore()
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let rows = try String(contentsOf: log, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            guard rows.map({ $0["action"] as? String ?? "" }) ==
                    ["prepare", "restore_run", "prepare", "restore_run"],
                  model.testRestoreJob != firstJob, !model.restoreFailureObserved,
                  model.stage.isEmpty, !model.busy,
                  FileManager.default.fileExists(atPath: old.appendingPathComponent("historical-receipt.json").path) else {
                throw NSError(domain: "FreshSnapshot", code: 3)
            }
            let directLog = base.appendingPathComponent("direct-fresh.jsonl")
            setenv("CAMERA_RECOVERY_FAKE_LOG", directLog.path, 1)
            let direct = RecoveryModel()
            direct.page = .restore
            direct.controlVerified = true
            direct.powerConfirmed = true
            direct.selectRestoreTarget(snapshot)
            direct.startRestore()
            while direct.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard direct.restoreFailureObserved, let directOld = direct.testRestoreJob else {
                throw NSError(domain: "FreshSnapshot", code: 4)
            }
            direct.startRestore() // The failure-page fresh button uses this same action.
            while direct.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let directRows = try String(contentsOf: directLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            guard directRows.map({ $0["action"] as? String ?? "" }) ==
                    ["prepare", "restore_run", "prepare", "restore_run"],
                  direct.testRestoreJob != directOld, !direct.restoreFailureObserved, !direct.busy else {
                throw NSError(domain: "FreshSnapshot", code: 5)
            }
            let unknownLog = base.appendingPathComponent("unknown.jsonl")
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_journal_unknown", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", unknownLog.path, 1)
            let unknown = RecoveryModel()
            unknown.page = .restore
            unknown.controlVerified = true
            unknown.powerConfirmed = true
            unknown.selectRestoreTarget(snapshot)
            unknown.startRestore()
            while unknown.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard unknown.restoreFailureObserved,
                  !unknown.restoreHasSkippedRegions,
                  unknown.taskMessage.contains("进度：本次写入状态无法核对"),
                  !unknown.taskMessage.contains("未开始写入相机"),
                  unknown.logs.contains("write_may_have_occurred=unknown"),
                  unknown.logs.contains("Worker completed: action=restore_run;"),
                  unknown.logs.contains("exit_status=0; busy=false"),
                  unknown.stage.isEmpty, !unknown.busy else {
                throw NSError(domain: "FreshSnapshot", code: 6)
            }
        } catch {
            fputs("Fresh snapshot restore smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-restore-parent-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            let backup = base.appendingPathComponent("full_backup-parent", isDirectory: true)
            try FileManager.default.createDirectory(at: backup, withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: ["kind": "full_backup"])
                .write(to: backup.appendingPathComponent("task.json"))
            let requestLog = base.appendingPathComponent("requests.jsonl")
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            defer { unsetenv("CAMERA_RECOVERY_FAKE_LOG") }
            let model = RecoveryModel()
            model.selectRestoreTarget(backup)
            guard model.restoreTarget?.path == backup.path, model.errorText == nil else {
                throw NSError(domain: "RestoreParent", code: 1)
            }
            model.controlVerified = true
            model.powerConfirmed = true
            model.startRestore()
            let deadline = Date().addingTimeInterval(10)
            while model.restoreFinishedAt == nil && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let requests = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            let inputs = requests.first?["inputs"] as? [String: Any] ?? [:]
            guard requests.map({ $0["action"] as? String ?? "" }) == ["prepare", "restore_run"],
                  inputs["target_snapshot"] as? String == backup.path else {
                throw NSError(domain: "RestoreParent", code: 2)
            }
        } catch {
            fputs("Restore parent selection smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-backup-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            let first = base.appendingPathComponent("first", isDirectory: true)
            let second = base.appendingPathComponent("second", isDirectory: true)
            try FileManager.default.createDirectory(at: first, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: second, withIntermediateDirectories: true)
            let model = RecoveryModel()
            model.selectBackupDestination(first)
            let partial = first.appendingPathComponent("full_backup-partial").path
            model.testSeedBackupJob(partial)
            model.selectBackupDestination(first.appendingPathComponent("."))
            guard model.testBackupJob == partial else { throw NSError(domain: "BackupSelection", code: 1) }
            model.selectBackupDestination(second)
            guard model.testBackupJob == nil, model.outputDirectory?.path == second.path,
                  model.backupJobPath == nil else { throw NSError(domain: "BackupSelection", code: 2) }
            model.controlVerified = true
            model.powerConfirmed = true
            model.spaceConfirmed = true
            model.startBackup()
            let deadline = Date().addingTimeInterval(10)
            while model.busy && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            // Each worker result schedules the next action. Wait for the final receipt verification.
            while !model.backupEvidenceState.contains("已验证") && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.backupEvidenceState.contains("已验证"), model.testBackupJob == nil,
                  model.backupJobPath?.hasPrefix(second.path + "/") == true else {
                throw NSError(domain: "BackupSelection", code: 3)
            }
            let completed = model.backupJobPath
            model.startBackup()
            let secondDeadline = Date().addingTimeInterval(10)
            while model.backupJobPath == completed && Date() < secondDeadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.backupJobPath != completed,
                  model.backupJobPath?.hasPrefix(second.path + "/") == true else {
                throw NSError(domain: "BackupSelection", code: 4)
            }
            while (model.busy || !model.backupEvidenceState.contains("已验证")) && Date() < secondDeadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.backupEvidenceState.contains("已验证") else {
                throw NSError(domain: "BackupSelection", code: 5)
            }
        } catch {
            fputs("Backup destination/lifecycle smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-backup-block-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
            let requestLog = base.appendingPathComponent("requests.jsonl")
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "backup_stock_blocked", 1)
            setenv("CAMERA_RECOVERY_FAKE_LOG", requestLog.path, 1)
            defer {
                unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO")
                unsetenv("CAMERA_RECOVERY_FAKE_LOG")
            }
            let model = RecoveryModel()
            model.page = .backup
            let existing = base.appendingPathComponent("full_backup-existing", isDirectory: true)
            try FileManager.default.createDirectory(at: existing, withIntermediateDirectories: true)
            try Data("{\"kind\":\"full_backup\"}".utf8).write(to: existing.appendingPathComponent("task.json"))
            model.selectBackupDestination(existing)
            guard model.testBackupJob == existing.path,
                  model.outputDirectory?.path == base.path else {
                throw NSError(domain: "BackupBlock", code: 0)
            }
            model.controlVerified = true
            model.powerConfirmed = true
            model.spaceConfirmed = true
            model.startBackup()
            let deadline = Date().addingTimeInterval(10)
            while !model.taskMessage.contains("安全检查暂停") && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            guard model.sdBlockingStockJob == nil,
                  model.backupEvidenceState.contains("备份尚未完成"),
                  model.taskMessage.contains("2/5"), model.testBackupJob != nil else {
                throw NSError(domain: "BackupBlock", code: 1)
            }
            let blockedRequests = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            guard blockedRequests.map({ $0["action"] as? String ?? "" }) == ["reconcile", "apply"] else {
                throw NSError(domain: "BackupBlock", code: 3)
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "backup_read_interrupted", 1)
            model.startBackup()
            while !model.taskMessage.contains("相机 USB 读取中断") && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
            }
            let requests = try String(contentsOf: requestLog, encoding: .utf8).split(separator: "\n")
                .compactMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any] }
            guard model.sdBlockingStockJob == nil, model.testBackupJob != nil,
                  model.taskMessage.contains("连续重连 5/5"),
                  model.taskMessage.contains("累计重连 7 次"),
                  model.stage.isEmpty,
                  requests.map({ $0["action"] as? String ?? "" }) ==
                    ["reconcile", "apply", "reconcile", "apply"] else {
                throw NSError(domain: "BackupBlock", code: 2)
            }
        } catch {
            fputs("Backup blocked-outcome smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-restore-progress-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_progress", 1)
            defer { unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO") }
            let model = RecoveryModel()
            model.page = .restore
            model.controlVerified = true
            model.powerConfirmed = true
            model.restoreTarget = base.appendingPathComponent("snapshot")
            model.startRestore()
            var observed: [String] = []
            var localFractionObserved = false
            var bridgeFractionCleared = false
            var fragmentCountsObserved = false
            var planCountsObserved = false
            let deadline = Date().addingTimeInterval(10)
            while model.restoreFinishedAt == nil && Date() < deadline {
                if !observed.contains(model.stage) { observed.append(model.stage) }
                if model.stage.contains("nflasha5.target"), model.progressFraction == 0.5 {
                    localFractionObserved = true
                }
                if model.stage.contains("临时恢复程序已发送"), model.progressFraction == nil {
                    bridgeFractionCleared = true
                }
                if model.stage == "恢复计划已计算，正在准备写入…",
                   model.restorePlannedFragments == 2,
                   model.restoreVerifiedFragments == 0 {
                    planCountsObserved = true
                }
                if model.stage == "正在写入并校验片段…",
                   model.restorePlannedFragments == 2,
                   model.restoreVerifiedFragments == 1,
                   model.restoreFailedFragments == 0 {
                    fragmentCountsObserved = true
                }
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01))
            }
            guard localFractionObserved, bridgeFractionCleared,
                  planCountsObserved, fragmentCountsObserved,
                  observed.contains(where: { $0.contains("正在校验本机恢复文件") }),
                  observed.contains("相机读取阶段已结束，正在校验本机备份…"),
                  observed.contains("保存当前状态期间，正在安全地重新进入恢复模式…"),
                  observed.contains("正在确认相机恢复连接仍可读取…"),
                  observed.contains("本机校验期间，正在安全地重新进入恢复模式…"),
                  observed.contains("本机备份已核验，正在建立相机恢复连接…"),
                  observed.contains(where: { $0.contains("相机未报告就绪，正在第 2/5") }),
                  observed.contains("正在写入并校验片段…"),
                  observed.contains("正在核验本机已保存的片段读回证据…"),
                  model.logs.contains("restore_local_verify: Verifying saved restore files"),
                  model.logs.contains("restore_plan_summary: Planned 2 fragments; 1024 target bytes to write"),
                  model.logs.contains("fragment_write_verified: nflasha5 fragment 1 written and readback verified; 1/2 completed, 1 verified, 0 failed"),
                  model.logs.contains("saved_fragment_evidence: Saved local fragment readback evidence verified"),
                  translatedText("正在确认相机恢复连接仍可读取…", language: AppLanguage.english) ==
                    "Checking that the camera recovery connection still responds…",
                  translatedText("本机校验期间，正在安全地重新进入恢复模式…", language: AppLanguage.english) ==
                    "Safely reentering recovery mode during local checks…",
                  translatedText("保存当前状态期间，正在安全地重新进入恢复模式…", language: AppLanguage.english) ==
                    "Safely reentering recovery mode while saving the current state…",
                  model.stage.isEmpty else {
                throw NSError(domain: "RestoreProgress", code: 1)
            }
            model.selectRestoreTarget(base.appendingPathComponent("another-snapshot"))
            guard model.restorePlannedFragments == nil,
                  model.restoreVerifiedFragments == 0,
                  model.restoreFailedFragments == 0 else {
                throw NSError(domain: "RestoreProgressStaleCount", code: 1)
            }
        } catch {
            fputs("Restore progress stage smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-log-test-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base) }
            let source = base.appendingPathComponent("source")
            let taskLogs = source.appendingPathComponent("tasks")
            let stage = base.appendingPathComponent("stage")
            let job = base.appendingPathComponent("job")
            try FileManager.default.createDirectory(at: taskLogs, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: job, withIntermediateDirectories: true)
            for name in ["native-session.log", "session-test.jsonl", "updater.bin"] {
                try Data(name.utf8).write(to: source.appendingPathComponent(name))
            }
            for name in ["task-test.jsonl", "candidate.bin"] {
                try Data(name.utf8).write(to: taskLogs.appendingPathComponent(name))
            }
            try Data("{}".utf8).write(to: job.appendingPathComponent("journal.json"))
            try Data("{}".utf8).write(to: job.appendingPathComponent("nor-flow.json"))
            try FileManager.default.createDirectory(at: job.appendingPathComponent("nor-rebuild/collection"), withIntermediateDirectories: true)
            try Data("{}".utf8).write(to: job.appendingPathComponent("nor-rebuild/collection/collection.json"))
            try Data("secret-image".utf8).write(to: job.appendingPathComponent("nor-rebuild/collection/raw.bin"))
            try Data("secret-image".utf8).write(to: job.appendingPathComponent("updater.bin"))
            try stageSupportLogs(from: source, into: stage, taskDirectories: [job])
            let exported = Set(try FileManager.default.contentsOfDirectory(atPath: stage.path))
            guard exported == Set(["native-session.log", "session-test.jsonl", "task-test.jsonl", "receipts", "manifest.json"]),
                  FileManager.default.fileExists(atPath: stage.appendingPathComponent("receipts/task-1/journal.json").path),
                  FileManager.default.fileExists(atPath: stage.appendingPathComponent("receipts/task-1/nor-flow.json").path),
                  FileManager.default.fileExists(atPath: stage.appendingPathComponent("receipts/task-1/nor-rebuild/collection/collection.json").path),
                  !FileManager.default.fileExists(atPath: stage.appendingPathComponent("receipts/task-1/nor-rebuild/collection/raw.bin").path),
                  !FileManager.default.fileExists(atPath: stage.appendingPathComponent("receipts/task-1/updater.bin").path) else {
                throw NSError(domain: "NativeLogTest", code: 1)
            }
        } catch {
            fputs("Log export filter smoke failed: \(error)\n", stderr)
            exit(1)
        }
        do {
            let base = FileManager.default.temporaryDirectory.appendingPathComponent("native-nor-flow-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: base); unsetenv("CAMERA_RECOVERY_FAKE_SCENARIO") }
            try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
            for scenario in ["restore_nor_success", "restore_nor_failed"] {
                setenv("CAMERA_RECOVERY_FAKE_SCENARIO", scenario, 1)
                let model = RecoveryModel()
                model.page = .restore
                model.controlVerified = true
                model.powerConfirmed = true
                model.restoreTarget = base.appendingPathComponent("snapshot")
                model.startRestore()
                let deadline = Date().addingTimeInterval(10)
                var observed = Set<String>()
                while model.restoreFinishedAt == nil && Date() < deadline {
                    if model.restoreNORStartedAt != nil && model.busy {
                        guard !model.restoreFullyVerified, model.taskMessage.isEmpty,
                              model.progressFraction == nil else { throw NSError(domain: "NORPrematureSuccess", code: 1) }
                        observed.insert(model.stage)
                    }
                    RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01))
                }
                guard observed.contains("正在重建并校验启动数据，请保持供电和连接…"),
                      observed.contains("正在核对启动数据重建结果…"),
                      observed.contains("正在清理临时恢复入口…"),
                      observed.contains("正在返回普通相机模式…"),
                      model.restoreNORFinishedAt != nil,
                      !observed.contains("正在备份相机…"),
                      !observed.contains("正在写入并校验片段…"),
                      model.logs.contains("capture: Offline low-level NOR capture"),
                      model.restoreFullyVerified == (scenario == "restore_nor_success"),
                      !model.restoreHasSkippedRegions,
                      model.restoreFailureObserved == (scenario == "restore_nor_failed"),
                      translatedText("启动数据阶段已进行 01:31", language: AppLanguage.english) == "Startup data stage elapsed: 01:31",
                      translatedText("启动数据重建预计约 3 分钟；请保持供电和连接。", language: AppLanguage.english).contains("about 3 minutes") else {
                    throw NSError(domain: "NORFlowResult", code: 1)
                }
                model.selectRestoreTarget(base.appendingPathComponent("fresh-snapshot"))
                guard model.restoreNORStartedAt == nil, model.restoreNORFinishedAt == nil,
                      model.restoreNORStatus.isEmpty, !model.restoreFullyVerified else {
                    throw NSError(domain: "NORStaleState", code: 1)
                }
            }
            setenv("CAMERA_RECOVERY_FAKE_SCENARIO", "restore_nor_unsupported", 1)
            let unsupported = RecoveryModel()
            unsupported.page = .restore
            unsupported.controlVerified = true
            unsupported.powerConfirmed = true
            unsupported.restoreTarget = base.appendingPathComponent("snapshot")
            unsupported.startRestore()
            let deadline = Date().addingTimeInterval(10)
            while unsupported.restoreFinishedAt == nil && Date() < deadline {
                RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.01))
            }
            guard unsupported.restoreHasSkippedRegions, !unsupported.restoreFullyVerified,
                  !unsupported.restoreFailureObserved, unsupported.restoreNORStartedAt == nil,
                  unsupported.taskMessage.contains("本次未执行启动数据重建（NOR）"),
                  translatedText(unsupported.taskMessage, language: AppLanguage.english).contains("was not performed") else {
                throw NSError(domain: "NORUnsupported", code: 1)
            }
        } catch {
            fputs("Native NOR flow smoke failed: \(error)\n", stderr)
            exit(1)
        }
        print("JSONL bridge and log filter smoke passed")
    }
}
#else
private struct LanguageSelectionView: View {
    @AppStorage(AppLanguage.preference) private var language = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Choose your language / 选择语言").font(.title.bold())
            Text("You can change this later in the app. / 稍后可在程序中切换。")
                .foregroundStyle(.secondary)
            HStack(spacing: 12) {
                Button("简体中文") { language = AppLanguage.chinese }
                    .buttonStyle(.borderedProminent)
                Button("English") { language = AppLanguage.english }
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(32)
        .frame(minWidth: 500, minHeight: 250)
    }
}

@main private struct CameraRecoveryApp: App {
    @StateObject private var model = RecoveryModel()
    @AppStorage(AppLanguage.preference) private var language = ""
    var body: some Scene {
        WindowGroup {
            if !needsLanguageSelection(language) {
                ContentView(model: model)
            } else {
                LanguageSelectionView()
            }
        }
            .windowResizability(.contentSize)
    }
}
#endif
