"""Central configuration. Runtime files are local and git-ignored."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ETHAN_TTS_DATA_DIR", ROOT / "data")).resolve()
DB_PATH = DATA_DIR / "ethan_tts.db"
ARTIFACT_DIR = DATA_DIR / "artifacts"
VOICE_DIR = DATA_DIR / "voices"
LOG_DIR = DATA_DIR / "logs"
MODEL_DIR = Path(os.environ.get("ETHAN_TTS_MODEL_DIR", ROOT / "models")).resolve()

for directory in (DATA_DIR, ARTIFACT_DIR, VOICE_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)

DEFAULT_MODEL = "custom-0.6b-4bit"
SAMPLE_RATE = 24000
MAX_REFERENCE_BYTES = 20 * 1024 * 1024

MODELS = {
    "custom-0.6b-4bit": {
        "name": "0.6B · CustomVoice · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit",
        "directory": "Qwen3-TTS-12Hz-0.6B-CustomVoice-4bit",
        "kind": "custom",
        "description": "默认。预设音色与方言，速度和内存的最佳平衡。",
    },
    "design-1.7b-4bit": {
        "name": "1.7B · VoiceDesign · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "directory": "Qwen3-TTS-12Hz-1.7B-VoiceDesign-4bit",
        "kind": "design",
        "description": "可选。根据文字描述创建声音，比默认模型更占内存。",
    },
    "base-0.6b-4bit": {
        "name": "0.6B · Base · 4bit",
        "repo": "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-4bit",
        "directory": "Qwen3-TTS-12Hz-0.6B-Base-4bit",
        "kind": "clone",
        "description": "可选。用参考音频克隆音色，需要同时提供准确逐字稿。",
    },
}

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
