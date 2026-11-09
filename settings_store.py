"""Runtime knobs that are read once at import time.

Kept out of app.py so route modules can import them without importing the
whole application object (which would create an import cycle).
"""
from __future__ import annotations

import os

# 同步合成的排队上限：超过就返回 503，而不是让客户端无限等。
#
# 这个值只管「卡死」不管「慢」：本地克隆音色单节约 60s，书稿类管线一次并发
# 3–10 节，串行队列里最后一个正常也要排几分钟。实测 180s 会把正常批量误杀
# （第 3 节等到 120s+ 就被判超时）。默认放到 900s（15 分钟），够覆盖正常排队；
# 真卡住（模型 hang / 死循环）仍能在 15 分钟内报出来。
# 需要更快失败的环境（CI）用 ETHAN_TTS_QUEUE_TIMEOUT 调小。
QUEUE_TIMEOUT_SEC = float(os.environ.get("ETHAN_TTS_QUEUE_TIMEOUT", "900"))
