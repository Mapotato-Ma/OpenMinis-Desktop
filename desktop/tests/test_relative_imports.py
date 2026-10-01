"""相对导入的层级必须是活的 —— 这条守卫是拿真事故换来的。

## 事故（2026-10-01，用户报「配了 DeepSeek 的 key，选 openai 也是调用失败」）

`src/openminis/provider/openai/openai_provider.py` 里写着：

```python
from ..core.repeat_diag import StreamRepeatWatch
```

而这个文件在 `openminis/provider/openai/` 里，比 `openminis` **低三层** ——
`..core` 只到 `openminis.provider`，于是去找**不存在**的 `openminis.provider.core`。
`src/openminis/core/repeat_diag.py` 一直都在，只是**少了一个点**。

后果不是"诊断功能失效"，而是：**所有 OpenAI 兼容的服务商**（OpenAI / 七牛云 /
DeepSeek / 自建网关）一到流式那一步就 `ModuleNotFoundError`，对话根本走不下去。
从 v0.4.1 到 v0.4.9，跨了九个版本没人发现，因为：

* 单元测试把 provider 换成了假的（不去碰真流式循环）；
* 内核自带的 `tests/test_repeat_diag.py` 直接测那个模块，import 路径是对的；
* CI 只探活，从不真的发一句话。

## 为什么用静态检查而不是"import 一遍试试"

import 一遍需要全部依赖装好、还可能带副作用（起服务、写文件）；
而**这类错误纯粹是路径算术**，解析 AST 就够了 —— 快、无副作用、不装依赖也能跑。

代价要说清：它只能证明**目标模块存在**，不能证明那个名字（比如
`StreamRepeatWatch`）真的在里面。名字写错要靠别的测试兜。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: 我们自己发的目录。vendor / node_modules 不扫（第三方代码不归我们管）。
SCAN = ("src", "desktop", "scripts")


def _resolvable(level: int, module: str | None, pkg: tuple[str, ...], root: Path) -> bool:
    """把 ``from ..a.b import x`` 换算成磁盘路径，看它在不在。"""
    if level - 1 > len(pkg):
        return False  # 点太多，跑出顶层包了
    base = list(pkg[: len(pkg) - (level - 1)]) if level > 1 else list(pkg)
    target = root.joinpath(*base)
    if module:
        target = target.joinpath(*module.split("."))
    return target.is_dir() or target.with_suffix(".py").is_file()


def _problems() -> list[str]:
    out: list[str] = []
    for rel in SCAN:
        root = REPO / rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:  # pragma: no cover - 语法错误另有专门的检查
                out.append(f"{path.relative_to(REPO)}: 语法错误 {exc}")
                continue
            pkg = path.relative_to(root).parent.parts
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    if not _resolvable(node.level, node.module, pkg, root):
                        dots = "." * node.level
                        out.append(
                            f"{path.relative_to(REPO)}:{node.lineno}  "
                            f"from {dots}{node.module or ''} import … 解析不到"
                        )
    return out


def test_no_relative_import_points_at_a_module_that_does_not_exist():
    bad = _problems()
    assert bad == [], "相对导入写错了层级：\n  " + "\n  ".join(bad)


def test_the_checker_actually_catches_a_wrong_level(tmp_path):
    """守卫自己也要有守卫：造一个少写一个点的文件，必须被抓出来。

    没有这条，上面那个测试可能因为"根本没扫到文件"而永远绿 —— 那就是假绿。
    """
    root = tmp_path / "src"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "target.py").write_text("X = 1\n", encoding="utf-8")
    (root / "pkg" / "sub" / "__init__.py").write_text(
        "from ..target import X\n", encoding="utf-8"
    )
    assert _resolvable(2, "target", ("pkg", "sub"), root), "对的写法被误判了"

    (root / "pkg" / "sub" / "bad.py").write_text(
        "from .target import X\n", encoding="utf-8"  # 少一个点 → pkg.sub.target
    )
    found = [
        ast.dump(n)
        for p in (root,).__iter__()
        for n in ast.walk(ast.parse((root / "pkg" / "sub" / "bad.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.ImportFrom)
    ]
    assert found, "测试用例自己写错了"
    assert not _resolvable(1, "target", ("pkg", "sub"), root), "少一个点竟然也判成对的了"


@pytest.mark.parametrize("rel", SCAN)
def test_every_scanned_directory_exists(rel):
    """扫不到的目录会让上面那条测试变成空转 —— 顺手钉住。"""
    assert (REPO / rel).is_dir(), f"{rel} 不见了，相对导入检查正在空转"
