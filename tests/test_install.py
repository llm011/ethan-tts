"""安装与自检：路径解析、打包完整性、setup/doctor 行为。

这批用例钉住的是**从 wheel 干净安装**才会暴露的问题——本地 git clone 跑永远发现不了：

1. `socket` 目录落在源码旁 vs. 用户目录：pip/uv 安装时源码目录是只读的，
   模型（1.6 GB）和数据必须写到用户可写位置。
2. `static/` 必须打进 wheel，否则装完打开 8765 是空白页；而且 data-files 会被装到
   `sys.prefix` 而不是模块旁边，所以运行时要按多路径查找。
3. 所有 import 到的第三方库都必须声明进 pyproject，否则干净安装直接 ModuleNotFoundError
   （实测漏过 httpx：服务起不来）。
"""
from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_all_third_party_imports_are_declared():
    """扫一遍源码里的 import，确认没有一个漏声明。

    这是能自动发现「本地能跑、干净安装挂掉」类问题的最省事办法：
    clone 开发时依赖是全的，只有从 wheel 装才会暴露漏声明。
    """
    local = {p.stem for p in ROOT.glob("*.py")}
    stdlib = set(sys.stdlib_module_names)
    imported: set[str] = set()
    for source in ROOT.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

    third_party = {
        name for name in imported
        if name not in stdlib and name not in local
    }
    declared = {
        # "mlx-audio==0.6.4" → mlx_audio；"uvicorn[standard]" → uvicorn
        spec.split("==")[0].split(">=")[0].split("[")[0].strip().lower().replace("-", "_")
        for spec in _pyproject()["project"]["dependencies"]
    }
    missing = third_party - declared
    assert not missing, (
        f"这些 import 没有在 pyproject.toml 的 dependencies 里声明：{sorted(missing)}"
        "——clone 开发时能跑，干净安装会 ModuleNotFoundError"
    )


def test_static_files_are_packaged():
    """static/ 必须出现在 wheel 的打包配置里，否则装完网页是空白。

    清单支持 glob（"static/*" 是最省事的写法），所以这里按 glob 匹配，
    而不是要求逐个文件名都字面出现。
    """
    import fnmatch

    data_files = _pyproject()["tool"]["setuptools"].get("data-files", {})
    patterns = [p for paths in data_files.values() for p in paths]
    on_disk = [p for p in (ROOT / "static").iterdir() if p.is_file()]
    missing = sorted(
        p.name for p in on_disk
        if not any(fnmatch.fnmatch(f"static/{p.name}", pattern) for pattern in patterns)
    )
    assert not missing, f"static/ 下这些文件没打进包：{missing}"


def test_setup_cmd_registered_as_module():
    """setup_cmd.py 是新模块，忘了加进 py-modules 就会装完找不到。"""
    modules = _pyproject()["tool"]["setuptools"]["py-modules"]
    assert "setup_cmd" in modules


def test_home_dir_respects_env(monkeypatch, tmp_path):
    """ETHAN_TTS_DIR 应能整体改掉数据位置——用户没法写默认目录时的唯一出路。"""
    import importlib

    import config

    monkeypatch.setenv("ETHAN_TTS_DIR", str(tmp_path / "custom"))
    reloaded = importlib.reload(config)
    try:
        assert reloaded.HOME_DIR == (tmp_path / "custom").resolve()
    finally:
        monkeypatch.delenv("ETHAN_TTS_DIR")
        importlib.reload(config)


