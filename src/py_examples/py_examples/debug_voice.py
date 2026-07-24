#!/usr/bin/env python3
"""
debug_voice.py — 台湾腔语音助手 · 桌面调试版

在 Mac/Ubuntu 直接测试：麦克风 → ASR → TTS → 扬声器
调通后只需替换输入/输出端即可适配灵犀 X2。

使用:
  python3 debug_voice.py

依赖:
  pip install funasr edge-tts pydub pyaudio

按键:
  说话自动检测(VAD) → 识别 → 台湾腔回复
  Ctrl+C 退出
"""

import sys
import os
import time
import struct
import threading
import asyncio
import io
import re
import json
import urllib.request
import signal

# ── 音频 I/O ───────────────────────────────────────────
import pyaudio

# ── 配置 ────────────────────────────────────────────────
# TTS 后端: edge (微软) 或 seed (豆包)
TTS_BACKEND = os.environ.get("TTS_BACKEND", "edge")
SEED_API_KEY = os.environ.get("SEED_API_KEY", "")
VOICE = "zh-TW-HsiaoYuNeural"   # edge-tts 台湾腔
SAMPLE_RATE = 16000
CHUNK = 1600                     # 100ms @ 16kHz
FORMAT = pyaudio.paInt16
CHANNELS = 1
SILENCE_THRESHOLD = 500          # 静音门限（RMS 值）
SILENCE_DURATION = 0.8           # 持续静音秒数后判定说话结束（略短）
MIN_SPEECH_DURATION = 0.3        # 最短语音片段（秒），防误触


def clean_asr_text(text: str) -> str:
    """清理 SenseVoice 输出的特殊标签，只保留纯文本"""
    # 移除 <|...|> 标签
    text = re.sub(r'<\|[^|]+\|>', '', text)
    # 移除多余空白
    text = text.strip()
    return text


def make_reply(text: str) -> str:
    """构造自然流畅的台湾腔回复"""
    text = clean_asr_text(text)
    if not text:
        return ""
    from responder import generate_reply
    return generate_reply(text)


