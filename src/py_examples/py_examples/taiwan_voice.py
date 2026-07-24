#!/usr/bin/env python3
"""
灵犀X2 × 台湾腔语音助手（豆包 Seed Audio）

流程:
  灵犀麦克风 → SenseVoice ASR → DeepSeek LLM → Seed Audio TTS → 灵犀扬声器

使用:
  ./run_taiwan_voice.sh

运行前:
  - 设置环境变量: SEED_API_KEY, LLM_API_KEY
  - 确保机器人已开机、网络通、ros2 topic list 能看到灵犀的 topic
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy, QoSDurabilityPolicy
from aimdk_msgs.msg import (
    ProcessedAudioOutput, AudioPlayback, AudioInfo, AudioData,
    FocusResponse, FocusRequester,
)
from aimdk_msgs.srv import RequestAudioFocus, AbandonAudioFocus
import threading
import asyncio
import io
import time
import re
import os
import json
import urllib.request


# ── TTS ─────────────────────────────────────────────────────
SEED_API_KEY = os.environ.get("SEED_API_KEY", "")


def clean_asr_text(text: str) -> str:
    """清理 SenseVoice 输出的特殊标签，只保留纯文本"""
    text = re.sub(r'<\|[^|]+\|>', '', text)
    return text.strip()


class TaiwanVoiceNode(Node):
    def __init__(self):
        super().__init__('taiwan_voice_node')
        self.pkg_name = 'taiwan_voice'

        # ── 参数 ──────────────────────────────────────────
        self.declare_parameter('low_latency', False)
        self._low_latency = self.get_parameter('low_latency').value

        self.get_logger().info(f'⚙️  low_latency={self._low_latency}')

        # ── ASR (SenseVoice) ──────────────────────────────
        if self._low_latency:
            self.get_logger().info('⏩ low_latency=True → 跳过 ASR，纯回声测试')
            self.asr = None
        else:
            self.get_logger().info('⏳ Loading ASR model (第一次较慢)...')
            try:
                from funasr import AutoModel
                self.asr = AutoModel(
                    model="iic/SenseVoiceSmall",
                    device="cpu",
                )
                self.get_logger().info('✅ ASR model loaded')
            except ImportError:
                self.get_logger().error(
                    '❌ funasr not installed. Run: pip install funasr')
                raise

        # ── 音频状态 ──────────────────────────────────────
        self.audio_buffer = bytearray()
        self.is_recording = False

        # ── 订阅灵犀麦克风 ────────────────────────────────
        # ProcessedAudioOutput 使用 BEST_EFFORT + VOLATILE
        mic_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=100,
            durability=QoSDurabilityPolicy.VOLATILE,
        )
        self.create_subscription(
            ProcessedAudioOutput, '/agent/process_audio_output',
            self.audio_cb, mic_qos)

        # ── 音频焦点与扬声器发布 ──────────────────────────
        self.focus_client = self.create_client(
            RequestAudioFocus, '/aimdk_5Fmsgs/srv/RequestAudioFocus')
        self.release_client = self.create_client(
            AbandonAudioFocus, '/aimdk_5Fmsgs/srv/AbandonAudioFocus')
        self.create_subscription(
            FocusResponse, '/aima/hal/audio/focus_response',
            self.focus_cb, 10)
        self.audio_pub = self.create_publisher(
            AudioPlayback, '/aima/hal/audio/playback', 10)
        self.has_focus = False

        # ── asyncio 事件循环线程 ──────────────────────────
        self.loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

        self.get_logger().info(
            f'✅ 台湾腔语音助手就绪 — 对灵犀说话试试吧！')

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ════════════════════════════════════════════════════════
    # 麦克风回调 (VAD 驱动)
    # ════════════════════════════════════════════════════════

    def audio_cb(self, msg: ProcessedAudioOutput):
        vad = msg.audio_vad_state.value  # 1=开始 2=说话中 3=结束
        data = bytes(msg.audio_data)

        if vad == 1:
            self.audio_buffer = bytearray(data)
            self.is_recording = True
        elif vad == 2:
            if self.is_recording:
                self.audio_buffer.extend(data)
        elif vad == 3:
            if self.is_recording:
                self.audio_buffer.extend(data)
                self.is_recording = False
                threading.Thread(
                    target=self._process_speech,
                    args=(bytes(self.audio_buffer),),
                    daemon=True).start()

    # ════════════════════════════════════════════════════════
    # ASR + TTS 处理链
    # ════════════════════════════════════════════════════════

    def _process_speech(self, audio_bytes):
        t0 = time.time()

        # 1. ASR 或回声
        if self._low_latency or self.asr is None:
            text = "測試語音回聲"
            self.get_logger().info(f'🔊 回声模式 (跳過ASR)')
        else:
            try:
                result = self.asr.generate(input=audio_bytes)
                raw_text = result[0]['text']
                cleaned = clean_asr_text(raw_text)
                self.get_logger().info(
                    f'🎤 ASR({time.time()-t0:.2f}s): {cleaned}')
            except Exception as e:
                self.get_logger().error(f'ASR error: {e}')
                return

        if not cleaned:
            return

        # 2. 构造回复
        from responder import generate_reply
        reply = generate_reply(cleaned)
        self.get_logger().info(f'💬 回复: {reply}')

        # 3. 提前获取音频焦点（在主线程安全地做 ROS2 操作）
        if not self._safe_request_focus():
            self.get_logger().warn('⚠️ 无法获取音频焦点，仍然尝试播放...')

        # 4. TTS 合成 → 播放
        self._speak(reply)

        # 5. 释放焦点
        self._safe_release_focus()

    # ════════════════════════════════════════════════════════
    # TTS (Seed Audio) → 灵犀扬声器
    # ════════════════════════════════════════════════════════

    def _speak(self, text):
        future = asyncio.run_coroutine_threadsafe(
            self._tts_and_play(text), self.loop)
        try:
            future.result(timeout=60)
        except Exception as e:
            import traceback
            self.get_logger().error(
                f'TTS error ({type(e).__name__}): {e}\n{traceback.format_exc()}')

    async def _tts_and_play(self, text):
        pcm_data = await self._tts_seed(text)

        if not pcm_data:
            return

        # 推流到灵犀扬声器（焦点已由 _process_speech 提前获取）
        msg = AudioPlayback()
        msg.pkg_name = self.pkg_name
        msg.token_id = self.pkg_name
        msg.info = AudioInfo()
        msg.info.channels = 1
        msg.info.sample_rate = 16000
        msg.data = AudioData()
        msg.data.data = pcm_data
        self.audio_pub.publish(msg)

        self.get_logger().info(
            f'🔊 播放完成 ({len(pcm_data)//32000}s)')

    async def _tts_seed(self, text):
        """豆包 Seed Audio TTS 合成"""
        import tempfile
        import os
        from pydub import AudioSegment

        api_key = os.environ.get("SEED_API_KEY", "")
        if not api_key:
            self.get_logger().error('❌ 请设置 SEED_API_KEY 环境变量')
            return None

        # 构造包含台湾口音的语音描述
        voice_prompt = (
            "女子（年轻女性，台湾口音，嗓音甜美自然，语气活泼親切）"
            "用自然流畅的语调说道"
        )

        request_json = {
            "model": "seed-audio-1.0",
            "text_prompt": f"{voice_prompt}：“{text}”",
            "audio_config": {
                "format": "mp3",
                "sample_rate": 24000,
                "pitch_rate": 0,
                "speech_rate": 0,
                "loudness_rate": 0,
            },
            "watermark": {},
        }

        data = json.dumps(request_json).encode("utf-8")
        req = urllib.request.Request(
            "https://openspeech.bytedance.com/api/v3/tts/create",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": api_key,
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=60)
            body = json.loads(resp.read())

            # 成功: 直接返回 {"audio": "base64..."}
            # 失败: 返回 {"code": 45000000, "message": "..."}
            if "code" in body and body["code"] != 80000:
                msg = body.get("message", body.get("msg", "unknown"))
                self.get_logger().error(f'Seed Audio 错误: {msg}')
                return None

            # 获取 base64 音频（可能在 top-level 的 audio 字段）
            import base64
            audio_b64 = body.get("audio", "")
            if not audio_b64 and "data" in body:
                audio_b64 = body["data"].get("audio", "")
            if not audio_b64:
                self.get_logger().error('Seed Audio 返回空音频')
                return None

            mp3_data = base64.b64decode(audio_b64)

            # MP3 → PCM (16kHz, 16bit, mono)
            audio = AudioSegment.from_mp3(io.BytesIO(mp3_data))
            audio = audio.set_frame_rate(16000).set_channels(1)
            pcm_data = audio.raw_data
            self.get_logger().info(
                f'🔊 Seed Audio 合成完成 ({len(pcm_data)//32000}s)')
            return pcm_data

        except Exception as e:
            self.get_logger().error(f'Seed Audio TTS error: {e}')
            return None

    # ════════════════════════════════════════════════════════
    # 音频焦点（在 _process_speech 线程中同步调用）
    # ════════════════════════════════════════════════════════

    def _safe_request_focus(self) -> bool:
        """同步请求音频焦点（在 _process_speech 线程中调用）"""
        if self.has_focus:
            return True
        if not self.focus_client.wait_for_service(timeout_sec=1.0):
            self.get_logger().warn('⚠️ 音频焦点服务不可用，跳过')
            return False
        req = RequestAudioFocus.Request()
        req.focus_requester = FocusRequester()
        req.focus_requester.pkg_name = self.pkg_name
        req.focus_requester.priority = 12
        future = self.focus_client.call_async(req)
        try:
            # 主线程的 rclpy.spin 会处理回调，我们只需等 future 完成
            response = future.result(timeout=3.0)
            if response is None:
                self.get_logger().warn('⚠️ 请求音频焦点超时')
                return False
            ok = response.response.status.value == 1
            if ok:
                self.has_focus = True
                self.get_logger().info('✅ 音频焦点已获取')
            else:
                self.get_logger().warn('⚠️ 音频焦点被拒绝')
            return ok
        except Exception as e:
            self.get_logger().warn(f'⚠️ 请求音频焦点异常: {e}')
            return False

    def _safe_release_focus(self):
        """同步释放音频焦点"""
        if not self.has_focus:
            return
        if not self.release_client.wait_for_service(timeout_sec=1.0):
            return
        req = AbandonAudioFocus.Request()
        req.focus_requester = FocusRequester()
        req.focus_requester.pkg_name = self.pkg_name
        future = self.release_client.call_async(req)
        try:
            future.result(timeout=2.0)
            self.has_focus = False
            self.get_logger().info('🔇 音频焦点已释放')
        except Exception:
            pass

    def focus_cb(self, msg: FocusResponse):
        if msg.pkg_name == self.pkg_name:
            self.has_focus = msg.focus_gain


def main(args=None):
    rclpy.init(args=args)
    node = TaiwanVoiceNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
