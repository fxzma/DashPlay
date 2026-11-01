# 热点无线参数

Debian 盒子的“盒子热点”页面增加频段、信道、频宽选择与当前实际运行状态。发射功率保持自动，不修改监管区域。只提供 `iw` 当前允许且 hostapd 配置生成器支持的组合，排除 DFS、禁用及 no-IR 信道。

修改前先启动独立于 DashPlay 服务的回退进程，再保存候选参数并应用。热点和 CarPlay 会短暂断开；重新连接后须在 180 秒内确认保留，否则恢复原参数。确认时核对实际 AP 的频段、信道和频宽。超时、手动恢复、回退进程失效或重启后未确认的状态都有恢复路径。

一次只允许一个待确认设置。等待确认或恢复期间，其他设置提交被暂时阻止。使用手机 Wi-Fi 热点上联时，盒子无线参数需跟随上游，因此独立设置不可用；USB 上网或独立热点模式可调整。待确认期间暂停新的 Wi-Fi 上联搜索，AP 健康恢复仍运行。OpenWrt 保持其原有网络管理入口。

无线切换与回退不能替代实机兼容性验证。单元测试覆盖状态、命令顺序及界面生成；监管规则、实际 WPA 加入和断电恢复仍应结合目标设备验收。

```sh
PYTHONPATH=scripts/onecloud:tests/onecloud python3 -m unittest test_hotspot_radio test_upgrade_radio_uplink test_phone_uplink test_settings_save
```