class DesktopVoiceDebug:
    """桌面调试版 — 纯本地麦克风 + 扬声器"""

    def __init__(self):
        self._running = True
        self._voice = VOICE
        self._pa = pyaudio.PyAudio()

        # ── ASR ──────────────────────────────────────────
        print("[⏳] Loading ASR model (第一次较慢)...")
        t0 = time.time()
        from funasr import AutoModel
        self._asr = AutoModel(
            model="iic/SenseVoiceSmall",
            device="cpu",
        )
        print(f"[✅] ASR loaded ({time.time()-t0:.1f}s)")

        # ── async 事件循环（给 edge-tts 用） ────────────
        self._loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

        # ── VAD 状态 ─────────────────────────────────────
        self._speech_buffer = bytearray()
        self._silent_sec = 0.0
        self._speaking = False
        self._processing = False

        print(f"\n{'='*50}")
        print(f"  台湾腔语音助手 · 桌面调试版")
        print(f"  TTS Voice: {self._voice}")
        print(f"  对麦克风说话试试… Ctrl+C 退出")
        print(f"{'='*50}\n")

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ════════════════════════════════════════════════════
    # 音频输入回调 (来自 PyAudio 麦克风)
    # ════════════════════════════════════════════════════

    def _mic_callback(self, in_data, frame_count, time_info, status):
        if self._processing:
            return (None, pyaudio.paContinue)

        # 计算 RMS 音量
        samples = struct.unpack_from(f"<{len(in_data)//2}h", in_data)
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5

        if rms > SILENCE_THRESHOLD:
            # ── 有声音 ──
            self._speech_buffer.extend(in_data)
            self._silent_sec = 0.0
            if not self._speaking:
                self._speaking = True
                print("\n[🎤] 说话中...", end="", flush=True)
        else:
            if self._speaking:
                self._silent_sec += CHUNK / SAMPLE_RATE
                self._speech_buffer.extend(in_data)
                print(".", end="", flush=True)

                if self._silent_sec >= SILENCE_DURATION:
                    # ── 说话结束 ──
                    self._speaking = False
                    self._processing = True
                    dur = len(self._speech_buffer) / SAMPLE_RATE / 2
                    print(f"\n[⏹️ ] 说话结束 ({dur:.1f}s)")

                    if dur > MIN_SPEECH_DURATION:
                        audio = bytes(self._speech_buffer)
                        self._speech_buffer = bytearray()
                        threading.Thread(
                            target=self._process,
                            args=(audio,),
                            daemon=True,
                        ).start()
                    else:
                        print("[💤] 太短，忽略")
                        self._speech_buffer = bytearray()
                        self._processing = False

        return (None, pyaudio.paContinue)

    # ════════════════════════════════════════════════════
    # ASR → TTS 管线
    # ════════════════════════════════════════════════════

    def _process(self, audio_bytes):
        t0 = time.time()

        # 1. ASR
        try:
            result = self._asr.generate(input=audio_bytes)
            raw_text = result[0]['text']
            asr_time = time.time() - t0
            # 清理标签后展示
            clean = clean_asr_text(raw_text)
            print(f"[🎤] ASR({asr_time:.2f}s): {clean}")
        except Exception as e:
            print(f"[❌] ASR error: {e}")
            self._processing = False
            return

        if not clean:
            self._processing = False
            return

        # 2. 构造自然回复
        reply = make_reply(raw_text)
        print(f"[💬] 回复: {reply}")

        # 3. TTS → 播放
        self._speak(reply)

    # ════════════════════════════════════════════════════
    # edge-tts → PyAudio 扬声器
    # ════════════════════════════════════════════════════

    def _speak(self, text):
        future = asyncio.run_coroutine_threadsafe(
            self._tts_and_play(text), self._loop)
        try:
            future.result(timeout=60)
        except Exception as e:
            print(f"[❌] TTS error: {e}")
        self._processing = False

    async def _tts_and_play(self, text):
        if TTS_BACKEND == 'seed':
            pcm_data = await self._tts_seed(text)
        else:
            pcm_data = await self._tts_edge(text)

        if not pcm_data:
            self._processing = False
            return

        # PyAudio 播放
        stream = self._pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
        )
        stream.write(pcm_data)
        stream.close()

        dur = len(pcm_data) // SAMPLE_RATE // 2
        print(f"[🔊] 播放完成 ({dur}s)")
        print(f"\n[🎤] 继续说话...", end="", flush=True)
        self._processing = False

    async def _tts_edge(self, text):
        """edge-tts 合成（微软）"""
        import edge_tts
        import pydub
        from pydub import AudioSegment
        import tempfile
        import os

        tmp = os.path.join(tempfile.gettempdir(), 'debug_tts.mp3')
        try:
            communicate = edge_tts.Communicate(text, self._voice, rate="+15%")
            await communicate.save(tmp)
            if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
                return None
            audio = AudioSegment.from_mp3(tmp)
            audio = audio.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS)
            pcm = audio.raw_data
            os.remove(tmp)
            return pcm
        except Exception as e:
            print(f"[❌] edge-tts error: {e}")
            return None

    async def _tts_seed(self, text):
        """豆包 Seed Audio TTS 合成"""
        import pydub
        from pydub import AudioSegment
        import io

        if not SEED_API_KEY:
            print("[❌] 请设置 SEED_API_KEY 环境变量")
            return None

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
                "speech_rate": 0,
            },
            "watermark": {},
        }

        data = json.dumps(request_json).encode("utf-8")
        req = urllib.request.Request(
            "https://openspeech.bytedance.com/api/v3/tts/create",
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Api-Key": SEED_API_KEY,
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=60)
            body = json.loads(resp.read())
            if body.get("code") != 80000:
                print(f"[❌] Seed Audio 错误: {body.get('message', 'unknown')}")
                return None

            import base64
            audio_b64 = body.get("data", {}).get("audio", "")
            if not audio_b64:
                return None

            mp3_data = base64.b64decode(audio_b64)
            audio = AudioSegment.from_mp3(io.BytesIO(mp3_data))
            audio = audio.set_frame_rate(SAMPLE_RATE).set_channels(CHANNELS)
            return audio.raw_data

        except Exception as e:
            print(f"[❌] Seed Audio error: {e}")
            return None

    # ════════════════════════════════════════════════════
    # 主循环
    # ════════════════════════════════════════════════════

    def run(self):
        stream = self._pa.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK,
            stream_callback=self._mic_callback,
        )
        stream.start_stream()

        try:
            while self._running:
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n[👋] 退出")
        finally:
            self._running = False
            stream.stop_stream()
            stream.close()
            self._pa.terminate()


if __name__ == "__main__":
    app = DesktopVoiceDebug()
    app.run()
