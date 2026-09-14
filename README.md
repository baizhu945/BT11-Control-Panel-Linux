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
- **启用/禁用自动模式选择**（即 bt11-auto-mode 服务是否按采样率改模式）
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

`bt11-auto-mode` **只读取正在送往 BT11 的 PipeWire 播放流采样率**，不抓取 PCM，
不检测音频质量、编码格式、码率，也不会自动选择 Low Latency：

| 已知播放采样率 | 目标模式 |
| --- | --- |
| 44.1 kHz（44100）或 88.2 kHz（88200） | `19` aptX Lossless |
| 其它正数采样率 | `3` High Quality |
| 没有播放流或所有流的采样率未知 | 不改变当前模式 |

服务每轮先读取 BT11 当前 aptX Adaptive 模式：

- 当前是 `2` Low Latency：自动逻辑暂停，不读取音频图，也不切回 High Quality/Lossless；
  服务继续等待，用户手动改回 `3` 或 `19` 后才恢复；
- 当前是 `3` 或 `19`：允许只在这两个模式之间按采样率切换；
- 当前模式未知或为其它值：为安全起见不写入；
- 写入前再次读取当前模式，避免覆盖用户刚刚手动选择的 Low Latency。

播放流来自连接到 BT11 sink 的、状态为 `running` 的 PipeWire links；采样率优先取
流的 `node.rate`，其次取 `audio.rate`。多个流同时播放时，优先采用最高的、不是
44.1/88.2 kHz 的已知采样率；如果没有其它速率，才采用最高的 44.1/88.2 kHz 速率。
因此所有已知流都是 44.1/88.2 kHz 才选 Lossless，只要存在其它已知采样率就选 High
Quality；所有流都未知则不作决定。

**手动开关**：GUI 里「Bluetooth 编码器」区域有一个复选框，CLI 用
`bt11-control auto-mode on|off|toggle`。开关状态保存在
`$XDG_STATE_HOME/bt11-control/auto-mode-disabled`（默认
`~/.local/state/bt11-control/auto-mode-disabled`）：文件存在 = 已禁用。服务每轮都读
它，禁用期间**只记录日志、不改模式**；`once --apply` 在禁用时会拒绝写入。拔掉 BT11
时也能切换，服务由 path 单元重新启动后仍遵守该状态。

服务与唤醒方式：

- `bt11-auto-mode.path`（user unit）监视 BT11 的厂商 HID 节点
  `/dev/input/by-id/usb-FIIO_FIIO_BT11__UAC1.0_-if01-hidraw`；
- **未插入 BT11 时只有这个 path 单元在**，服务完全不运行；
- 插入后 path 单元立刻拉起 `bt11-auto-mode.service`；
- 服务在设备拔出时自行退出，path 单元重新待命。

手动调试：

```text
bt11-auto-mode once              # 读一次采样率并打印判定（不写入设备）
bt11-auto-mode once --apply      # 读一次并立即写入 3/19（LL 时不写）
bt11-auto-mode run --dry-run     # 跑服务循环但只打印，不写入设备
bt11-auto-mode self-test         # 采样率映射与 LL 保护自检
bt11-auto-mode analyse file.wav [--content-rate 44100]
```

可调参数只有去抖和轮询参数：`--confirm`（新采样率判定需持续的秒数，默认 1）、
`--interval`（轮询间隔，默认 0.4）、`--cooldown`（切换后的冷却，默认 3 s）。服务
只查询 PipeWire 元数据，因此不会启动 `pw-record`，也不需要 numpy。

已知限制：播放器或 PipeWire 若已将不同源采样率重采样，服务只能看到重采样后的
`node.rate`；多个播放流混音时只能采用上述保守的非 Lossless 速率优先规则；未知采样
率时不改变模式。空闲（没有流在播）时同样不改变当前模式。

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
