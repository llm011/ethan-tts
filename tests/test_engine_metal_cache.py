"""Metal 缓冲缓存的回归测试。

背景：批量合成（一次 40 段）跑到第 21 段时，进程 footprint 到 14.8 GB，
其中 14.4 GB 是 ``IOAccelerator (graphics)``，而 MLX 自报的 active 只有
1.92 GB。整机 24 GB 被顶爆，swap 吃到 22 GB、load 7+。

两个根因，各有一条测试锁住：

1. ``mx.set_cache_limit`` 的默认值是整机内存（这台机器上 24.4 GB），等于
   没上限——缓存池只增不减。
2. ``synthesize`` 每句生成完没有归还缓冲，批量任务里峰值随句数线性累积。

这些测试全部用替身，不加载真模型（套件约定：不跑真实推理）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

TEST_DATA = Path(tempfile.mkdtemp(prefix="ethan-tts-metal-test-"))
os.environ.setdefault("ETHAN_TTS_DATA_DIR", str(TEST_DATA))

import engine as engine_module  # noqa: E402


class FakeResult:
    """最小可用的生成结果：只需要 ``.audio``。"""

    def __init__(self, samples: int = 2400):
        self.audio = np.zeros(samples, dtype=np.float32)


class FakeModel:
    """design / clone / preset 三条路径都指向同一个计数器。"""

    def __init__(self):
        self.calls = 0

    def generate_voice_design(self, **_kwargs):
        self.calls += 1
        return [FakeResult()]

    def generate(self, **_kwargs):
        self.calls += 1
        return [FakeResult()]

    def generate_custom_voice(self, **_kwargs):
        self.calls += 1
        return [FakeResult()]


@pytest.fixture()
def clear_cache_spy(monkeypatch):
    """记录每次 clear_cache，并阻断对真实 MLX 的调用。"""
    calls: list[int] = []

    def spy():
        calls.append(1)

    monkeypatch.setattr(engine_module.mx, "clear_cache", spy)
    return calls


def _params(text: str, *, stream: bool = False) -> "engine_module.SynthesisParams":
    return engine_module.SynthesisParams(
        text=text,
        voice={"id": "v1", "name": "测试", "kind": "design", "language": "Chinese"},
        model_key="custom-0.6b-4bit",
        stream=stream,
    )


def test_metal_cache_limit_is_bounded():
    """缓存上限必须设下去，且明显小于整机内存。

    退化场景：把 ``_init_metal_cache_limit()`` 删掉、或把上限改回默认值，
    批量合成就会重新把整机内存吃光。
    """
    limit = engine_module.mx.set_cache_limit(engine_module._METAL_CACHE_LIMIT_BYTES)
    assert limit == engine_module._METAL_CACHE_LIMIT_BYTES
    # 24 GB 的机器上，2 GB 上限必须远小于整机内存，否则等于没限制。
    assert engine_module._METAL_CACHE_LIMIT_BYTES <= 4 * 1024**3
    assert engine_module._METAL_CACHE_LIMIT_BYTES > 0


def test_synthesize_clears_cache_once_per_sentence(monkeypatch, clear_cache_spy):
    """多句合成时，每句结束都要归还一次缓冲。

    退化场景：把 ``finally: mx.clear_cache()`` 删掉，这里会掉到 0 次，
    而批量任务的内存就会随句数线性增长——正是 14.4 GB 那个现场。
    """
    sentences = "第一句。第二句。第三句。"
    eng = engine_module.TTSEngine()
    model = FakeModel()
    monkeypatch.setattr(eng, "load", lambda _key: model)

    produced = list(eng.synthesize(_params(sentences)))

    assert model.calls == 3, "三句话应该触发三次生成"
    assert produced, "应该产出音频"
    assert len(clear_cache_spy) == 3, (
        f"每句结束都应调用一次 clear_cache，实际 {len(clear_cache_spy)} 次"
    )


def test_synthesize_clears_cache_even_when_generation_raises(monkeypatch, clear_cache_spy):
    """生成抛异常时也要回收，否则失败的任务会把缓冲留在池里。"""
    eng = engine_module.TTSEngine()

    class Boom:
        def generate_voice_design(self, **_kwargs):
            raise RuntimeError("上游炸了")

    monkeypatch.setattr(eng, "load", lambda _key: Boom())

    with pytest.raises(RuntimeError):
        list(eng.synthesize(_params("一句话。")))

    assert len(clear_cache_spy) == 1, "异常路径同样要归还缓冲"


def test_streaming_synthesis_also_clears_per_sentence(monkeypatch, clear_cache_spy):
    """流式路径同样要逐句回收（批量脚本走的就是流式分块）。"""
    eng = engine_module.TTSEngine()
    model = FakeModel()
    monkeypatch.setattr(eng, "load", lambda _key: model)

    list(eng.synthesize(_params("甲。乙。", stream=True)))

    assert model.calls == 2
    assert len(clear_cache_spy) == 2


def test_resource_snapshot_reports_cache_memory():
    """快照要带缓存池占用：泄漏时 active 可能很小而 cache 很大。"""
    snapshot = engine_module.TTSEngine().resource_snapshot()
    assert "metal_cache_gb" in snapshot
    assert "metal_cache_limit_gb" in snapshot
    assert snapshot["metal_cache_limit_gb"] == round(
        engine_module._METAL_CACHE_LIMIT_BYTES / 1e9, 2
    )
