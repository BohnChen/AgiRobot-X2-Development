# x2_voice_chat —— 灵犀X2 语音对话程序

对着上位机麦克风说话 → 本机离线识别(vosk)→ DashScope 大模型生成回答
(人设:灵渠OS布道师 黄惠杰老师)→ 灵犀X2 通过 TTS 说出来。

## 1. 前提

- 上位机 Ubuntu 22.04 + ROS 2 Humble,且 **X2 AimDK 已 `colcon build` 完成**
- 已按官方文档连好机器人(能跑通官方例程 `ros2 run py_examples play_tts` 即可)
- 上位机有麦克风、能上外网(大模型调用需要)

## 2. 安装(仅一条)

```bash
pip3 install vosk
```

> 录音用系统自带的 `arecord`,若提示缺失:`sudo apt install alsa-utils`
> 语音识别模型已打包在 `models/` 里,无需下载。

## 3. 运行

```bash
source /opt/ros/humble/setup.bash
source ~/aimdk-aarch64-a424add7-artifacts/install/local_setup.bash   # 按实际 SDK 路径
cd x2_voice_chat
python3 voice_chat.py
```

## 4. 用法

1. 启动后机器人先说开场白;
2. **按回车开始说话,说完再按一次回车**;
3. 识别文字、老师的回答都会打印在终端,机器人同步朗读;
4. 对机器人说 **"再见" / "退出"** 结束程序(或 Ctrl+C)。

## 5. 可调项(都在 `config.py` 顶部)

| 项 | 说明 |
|---|---|
| `DASHSCOPE_API_KEY` | 大模型 API Key |
| `LLM_MODEL` | `qwen-plus`(默认)/ `qwen-turbo`(更快)/ `qwen-max`(更强) |
| `SYSTEM_PROMPT` / `GREETING` | 人设卡与开场白 |
| `ARECORD_DEVICE` | 麦克风设备,默认系统默认设备;多麦克风时可填 `plughw:1,0` 等(用 `arecord -l` 查看) |

## 6. 常见问题

- **一直"等待机器人 TTS 服务上线"** → 先确认官方 `play_tts` 例程能跑通(网络、SDK 环境)。
- **识别不准** → 靠近麦克风、吐字清晰;或到 [vosk 官网](https://alphacephei.com/vosk/models) 换大模型 `vosk-model-cn-0.22`(1.3GB),解压后改 `config.py` 里的 `VOSK_MODEL_DIR`。
- **大模型请求失败** → 检查外网连通性与 API Key 余额。
