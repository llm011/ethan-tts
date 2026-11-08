"""Central configuration. Runtime files are local and git-ignored."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _home_dir() -> Path:
    """用户级数据目录，和源码目录彻底分开。

    以前 data/ 和 models/ 都挂在源码旁边（ROOT），从 git clone 的本地开发跑没问题，
    但用 `uv tool install` 装出来时 ROOT 落在 site-packages 里——那里是只读的，
    模型权重（1.6 GB）和数据文件都会写失败。

    现在默认写到平台标准位置，`ETHAN_TTS_DIR` 可整体改；
    老版本留在源码旁边的 data/models 仍会被复用（见 _pick），避免升级后重复下载。
    """
    override = os.environ.get("ETHAN_TTS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ethan-tts"
    return Path.home() / ".local" / "share" / "ethan-tts"


def _pick(name: str, env_key: str, legacy: Path) -> Path:
    """选一个目录：显式环境变量 > 用户目录 > 源码旁边的老位置（它已经有内容时）。

    迁移友好：老用户 git clone 装的，models/ 里 1.6 GB 权重原样能用；
    新用户走用户目录，不碰源码。
    """
    explicit = os.environ.get(env_key)
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = _home_dir() / name
    if home.exists():
        return home
    if legacy.exists():
        return legacy
    return home


HOME_DIR = _home_dir()
DATA_DIR = _pick("data", "ETHAN_TTS_DATA_DIR", ROOT / "data")
DB_PATH = DATA_DIR / "ethan_tts.db"
ARTIFACT_DIR = DATA_DIR / "artifacts"
VOICE_DIR = DATA_DIR / "voices"
LOG_DIR = DATA_DIR / "logs"
MODEL_DIR = _pick("models", "ETHAN_TTS_MODEL_DIR", ROOT / "models")

for directory in (DATA_DIR, ARTIFACT_DIR, VOICE_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def _static_dir() -> Path:
    """定位网页界面目录。

    源码目录里是 ROOT/static；但用 pip/uv tool 装 wheel 时，data-files 被放到
    sys.prefix（venv 的根）而不是模块目录旁边——那样 app.py 里的 ROOT/"static"
    就是空的，页面打不开。这里按「源码优先、安装位置兜底」依次找。
    """
    candidates = [ROOT / "static", Path(sys.prefix) / "static"]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


STATIC_DIR = _static_dir()

DEFAULT_MODEL = "custom-0.6b-4bit"
SAMPLE_RATE = 24000
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MIN_REFERENCE_SECONDS = 3

# One registry for every weight this app can load. ``kind`` decides which
# sub-model the engine expects; ``role`` groups them in the settings page.
MODELS = {
    "custom-0.6b-4bit": {
        "name": "0.6B · CustomVoice · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit",
        "directory": "Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit",
        "kind": "custom",
        "role": "tts",
        "size_gb": 1.6,
        "description": "默认。预设音色与方言，速度和内存的最佳平衡。",
    },
    "design-1.7b-4bit": {
        "name": "1.7B · VoiceDesign · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "directory": "Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "kind": "design",
        "role": "tts",
        "size_gb": 0,
        "description": "可选。根据文字描述创建声音，比默认模型更占内存。",
    },
    "base-0.6b-4bit": {
        "name": "0.6B · Base · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        "directory": "Qwen3-TTS-12Hz-0.6B-Base-4bit",
        "kind": "clone",
        "role": "tts",
        "size_gb": 0,
        "description": "可选。用参考音频克隆音色，需要同时提供准确逐字稿。",
    },
    # Speech-to-text, used by the voice chat page before every model call.
    "qwen3-asr-0.6b-4bit": {
        "name": "Qwen3-ASR 0.6B · 4bit",
        "repo": "mlx-community/Qwen3-ASR-0.6B-4bit",
        "directory": "Qwen3-ASR-0.6B-4bit",
        "kind": "asr",
        "family": "qwen3-asr",
        "role": "asr",
        "size_gb": .9,
        "description": "默认。中英日韩多语种，短句识别准、延迟低。",
    },
    "whisper-turbo-4bit": {
        "name": "Whisper large-v3 turbo · 4bit",
        "repo": "mlx-community/whisper-large-v3-turbo-4bit",
        "directory": "whisper-large-v3-turbo-4bit",
        "kind": "asr",
        "family": "whisper",
        "role": "asr",
        "size_gb": .5,
        "description": "备选。跨语种稳健，英文口音和嘈杂环境更稳。",
        "extra_repo": {
            "repo": "openai/whisper-large-v3-turbo",
            "files": ["tokenizer*.json", "vocab.json", "merges.txt",
                      "preprocessor_config.json", "normalizer.json",
                      "added_tokens.json", "special_tokens_map.json"],
        },
    },
}

TTS_MODELS = {k: v for k, v in MODELS.items() if v["role"] == "tts"}
ASR_MODELS = {k: v for k, v in MODELS.items() if v["role"] == "asr"}

PRESET_SPEAKERS = [
    ("Vivian", "薇薇安", "中文女声，清晰自然", False, "Chinese"),
    ("Serena", "塞琳娜", "中文女声，温和流畅", False, "Chinese"),
    ("Uncle_Fu", "傅叔叔", "中文男声，沉稳", False, "Chinese"),
    ("Dylan", "迪伦", "北京话", True, "Chinese"),
    ("Eric", "埃里克", "四川话", True, "Chinese"),
    ("Ono_Anna", "小野安娜", "日语", False, "Japanese"),
    ("Sohee", "素熙", "韩语", False, "Korean"),
    ("Ryan", "瑞安", "英语男声", False, "English"),
    ("Aiden", "艾登", "英语男声", False, "English"),
]

DEFAULT_SETTINGS = {
    "default_voice_id": "preset:Vivian",
    "max_chars": 2000,
    "sentence_chars": 40,
    "streaming_interval": 0.32,
    "autoplay": True,
    "theme": "light",
}

# ---------------------------------------------------------------- voice chat
# Speech-to-text backends for the voice chat page. Qwen3-ASR is the default:
# small, fast, and notably stronger on Chinese than Whisper turbo.
STT_SAMPLE_RATE = 16000
DEFAULT_ASR_MODEL = "qwen3-asr-0.6b-4bit"

# Spoken-default system prompt. The voice page is conversational: a paragraph
# read aloud is unusable, so the prompt caps length first and only then asks
# for markdown/punctuation discipline that keeps TTS natural.
DEFAULT_SYSTEM_PROMPT = (
    "你是一个正在和用户语音对话的助手。你的回答会被立刻朗读出来，所以请始终遵守：\n"
    "1. 像面对面说话一样开口就答，不要寒暄铺垫，不要复述用户的问题。\n"
    "2. 一次回答控制在 1–3 句、一般不超过 80 字；只有用户明确要求展开时才多说。\n"
    "3. 只输出要朗读的纯文本：不要 Markdown、列表、标题、代码块、链接、表情符号，也不要括注。\n"
    "4. 数字、单位、缩写和英文单词尽量口语化（例如「下午三点」而不是「15:00」，"
    "「百分之二十」而不是「20%」）。\n"
    "5. 需要多步才能回答时，先给结论，再问用户要不要继续。\n"
    "6. 不确定就直说不确定，不要编造。\n"
    "7. 用用户说话的语言回答，默认中文。"
)

# Chunking and turn-taking knobs, kept here so the frontend can read them.
CHAT_DEFAULTS = {
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
    "llm_temperature": 0.7,
    "llm_max_tokens": 320,
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "voice_id": "preset:Vivian",
    "asr_model": DEFAULT_ASR_MODEL,
    "tts_min_chars": 6,
    "tts_max_chars": 60,
    "tts_first_chars": 8,
    "speech_rate": 1.0,
    "auto_send": True,
    "history_turns": 8,
}

CHAT_MODEL_PRESETS = [
    {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"label": "通义千问", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"label": "月之暗面 Kimi", "base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"label": "Ollama 本地", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b"},
    {"label": "LM Studio 本地", "base_url": "http://127.0.0.1:1234/v1", "model": "local-model"},
]

CHAT_SETTING_KEYS = tuple(CHAT_DEFAULTS)

# Turns kept per conversation in the LLM request. Voice exchanges are short,
# but a long session would still grow the prompt on every call.
HISTORY_TURNS = 8

# A user pressing stop must end the LLM/TTS work quickly; this is the poll
# interval of the cancellation flag inside the streaming loop.
CANCEL_POLL_SECONDS = .05
