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

判定方式（**只看音频本身**，不看是哪个播放器）：从 BT11 的 PipeWire monitor 抓
1.2 秒 PCM 做 FFT，在 8 kHz 以上按 **1 kHz 带宽**统计各带能量，取**相邻带之间的最大
台阶**（step）。有损编码器在截止频率处是一条砖墙（台阶几十 dB），无损内容即使音乐
偏暗也只是平缓滚降（实测 ≤4 dB）。台阶只统计到 **19 kHz**：44.1 kHz 的源被重采样到
48 kHz 后会在 22.05 kHz 结束，Chromium 的过渡带在 20–21 kHz 会产生一个 **19 dB** 的
假台阶，正好落进有损区间 —— 因此把 19 kHz 以上排除掉。

本机实测分布（服务日志 + 直接抓包）：

| 内容 | step |
|---|---|
| 无损 PCM 44.1 / 48 kHz | 0.3 – 0.5 dB |
| Chromium 播 44.1 kHz 无损 | 0.5 dB |
| **Spotify 无损（真实音乐，多轮实测）** | **3.8 – 15 dB**（中位约 5） |
| **Bilibili HiRes（Chromium，AAC）** | **22 – 55 dB** |
| MP3 128k / AAC 128k | 66 / 54 dB |

真实音乐把"粉噪声标定"给出的 18 dB 间隔压缩到约 **7 dB**（Spotify 最高 ~15 dB，
Bilibili 最低 ~22 dB），所以阈值必须放在这个窄缝里，并利用**代价不对称**：误进
Low Latency 会损失音质，而该进未进只是延迟略高、不损失音质 —— 因此偏向"不轻易进"。

**判据用可变阈值（施密特触发）**，两类之间留出 8–16 dB 的死区：

- **M_l**：进入 Low Latency 需要 step **≥ 20 dB**（质量明显变差才降级）；
- **M_h**：从 Low Latency 返回，需要 step **≤ 12 dB**（质量明显恢复才升级）；
- 落在 **12–20 dB** 之间**不改变当前模式** —— 这是防横跳的关键（每次切换都要重协商
  蓝牙链路，会听到中断）；死区也带来一个好处：Bilibili 只要有一段明确落墙就进入
  Low Latency，之后即便某几段台阶掉进死区也会**留在** Low Latency，不会来回跳。

按你的说法：切到 Low Latency 后要"音质达到更高的阈值 M_h"才切回，切到
High Quality/Lossless 后要"音质低到 M_l"才切到 Low Latency。

**采样率（44.1 vs 其它）是确定事实，因此每一轮都会重新比对**：只要当前模式不是该
采样率对应的那个（44.1 kHz → Lossless，其它 → High Quality），就会切过去 —— 不再
只在"离开 Low Latency 的那一刻"决定一次（那会导致一旦落到 High Quality 就再也升
不回 Lossless）。

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

可调参数：`--enter-ll`（M_l，进入 Low Latency 的台阶阈值，默认 16 dB）、
`--leave-ll`（M_h，返回所需的台阶阈值，默认 8 dB）、`--confirm`（新判定需持续的
秒数，默认 2.5）、`--seconds`（分析窗长，默认 1.2）、`--interval`（轮询间隔，
默认 0.4）、`--cooldown`（切换后的冷却，默认 3 s）。

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

已知限制（都是频谱类判据的固有歧义，死区保证它们至少"稳定"而非反复横跳）：
**1)** 从有损转码而来的"无损文件"会被判成无损；**2)** 截止频率高于 19 kHz 的有损
编码（如 320k AAC/MP3，墙在 20–21 kHz）会被判成无损 → 走 High Quality（只是延迟
略高，音质无损）；**3)** 母带本身只到 16–19 kHz 的无损文件会被判成有损。
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
