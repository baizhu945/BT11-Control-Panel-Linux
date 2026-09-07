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
