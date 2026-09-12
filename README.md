# FiiO BT11 Linux Control

这是一个只依赖 Python 标准库的 Linux 控制面板，直接通过 BT11 的 USB HID
interface 1 工作。它不依赖 Chromium 的 WebHID，因此可以在 Linux 桌面直接使用。

## 当前支持的控制项

- 读取固件版本、当前 BT11 名称、连接状态和指示灯亮度
- 执行无写入 DFU 通道探测，确认 Linux HID 能访问固件升级接口
- 修改 BT11 的 Bluetooth 名称（最多 32 个 UTF-8 字节）
- 修改指示灯亮度（0–7）
- 选择可用 codec：LDAC、aptX Adaptive、aptX HD、aptX、aptX LL（SBC 是 BT11 的基础 codec）
- 选择 LDAC 模式：High Quality、Standard Quality、Mobile Quality
- 选择 aptX Adaptive 模式：Low Latency、High Quality、aptX Lossless
- 配对模式：关闭、自动、手动
- 读取、连接、断开和删除已配对设备
- 扫描附近设备，并将扫描结果配对后连接
- 清除全部配对记录、恢复 BT11 设置（GUI 有确认框，CLI 必须显式 `--yes`）

固件升级不是普通运行时设置。程序提供了显式确认的本地文件刷写入口，但不会自动
下载或选择固件；只有手动提供官方 `BT11.bin` 并添加 `--yes` 才会执行：

```text
bt11-control firmware-probe
bt11-control firmware-update /path/to/BT11.bin --yes
```

刷写期间不能拔出 BT11，也不应使用来历不明的固件。`firmware-probe` 已在当前设备
上验证，实际刷写未执行，以避免在没有必要升级时重写当前可用固件。

## 自动模式服务（systemd）

`bt11-auto-mode` 持续观察**真正送给 BT11 的音频**，据此选择 aptX Adaptive 模式：

| 播放内容 | 目标模式 |
| --- | --- |
| 无损 / 近无损，且源采样率 44.1 kHz | `19` aptX Lossless |
| 无损 / 近无损，其它采样率 | `3` High Quality |
| 有损内容 | `2` Low Latency |

判定方式（**只看音频本身**，不看是哪个播放器）：从 BT11 的 PipeWire monitor 抓 2 秒
PCM 做 FFT，在 10 kHz 以上按 **1 kHz 带宽**统计各带能量，取**相邻带之间的最大台阶**。
有损编码器在截止频率处是一条砖墙（台阶 50–70 dB 掉到数字底噪），而无损内容最多
只有几 dB 的自然滚降 —— 本机实测：无损 ≤ 6 dB、MP3 128k 66 dB、MP3 320k 52 dB、
AAC 128k 54 dB，门限取 **35 dB**（两侧余量 ≥29 dB）。台阶只有在位于
0.95 × 源奈奎斯特以下时才算数，因为 44.1 kHz 的源被重采样到 48 kHz 后也会在
22.05 kHz 处突然结束，那不是编码器造成的。

> 早期版本用"高频带 vs 中频带"的固定门限，那是错的：用粉噪声校准时能分开，
> 但真实音乐高频滚降远比粉噪声陡，结果真正的无损流（实测 Spotify 无损）被判成有损。
> 改成"找台阶"后不再依赖绝对电平。

源采样率取**正在播放的 PipeWire 流的 `node.rate`**（例如 `1/44100`），不是 BT11
设备的 48 kHz —— 设备速率是所有内容重采样后的结果，无法反映内容本身。

服务与唤醒方式：

- `bt11-auto-mode.path`（user unit）监视 BT11 的厂商 HID 节点
  `/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw`；
- **未插入 BT11 时只有这个 path 单元在**，服务完全不运行；
- 插入后 path 单元立刻拉起 `bt11-auto-mode.service`；
- 服务在设备拔出时自行退出，path 单元重新待命。

手动调试：

