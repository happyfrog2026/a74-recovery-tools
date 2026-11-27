# Third-party notices

The application's original host code is provided under [PolyForm Noncommercial 1.0.0](LICENSE). Each upstream component retains its own terms.

| Component | Integration | Notice |
| --- | --- | --- |
| DavidBuchanan314/ILCE-7M4-RE | Pinned submodule providing RP2040 and RAM recovery source and runtime references | Original notices in the submodule; copies in [third_party/licenses](third_party/licenses) |
| Host toolkit adaptations | Firmware container and FAT handling adapted from the fixed upstream revision in [UPSTREAMS.json](UPSTREAMS.json) | [MIT notice](third_party/licenses/host-toolkit-LICENSE) |
| PySquashfsImage 0.9.0 | Python dependency and corresponding source copy | [LGPL notice](third_party/vendor/PySquashfsImage-0.9.0/LICENSE.LGPL), [GPL text](third_party/vendor/PySquashfsImage-0.9.0/LICENSE.GPL3) |
| pyserial 3.5 | Python serial transport | Upstream BSD license |
| PyUSB 1.3.1 | Python USB transport | Upstream BSD license |
| cryptography 46.0.7 | Python cryptographic primitives | Upstream Apache-2.0/BSD license |
| NumPy 2.3.5 | Offline firmware resource decoding | Upstream BSD license |
| Electron | Windows desktop runtime | Upstream MIT license and bundled component notices |

Upstream revisions and resource identities are recorded in [UPSTREAMS.json](UPSTREAMS.json) and [upstream_resources.json](tools/upstream_resources.json). Generated resource provenance records identify the inputs used for a local preparation.

上游版本和资源校验值见上述清单。各组件保留原许可，生成资源的 provenance 记录本地准备所用输入。

（主项目许可适用于原始应用代码；第三方许可文本按原文保留。）

(The main project license applies to original application code; third-party license notices are retained verbatim.)
