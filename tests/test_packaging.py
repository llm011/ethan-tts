"""打包完整性：py-modules 必须覆盖所有顶层模块。

回归背景：新增 server_cmd.py / single_instance.py 后忘了加进 py-modules，
`uv tool install` 装出来的 CLI 直接
`ModuleNotFoundError: No module named 'setup_cmd'`——wheel 里根本没有这些文件。
setuptools 的 py-modules 是手工清单，不会自动发现，漏一个就废掉一个子命令。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _declared_modules() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    assert match, "pyproject.toml 里找不到 py-modules"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _actual_modules() -> set[str]:
    return {path.stem for path in ROOT.glob("*.py")}


def test_every_top_level_module_is_packaged():
    missing = _actual_modules() - _declared_modules()
    assert not missing, (
        f"这些顶层模块没写进 pyproject.toml 的 py-modules，"
        f"装出来的 CLI 会报 ModuleNotFoundError：{sorted(missing)}"
    )


def test_no_phantom_modules_declared():
    """声明了但文件不存在的模块会让 build 直接失败，同样要拦。"""
    phantom = _declared_modules() - _actual_modules()
    assert not phantom, f"py-modules 里声明了不存在的模块：{sorted(phantom)}"


def test_cli_imports_resolve_when_installed_layout():
    """模拟安装后的导入路径：cli 依赖的每个模块都必须能 import。"""
    import importlib

    for name in sorted(_declared_modules()):
        if name in {"app"}:
            continue  # app.py 会拉起 FastAPI/MLX，不适合放单测里 import
        importlib.import_module(name)
