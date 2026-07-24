"""
responder.py — 灵犀台湾腔回复生成器

支持两种模式:
  1. 模板模式（默认）：关键词匹配，无需联网
  2. LLM 模式（可选）：通过 API 生成更自然的对话

使用 LLM 模式需设置环境变量:
  export LLM_API_KEY=your_api_key
  export LLM_API_URL=https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions
  # 阿里百炼 Qwen-flash 有免费额度
"""

import os
import random
import re
import threading
from typing import Optional


# ════════════════════════════════════════════════════════════
# 模板模式 — 关键词匹配，生成丰富的台湾腔回复
# ════════════════════════════════════════════════════════════

# ── 开场白（没说特定内容时） ──
GREETINGS = [
    "嘿！我在這呢～有什麼需要幫忙的嗎？",
    "嗨嗨！你找我喔？",
    "嗯？我在聽你說～",
    "來啦來啦！有什麼事儘管說～",
    "有！我在這裡～你今天心情怎麼樣啊？",
    "聽到囉～有什麼好玩的事要跟我分享嗎？",
    "嘿！我在這裡等你說話等好久了～",
    "喔？誰在叫我啊？",
]

# ── 肯定/同意 ──
AGREEMENT = [
    "對呀！我也是這麼覺得～",
    "沒錯沒錯！你說得太對了！",
    "真的耶！你也這樣想喔？",
    "好啊好啊！這個提議不錯～",
    "嗯嗯！我覺得可以～",
    "沒錯！完全同意你～",
    "對！你講到重點了～",
    "就是說啊！我也這麼認為！",
]

# ── 疑问 — 当检测到问句 ──
QUESTIONS = [
    "嗯～讓我想想喔⋯⋯這個問題很有意思耶！",
    "喔！這個問題問得好～我也還在研究說～",
    "好問題！不過我覺得你可以再跟我說多一點～",
    "哇，這個嘛⋯⋯你覺得呢？我想先聽聽你的想法～",
    "這個喔～我其實也滿好奇的耶！",
    "嗯～好問題！我來幫你想一想～",
    "這個嘛⋯⋯你要不要先跟我說你怎麼想的？",
    "哎唷，這個問題有點難耶！不過我喜歡～",
]

# ── 情感回应 ──
EMPATHY = [
    "哎呀～聽你這樣說，我也覺得⋯⋯",
    "哇賽！真的假的？好有趣喔！",
    "喔～原來是這樣啊！我明白了～",
    "哎唷！這樣很好啊～替你開心耶！",
    "嗯嗯～我在聽，你繼續說～",
    "哇！這麼酷的嗎？太厲害了吧！",
    "真的喔？好扯喔！然後呢然後呢？",
    "哎呀呀～這也太可愛了吧！",
]

# ── 道别 ──
FAREWELL = [
    "好啦～下次再聊囉！拜拜～",
    "好囉！我先休息一下，晚點再聊～",
    "掰掰～有事隨時叫我喔！",
    "好喔！那先這樣，等等見～",
    "OK！我先閃啦～拜拜！",
    "好囉！下次見～要想我喔！",
]

# ── 称赞 ──
PRAISE = [
    "哇！你好厲害喔！",
    "你也太強了吧！好佩服你～",
    "哎唷～不錯喔！真有你的！",
    "帥喔！這個厲害～",
    "哇賽！你怎麼這麼會啊！",
    "好棒棒！給你拍拍手～",
]

# ── 没听清 ──
MISHEARD = [
    "啊？不好意思我沒聽清楚，可以再說一次嗎？",
    "咦？我剛剛沒有聽好，可以再說一次嗎？",
    "嗯？可以再說一次嗎？我剛剛有點分心～",
    "啊？抱歉抱歉，你再說一次～",
    "蛤？我沒聽明白，再說一次好不好？",
]

# ── 关键词映射表 ──
KEYWORD_MAP = {
    # 打招呼
    "你好": GREETINGS,
    "嗨": GREETINGS,
    "哈囉": GREETINGS,
    "hello": GREETINGS,
    "hi": GREETINGS,
    "在嗎": GREETINGS,
    "在不在": GREETINGS,

    # 称赞你
    "好棒": PRAISE,
    "好厲害": PRAISE,
    "聰明": PRAISE,
    "可愛": PRAISE,
    "漂亮": PRAISE,
    "帥": PRAISE,
    "喜歡": PRAISE,

    # 道别
    "拜拜": FAREWELL,
    "再見": FAREWELL,
    "掰掰": FAREWELL,
    "下次見": FAREWELL,
    "先走": FAREWELL,
    "晚安": FAREWELL,
    "我先": FAREWELL,

    # 肯定
    "對": AGREEMENT,
    "好": AGREEMENT,
    "可以": AGREEMENT,
    "沒錯": AGREEMENT,
    "同意": AGREEMENT,
    "是喔": AGREEMENT,
    "真的": AGREEMENT,
    "對啊": AGREEMENT,

    # 情感 — 正向
    "開心": ["哇！開心最好了～我也被你感染了耶！",
             "太好了～開心很重要！要保持下去喔！",
             "嘻嘻～開心就好！我也很開心跟你聊天～"],
    "難過": ["哎唷～不要難過啦！我陪你～",
             "秀秀～不難過不難過，一切都會好起來的！",
             "沒事沒事～有我陪著你，難過的事很快就會過去的～"],
    "無聊": ["無聊喔？那我陪你聊天啊！",
             "無聊的時候找我準沒錯啦！我超會聊天的～",
             "不會無聊的啦！你看我不是在這裡嗎～"],
    "累": ["辛苦了～累了就休息一下沒關係的！",
           "哎呀～快去休息啦！身體要緊～",
           "你太努力了啦！先休息一下，等等再繼續～"],
    "餓": ["餓了喔？那快去吃点東西啊！",
           "我也餓了⋯⋯可是我又不能吃東西 QQ",
           "快去覓食吧！吃飽了才有力氣聊天～"],

    # 回应 — 通用填充
    "嗯": EMPATHY,
    "這樣": EMPATHY,
    "是喔": EMPATHY,
    "原來": EMPATHY,
    "然後": EMPATHY,
    "所以": EMPATHY,
    "但是": EMPATHY,
}