```text
bt11-auto-mode once              # 读一次并打印判定（不写入设备）
bt11-auto-mode once --apply      # 读一次并立即写入
bt11-auto-mode run --dry-run     # 跑服务循环但只打印
bt11-auto-mode self-test         # 判定逻辑 + 检测器自检（含合成砖墙）
bt11-auto-mode analyse file.wav [--content-rate 44100]
bt11-auto-mode --min-step 40 run
```

可调参数：`--min-step`（判为有损所需的台阶，dB，默认 35）、`--seconds`
（分析窗长，默认 1.2）、`--interval`（轮询间隔，默认 0.4）、`--debounce`
（连续多少次一致才切换，默认 3）、`--cooldown`（切换后的冷却，默认 2 s）。

**反应速度**：程序**持续**采集 monitor 到环形缓冲，每次轮询分析最近 1.2 s，
因此判定几乎无等待。实测本机：

| 环节 | 耗时 |
|---|---|
| BT11 HID 读 / 写（`bt11-control aptx-mode`） | 0.12 s / **0.14 s** |
| 检测到新内容并写入新模式（含静置与去抖） | **1.5–2.7 s**（早期版本约 18 s） |
| 之后的蓝牙链路重协商 | 由 BT11 + 耳机决定，主机侧无法测量 |

切换瞬间（换播放器、播放/停止）会有一段时间窗内是"新旧混合"的音频，因此程序在
**流集合发生变化后先静置一个窗口**再判定，并在写入后**冷却 2 s**，避免来回翻转
（早期版本确实会 19↔2 反复切换，日志可见）。
`analyse` 的 `--content-rate` 用于分析"重采样后的抓包"：不给出时会用文件自身速率，
44.1 kHz 源的上限就会被误当成砖墙。

已知限制：判据是"有没有编码器砖墙"，所以一份**本来就是有损转码的无损文件**
（例如从 128k MP3 转成的 FLAC）会被判成无损 —— 这是任何只看频谱的方法都无法
区分的；反过来，自然滚降极陡的内容理论上也可能误判，故门限留了 29 dB 余量。
空闲（没有流在播）时不改动当前模式。

## 运行

```text
python3 bt11_control.py gui
python3 bt11_control.py status
python3 bt11_control.py codecs
python3 bt11_control.py aptx-mode 19
python3 bt11_control.py ldac-mode 0
python3 bt11_control.py brightness 7
```

也可以用 `--device` 指定 HID 节点：

```text
python3 bt11_control.py --device /dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw status
```

## 权限

BT11 的当前系统节点通常是 root 专属的 `hidraw`。建议在 NixOS 配置中加入下面的
udev 规则，然后重新加载规则或重插 BT11：

```nix
services.udev.extraRules = ''
  SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0a12", ATTRS{idProduct}=="4007", \
    MODE="0660", GROUP="input", TAG+="uaccess"
'';
```

当前用户需要属于 `input` 组，或桌面会话需要给设备添加 `uaccess` ACL。没有规则时，
可以暂时用 root 运行 CLI 验证，但不建议把 GUI 长期作为 root 运行。

## 协议来源与边界

BT11 的 HID framing、命令号和字段布局来自 FiiO 官方 BT11 网页控制程序的公开
JavaScript，以及官方页面在 Linux 直接接入 USB 音频的说明。实现只复制了设备控制
协议，不包含 FiiO 的网页资源、账号逻辑或固件文件。

设备实际支持哪些 codec、配对设备能否连接，仍由 BT11 固件和耳机端能力决定；写入
codec 选择并不保证当前耳机会协商到该 codec。

BT11 官方 FAQ 明确说明 AAC 和 LHDC 不受支持，因此本程序不会把它们显示为可写的
选项；未知的原始 codec 字节仍会在 `status` 输出中保留为 `unknown(0x..)`, 便于发现
未来固件变化。

个别 BT11 1.1.x 固件在对“已经配对”的设备再次执行 `pair` 后，会把配对状态保持在
自动/手动状态，短时间内对 `close` 命令不生效；本程序会如实读回设备状态，不会假装
已经关闭。遇到这种固件状态机限制时，重新插拔 BT11，或用 FiiO Control 的配对管理页
关闭配对模式即可；这不会删除配对记录。
