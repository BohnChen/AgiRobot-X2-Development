#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
x2_voice_chat —— 灵犀X2 语音对话程序

流程:本机麦克风(按回车说话) → vosk 离线中文识别
     → DashScope 大模型(黄惠杰老师人设) → 灵犀X2 TTS 播报

运行前:先 source ROS 2 与 SDK 环境(见 README.md)
用法:  python3 voice_chat.py
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

# ---------------------------------------------------------- 环境自检
def die(msg):
    print("\n[错误] " + msg)
    sys.exit(1)

try:
    import rclpy
    from rclpy.node import Node
except ImportError:
    die("找不到 rclpy。请先执行: source /opt/ros/humble/setup.bash")

try:
    from aimdk_msgs.srv import PlayTts   # noqa: F401
except ImportError:
    die("找不到 aimdk_msgs。请先 source SDK 环境:\n"
        "  source ~/aimdk-aarch64-a424add7-artifacts/install/local_setup.bash")

try:
    from vosk import Model, KaldiRecognizer, SetLogLevel
except ImportError:
    die("找不到 vosk。请安装: pip3 install vosk")

if not os.path.isdir(config.VOSK_MODEL_DIR):
    die("找不到语音识别模型目录:\n  %s\n请确认 models 文件夹与本脚本在一起。"
        % config.VOSK_MODEL_DIR)

if shutil.which("arecord") is None:
    die("找不到 arecord(录音工具)。请安装: sudo apt install alsa-utils")


# ---------------------------------------------------------- 语音识别
SetLogLevel(-1)          # 关闭 vosk/kaldi 日志


def record_and_recognize(model):
    """按回车开始录音,再按回车结束;返回识别出的中文文本。"""
    rec = KaldiRecognizer(model, config.SAMPLE_RATE)
    cmd = ["arecord", "-q", "-t", "raw", "-f", "S16_LE",
           "-r", str(config.SAMPLE_RATE), "-c", "1"]
    if config.ARECORD_DEVICE:
        cmd += ["-D", config.ARECORD_DEVICE]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as e:
        print("[错误] 启动录音失败: %s" % e)
        return ""

    time.sleep(0.3)
    if proc.poll() is not None:                    # arecord 立刻退出 = 打不开麦克风
        print("[错误] 无法打开麦克风。用 arecord -l 查看设备,"
              "并在 config.py 里设置 ARECORD_DEVICE(如 plughw:1,0)。")
        return ""

    segments = []

    def reader():
        while True:
            data = proc.stdout.read(3200)          # 0.1 秒一块
            if not data:
                break
            if rec.AcceptWaveform(data):
                seg = json.loads(rec.Result()).get("text", "")
                if seg:
                    segments.append(seg)
            else:
                p = json.loads(rec.PartialResult()).get("partial", "")
                if p:
                    print("\r  正在识别: %s        " % p, end="", flush=True)

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    input()                                        # 回车结束录音
    proc.terminate()
    th.join(timeout=3)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()

    tail = json.loads(rec.FinalResult()).get("text", "")
    if tail:
        segments.append(tail)
    print()
    text = "".join(segments).replace(" ", "").strip()
    for wrong, right in config.ASR_FIXES.items():   # 同音纠错
        text = text.replace(wrong, right)
    return text


# ---------------------------------------------------------- 大模型
LLM_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def llm_reply(history, user_text):
    """带多轮上下文调用 DashScope,返回回复文本;失败返回 None。"""
    messages = ([{"role": "system", "content": config.SYSTEM_PROMPT}]
                + history[-2 * config.MAX_HISTORY_TURNS:]
                + [{"role": "user", "content": user_text}])
    body = json.dumps({"model": config.LLM_MODEL,
                       "messages": messages}).encode("utf-8")
    req = urllib.request.Request(LLM_URL, data=body, headers={
        "Authorization": "Bearer " + config.DASHSCOPE_API_KEY,
        "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_SEC) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["choices"][0]["message"]["content"].strip()
    except (urllib.error.URLError, KeyError, TimeoutError, OSError) as e:
        print("[警告] 大模型请求失败: %s" % e)
        return None


# ---------------------------------------------------------- 机器人 TTS
class TtsClient(Node):
    """与官方 py_examples/play_tts.py 相同的调用方式。"""

    def __init__(self):
        super().__init__("x2_voice_chat_tts")
        self.client = self.create_client(PlayTts, config.TTS_SERVICE_NAME)
        n = 0
        while not self.client.wait_for_service(timeout_sec=2.0):
            n += 1
            print("  等待机器人 TTS 服务上线 ... (%d)" % n)
            if n % 5 == 0:
                print("  [提示] 一直等不到,请检查:是否已连接机器人网络、"
                      "是否能跑通官方例程 ros2 run py_examples play_tts")
        print("  TTS 服务已连接。")

    def speak(self, text):
        """播报 text;返回预计播报毫秒数(失败返回 None)。"""
        req = PlayTts.Request()
        req.tts_req.text = text
        req.tts_req.domain = config.TTS_DOMAIN
        req.tts_req.trace_id = "vc-%d" % int(time.time())
        req.tts_req.is_interrupted = True
        req.tts_req.priority_weight = 0
        req.tts_req.priority_level.value = 6      # INTERACTION_L6

        future = None
        for _ in range(8):                        # 官方例程同款重试
            req.header.header.stamp = self.get_clock().now().to_msg()
            future = self.client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=0.25)
            if future.done():
                break
        resp = future.result() if future else None
        if resp is None:
            print("[警告] TTS 调用超时。")
            return None
        if not resp.tts_resp.is_success:
            print("[警告] TTS 播报失败: %s" % resp.tts_resp.error_message)
            return None
        return resp.tts_resp.estimated_duration


def speak_and_wait(tts, text):
    """播报并等机器人说完(避免它自己的声音被话筒收进去)。"""
    ms = tts.speak(text)
    if ms is None:
        return
    if ms == 0:                       # 服务未给预计时长时按语速估算
        ms = len(text) * 250
    time.sleep(min(ms / 1000.0 + 0.5, config.TTS_MAX_WAIT_SEC))


# ---------------------------------------------------------- 主程序
def main():
    print("=" * 56)
    print("  灵犀X2 语音对话  |  人设:灵渠OS布道师 黄惠杰老师")
    print("=" * 56)

    print("[1/3] 加载语音识别模型 ...")
    model = Model(config.VOSK_MODEL_DIR)

    print("[2/3] 连接机器人 TTS 服务 ...")
    rclpy.init()
    tts = TtsClient()

    print("[3/3] 就绪!\n")
    if config.GREETING:
        print("开场白: %s" % config.GREETING)
        speak_and_wait(tts, config.GREETING)

    history = []
    try:
        while True:
            input("【按回车开始说话】(说完再按一次回车;说\"再见\"结束) ")
            print("  正在录音,请讲 ...(按回车结束)")
            text = record_and_recognize(model)

            if not text:
                print("  没有听清,请再试一次。\n")
                continue
            print("  你说: %s" % text)

            if any(w in text for w in config.EXIT_WORDS):
                speak_and_wait(tts, config.FAREWELL)
                break

            reply = llm_reply(history, text)
            if reply is None:
                speak_and_wait(tts, config.LLM_FAIL_SPEECH)
                continue

            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            print("  黄老师: %s\n" % reply)
            speak_and_wait(tts, reply)
    except (KeyboardInterrupt, EOFError):
        print("\n再见!")

    tts.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
