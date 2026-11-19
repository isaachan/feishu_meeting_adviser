# Mac 飞书静音联动检查

`probe_recorder_mic.py` 在本机运行一段时间，验证飞书会议麦克风静音后，recorder
是否停止采集系统默认输入麦克风。脚本只统计音频块，不保存音频或连接云端。

先加入飞书会议，再从仓库根目录运行：

```sh
.venv-local/bin/python studies/macos-feishu-mute/probe_recorder_mic.py
```

运行期间按“飞书静音约 5 秒 → 取消静音并对系统默认麦克风说话约 5 秒 → 再次静音约 5 秒”
切换。每次切换后等待约 1 秒再判读。

| 飞书状态 | 预期输出 |
| --- | --- |
| 静音 | `known=True muted=True`、`采集=muted`、`非零音频块/秒=0` |
| 开麦并说话 | `known=True muted=False`、`采集=open`、`非零音频块/秒>0` |

正常情况下“检测”显示 `meeting_state`。`详情` 中的 `AX: 未找到可用的麦克风按钮`
表示飞书没有向辅助功能接口暴露该按钮；只要 `icon:` 正确识别图标，静音联动仍可工作。
图标检测通过底部工具栏的摄像头关闭图标定位麦克风；如果摄像头已打开或定位点缺失，
状态会保持未知，recorder 不采集本机麦克风。首次运行时 Swift 辅助程序需要编译，
开头可能暂时显示 `stale`。持续显示 `known=False` 时查看“详情”：窗口或图标识别不到时
本机麦克风保持关闭；`采集=error` 则检查麦克风设备和系统授权。

运行需要 macOS 的麦克风与屏幕录制权限。脚本使用仓库中的 recorder 源码，不依赖已安装的
recorder wheel。
