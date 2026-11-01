# USB TLS 失败诊断

TLS 通道失败时，保留原始异常作为包装异常的 suppressed attachment，并在首次失败时记录原异常类型、消息、调用栈、源端口和时间。原异常自身的 cause 链保持不变。诊断限制为 4 层 cause、每层 12 个栈帧和 512 字符消息；消息中的控制字符替换为空格，诊断失败不影响连接清理。

USBMUX 仅观察非空接收包的序号。序号不符时记录期望/实际序号、长度、标志及累计计数，不输出包体；先记录前 8 次，之后只记录 2 的幂次事件，限制持续异常的日志量。

本补丁不修改 ACK、接收队列或重连策略，也不修复或确定 TLS 断线根因。异常消息由底层库提供，公开分享运行日志前仍需检查是否包含环境信息。补丁不更改日志保存位置与保留规则。

在固定 PlayPort 源码上应用 `patches/series` 后，执行：

```sh
./gradlew :protocol:test --tests '*TransportDiagnosticsTest'
./gradlew :protocol:test :server:test :server:installDist
```

11 项测试只使用人工异常与内存传输，覆盖原异常保留、首次失败、日志限长、循环 cause、诊断异常隔离、正常序号、重复/缺口/重叠、32 位回绕、限频和日志输出故障。测试不需要设备证书、配对文件或手机。