# ── 无匹配时的通用回应 ──
FALLBACK = [
    "喔～原來是這樣啊！我記下來了～",
    "嗯嗯！我聽到了～你說得很有道理耶！",
    "哎唷～這有趣！我喜歡～",
    "好喔好喔！我明白你的意思了～",
    "嗯～我懂我懂！你繼續說，我在聽～",
    "哇！原來你是在說這個啊！我懂了～",
    "好喔！我知道了～還有什麼要說的嗎？",
    "嗯嗯～我在認真聽你說呢！",
    "喔喔！原來是這樣～我學到了！",
    "收到收到！你講的我都有認真聽喔～",
]


def is_question(text: str) -> bool:
    """检测是否为疑问句"""
    # 以问号结尾或是疑问词开头
    q_words = ["什麼", "怎麼", "為什麼", "哪", "誰", "何時", "哪裡",
               "嗎", "呢", "吧", "有沒", "是不是", "會不會", "能不能",
               "可不可以", "要不要", "what", "how", "why", "which",
               "who", "when", "where", "can", "do", "does", "is"]
    if text.endswith("?") or text.endswith("？"):
        return True
    for w in q_words:
        if text.startswith(w) or w in text[:6]:
            return True
    return False


def generate_template_reply(text: str) -> str:
    """基于关键词匹配生成模板回复"""

    # 1. 检查关键词
    for keyword, replies in KEYWORD_MAP.items():
        if keyword in text:
            return random.choice(replies)

    # 2. 检查是否是问句
    if is_question(text):
        return random.choice(QUESTIONS)

    # 3. 如果文本很短（< 5个字），用通用回应
    if len(text) < 5:
        return random.choice(EMPATHY)

    # 4. 否则用随机回应
    return random.choice(FALLBACK)


# ════════════════════════════════════════════════════════════
# LLM 模式 — 调用大模型生成真正自然的对话
# ════════════════════════════════════════════════════════════

class LLMResponder:
    """通过 API 调用 LLM 生成回复"""

    def __init__(self):
        self._api_key = os.environ.get("LLM_API_KEY", "")
        self._api_url = os.environ.get(
            "LLM_API_URL",
            "https://api.deepseek.com/chat/completions",
        )
        self._model = os.environ.get("LLM_MODEL", "deepseek-chat")
        self._history = []
        self._ready = bool(self._api_key)

    @property
    def ready(self) -> bool:
        return self._ready

    def generate(self, user_text: str) -> Optional[str]:
        if not self._ready:
            return None

        import urllib.request
        import json

        # 保留最近 4 轮对话作为上下文
        self._history.append({"role": "user", "content": user_text})
        if len(self._history) > 10:
            self._history = self._history[-10:]

        system_prompt = (
            "你是一個名叫靈犀的台灣腔語音助手，說話要像真人一樣自然流暢。"
            "以下是規則：\n"
            "1. 用繁體中文回答，但回覆中不要出現標點符號以外的特殊符號\n"
            "2. 語氣要活潑親切，像朋友聊天一樣\n"
            "3. 回覆要簡短，不超過 30 個字\n"
            "4. 說話要用台灣口吻，例如：喔、啦、耶、啊、耶、吧、嗎\n"
            "5. 不要說自己是大模型或AI，你是靈犀機器人\n"
            "6. 不要使用任何表情符號或顏文字"
        )

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._history)
        messages.append({
            "role": "user",
            "content": f"請用台灣腔簡短回覆：{user_text}"
        })

        data = json.dumps({
            "model": self._model,
            "messages": messages,
            "max_tokens": 100,
            "temperature": 0.8,
        }).encode("utf-8")

        req = urllib.request.Request(
            self._api_url,
            data=data,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            resp = urllib.request.urlopen(req, timeout=10)
            body = json.loads(resp.read())
            reply = body["choices"][0]["message"]["content"].strip()

            # 清理：去掉可能的引号包裹
            reply = reply.strip("\"'「」『』")
            self._history.append({"role": "assistant", "content": reply})
            return reply
        except Exception as e:
            print(f"[LLM] API error: {e}")
            return None


# ════════════════════════════════════════════════════════════
# 对外接口
# ════════════════════════════════════════════════════════════

_llm = None


def get_responder():
    """获取回复生成器（单例）"""
    global _llm
    if _llm is None:
        _llm = LLMResponder()
    return _llm


def generate_reply(user_text: str) -> str:
    """生成回复，优先用 LLM，失败则降级到模板"""
    # 先尝试 LLM
    responder = get_responder()
    if responder.ready:
        reply = responder.generate(user_text)
        if reply:
            return reply

    # 降级到模板
    return generate_template_reply(user_text)


# ════════════════════════════════════════════════════════════
# 自测
# ════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== 灵犀回复生成器 自测 ===\n")

    test_cases = [
        "哈囉",
        "你好啊",
        "你好厲害",
        "你今天心情怎麼樣",
        "我肚子好餓",
        "我先走囉拜拜",
        "我今天好開心",
        "為什麼天空是藍色的",
        "嗯嗯",
        "你喜歡吃什麼",
    ]

    for t in test_cases:
        reply = generate_reply(t)
        print(f"  你: {t}")
        print(f"  靈犀: {reply}")
        print()
