#!/usr/bin/env python3
"""极简测试：DeepSeek 台湾腔 LLM → Seed Audio TTS → 播放"""

import os, sys, json, base64, io, urllib.request
SEED_KEY = os.environ.get("SEED_API_KEY", sys.argv[2] if len(sys.argv) > 2 else "")
LLM_KEY  = os.environ.get("LLM_API_KEY", "")
USER_INPUT = sys.argv[1] if len(sys.argv) > 1 else "哈囉你叫什麼名字"

if not LLM_KEY:
    raise RuntimeError("请先设置 LLM_API_KEY 环境变量")

# 1. LLM 生成台湾腔回复
llm_req = urllib.request.Request(
    "https://api.deepseek.com/chat/completions",
    data=json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你叫靈犀，用台灣腔簡短回覆，不超過20字，像朋友聊天，不要用表情符號。"},
            {"role": "user", "content": USER_INPUT}
        ],
        "max_tokens": 50,
    }).encode(),
    headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
)
reply = json.loads(urllib.request.urlopen(llm_req).read())["choices"][0]["message"]["content"].strip()
print(f"🧠 LLM: {reply}")

# 2. Seed Audio TTS
tts_req = urllib.request.Request(
    "https://openspeech.bytedance.com/api/v3/tts/create",
    data=json.dumps({
        "model": "seed-audio-1.0",
        "text_prompt": f"女子（台湾口音，嗓音甜美自然）用活泼亲切的语气说道：{reply}",
        "audio_config": {"format": "mp3", "sample_rate": 24000},
        "watermark": {},
    }).encode(),
    headers={"Content-Type": "application/json", "X-Api-Key": SEED_KEY},
)
body = json.loads(urllib.request.urlopen(tts_req).read())
mp3 = base64.b64decode(body.get("audio") or body.get("data", {}).get("audio", ""))
print(f"🔊 音频: {len(mp3)} bytes")

# 3. 播放
from pydub import AudioSegment
import pyaudio
pcm = AudioSegment.from_mp3(io.BytesIO(mp3)).set_frame_rate(16000).set_channels(1).raw_data
pa = pyaudio.PyAudio()
s = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, output=True)
s.write(pcm); s.close(); pa.terminate()
print("✅ 播放完成")