def test_data_dir_is_outside_source_tree(monkeypatch):
    """默认数据目录不能落在源码目录里。

    clone 开发时无所谓，但 uv tool / pip 装出来的源码在 site-packages，
    往那儿写模型会 PermissionError。
    """
    import importlib

    import config

    monkeypatch.delenv("ETHAN_TTS_DIR", raising=False)
    monkeypatch.delenv("ETHAN_TTS_DATA_DIR", raising=False)
    monkeypatch.delenv("ETHAN_TTS_MODEL_DIR", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert not str(reloaded.HOME_DIR).startswith(str(config.ROOT))
    finally:
        importlib.reload(config)


def test_static_dir_finds_index(tmp_path, monkeypatch):
    """STATIC_DIR 必须真的指向一个含 index.html 的目录。"""
    import importlib

    import config

    reloaded = importlib.reload(config)
    try:
        assert (reloaded.STATIC_DIR / "index.html").is_file(), (
            f"STATIC_DIR={reloaded.STATIC_DIR} 里没有 index.html"
        )
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("assume_yes,expected", [(True, True), (False, False)])
def test_ask_honours_explicit_answer(monkeypatch, assume_yes, expected):
    """--yes / --download 时不再询问，直接按用户的选择走。"""
    from setup_cmd import _ask

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _ask("下载吗？", default=False, assume_yes=assume_yes) is expected


def test_ask_skips_in_non_interactive(monkeypatch):
    """非交互（agent / CI / 管道）必须按默认跳过，绝不能挂住等输入。"""
    from setup_cmd import _ask

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert _ask("下载吗？", default=False, assume_yes=None) is False


def test_doctor_does_not_import_engine():
    """doctor 存在的意义就是「依赖坏了也能诊断」，不能依赖 mlx。

    engine.py 顶层 import mlx，一旦 setup_cmd 间接 import engine，在没装 mlx 的
    环境里 doctor 会直接抛 ModuleNotFoundError——自检等于没用。
    """
    source = (ROOT / "setup_cmd.py").read_text(encoding="utf-8")
    assert "from engine import" not in source, "setup_cmd 不能 import engine（会拖进 mlx）"


def test_model_roots_covers_legacy_source_dir(tmp_path, monkeypatch):
    """升级用户：权重还在源码旁的 models/ 里，必须也能被找到。

    真实踩过：新版第一次启动创建了空的用户目录，之后「用户目录存在」就优先用它，
    源码旁已经下好的 4 GB 权重全显示成「未下载」，安装流程又白下一份。
    """
    import importlib

    import config

    monkeypatch.delenv("ETHAN_TTS_MODEL_DIR", raising=False)
    monkeypatch.setenv("ETHAN_TTS_DIR", str(tmp_path / "home"))
    reloaded = importlib.reload(config)
    try:
        roots = reloaded.MODEL_ROOTS
        assert len(roots) >= 1
        assert config.ROOT / "models" in roots, "源码目录必须留在候选里，否则老权重找不到"
    finally:
        monkeypatch.delenv("ETHAN_TTS_DIR")
        importlib.reload(config)


def test_has_content_ignores_empty_dir(tmp_path):
    """「建了但空」的目录不能被当成「用户已经用这个位置了」。"""
    import config

    empty = tmp_path / "empty"
    empty.mkdir()
    assert config._has_content(empty) is False

    filled = tmp_path / "filled"
    filled.mkdir()
    (filled / "x.txt").write_text("y")
    assert config._has_content(filled) is True


def test_setup_cmd_finds_model_in_any_root(monkeypatch, tmp_path):
    """doctor 的模型检测也要遍历所有根目录，不能只看 MODEL_DIR。

    否则从 clone 安装升级到 uv tool 安装的用户，doctor 会一直报「未下载」，
    而他其实什么都装好了。
    """
    import setup_cmd

    fake_root = tmp_path / "legacy"
    name = setup_cmd.MODELS[setup_cmd.DEFAULT_MODEL]["directory"]
    target = fake_root / name
    target.mkdir(parents=True)
    (target / "model.safetensors").write_bytes(b"x")

    monkeypatch.setattr(setup_cmd, "MODEL_ROOTS", [tmp_path / "empty-home", fake_root])
    assert setup_cmd._model_location(setup_cmd.DEFAULT_MODEL) == target
    assert setup_cmd.DEFAULT_MODEL in setup_cmd._installed_models()
