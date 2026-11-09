"""Central configuration. Runtime files are local and git-ignored."""
from __future__ import annotations

import json
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


def _has_content(path: Path) -> bool:
    """目录里到底有没有东西——只看 exists() 会被「建了但空」的目录骗到。

    这个坑真实发生过：新版本第一次启动时创建了空的用户目录，之后判定
    「用户目录存在」就优先用它，于是源码旁已经下好的 4 GB 权重全部显示成
    「未下载」，安装流程又白白重下了一份。
    """
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def _pick(name: str, env_key: str, legacy: Path) -> Path:
    """选目录：显式环境变量 > 已经有内容的用户目录 > 有内容的源码目录 > 用户目录。

    迁移友好：老用户 clone 出来的 models/ 原样能用，不会因为升级而重新下载；
    新用户走用户目录，不碰源码目录。
    """
    explicit = os.environ.get(env_key)
    if explicit:
        return Path(explicit).expanduser().resolve()
    home = _home_dir() / name
    if _has_content(home):
        return home
    if _has_content(legacy):
        return legacy
    return home


HOME_DIR = _home_dir()


MODEL_ROOTS_FILE = HOME_DIR / "model-roots.json"


def _remembered_model_roots() -> list[Path]:
    """以前用过的权重目录（跨安装形态记住）。

    为什么需要它：从 clone 安装换成 uv tool 安装后，`ROOT` 变成 site-packages，
    `ROOT / "models"` 指向一个根本不存在的地方——源码旁那 4 GB 权重就彻底找不到了。
    所以源码运行时把「我的 models 在这」写下来，装成 wheel 后还能顺着记录找回去。
    """
    try:
        raw = json.loads(MODEL_ROOTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    roots: list[Path] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            path = Path(item).expanduser().resolve()
        except (TypeError, ValueError):
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def remember_model_root(path: Path) -> None:
    """记下这个目录里有权重，供之后（可能换了安装形态的）进程复用。"""
    path = path.resolve()
    known = _remembered_model_roots()
    if path in known:
        return
    known.append(path)
    try:
        MODEL_ROOTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODEL_ROOTS_FILE.write_text(
            json.dumps([str(item) for item in known], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass  # 记不住不算错，下次照样能靠显式环境变量


def model_roots() -> list[Path]:
    """按优先级列出可以放权重的目录。

    权重动辄数 GB，且老用户手里已经下好了一堆。单靠「哪个目录有内容」
    会做出错误判断：新目录里只重下了一个模型，源码旁另外三个就被判成
    「未下载」。所以按模型逐个找：谁先有这个模型就先用谁的。

    顺序：显式环境变量 > 用户目录 > 历史记录过的目录 > 当前代码旁。
    """
    explicit = os.environ.get("ETHAN_TTS_MODEL_DIR")
    if explicit:
        return [Path(explicit).expanduser().resolve()]
    roots: list[Path] = []
    for root in [MODEL_DIR, *_remembered_model_roots(), ROOT / "models"]:
        if root not in roots:
            roots.append(root)
    return roots


DATA_DIR = _pick("data", "ETHAN_TTS_DATA_DIR", ROOT / "data")
DB_PATH = DATA_DIR / "ethan_tts.db"
ARTIFACT_DIR = DATA_DIR / "artifacts"
VOICE_DIR = DATA_DIR / "voices"
LOG_DIR = DATA_DIR / "logs"
# 单实例锁：服务启动时独占，防止并发 CLI 各自 Popen 出多个 app.py。
# 刻意和 server_cmd.PID_PATH 分开：那个文件存的是「Popen 出来的 pid」，
# 供 start/stop 用；这个文件只做 flock 互斥，写进去的是持锁进程自己的 pid。
# 两个模块共用同一路径会互相覆盖，语义也容易混淆。
PID_FILE = DATA_DIR / "server.lock"
MODEL_DIR = _pick("models", "ETHAN_TTS_MODEL_DIR", ROOT / "models")
MODEL_ROOTS = model_roots()

# 源码运行时，把「权重就在我旁边」这件事记下来：之后用 uv tool / pip 装成 wheel，
# ROOT 会变成 site-packages，靠这条记录才找得回源码旁的旧权重。
if _has_content(ROOT / "models"):
    remember_model_root(ROOT / "models")

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
        "size_gb": 1.7,
        "description": "默认。预设音色与方言，速度和内存的最佳平衡。",
    },
    "design-1.7b-4bit": {
        "name": "1.7B · VoiceDesign · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "directory": "Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "kind": "design",
        "role": "tts",
        "size_gb": 2.3,
        "description": "可选。根据文字描述创建声音，比默认模型更占内存。",
    },
    "base-0.6b-4bit": {
        "name": "0.6B · Base · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        "directory": "Qwen3-TTS-12Hz-0.6B-Base-4bit",
        "kind": "clone",
        "role": "tts",
        "size_gb": 1.7,
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
        "size_gb": .7,
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
    # 默认直连。系统开着代理时，httpx 会把 API 请求也塞进去导致连接被重置。
    "llm_use_proxy": False,
    "llm_proxy_url": "",
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
