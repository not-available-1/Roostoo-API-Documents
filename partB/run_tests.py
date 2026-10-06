"""不装 pytest 也能跑测试的最小 runner。

为什么要有这个: 环境里没装 pytest（装依赖要先问）, 但测试**必须能跑** ——
FAQ Q40/Q44 明确要求仓库"可复现", 评委 clone 下来跑不动测试等于没有测试。
这个 runner 只做 pytest 最核心的两件事: 发现 `test_*` 函数、给需要 `tmp_path` 的测试塞一个临时目录。

用法:
  python -m partB.run_tests                    # 跑 partB 下所有 test_*.py
  python -m partB.run_tests partB.test_ml      # 只跑一个模块
退出码: 全过 = 0, 有失败 = 1（CI/cron 里能看见）
"""
from __future__ import annotations

import importlib
import inspect
import pathlib
import shutil
import sys
import tempfile
import traceback


def discover(modules: list[str] | None) -> list[str]:
    if modules:
        return modules
    here = pathlib.Path(__file__).resolve().parent
    return sorted(f"partB.{p.stem}" for p in here.glob("test_*.py"))


def run(modules: list[str] | None = None) -> int:
    passed, failed = [], []
    for mod_name in discover(modules):
        try:
            mod = importlib.import_module(mod_name)
        except Exception:                                        # noqa: BLE001
            print(f"!! {mod_name} 导入失败")
            traceback.print_exc()
            failed.append(f"{mod_name}:<import>")
            continue
        tests = [(n, f) for n, f in vars(mod).items()
                 if n.startswith("test_") and callable(f) and getattr(f, "__module__", "") == mod_name]
        if not tests:
            continue
        print(f"\n--- {mod_name} ({len(tests)} 个测试) ---")
        for name, fn in sorted(tests, key=lambda kv: kv[0]):
            params = inspect.signature(fn).parameters
            kwargs = {}
            tmpdir = None
            if "tmp_path" in params:
                tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="partb_test_"))
                kwargs["tmp_path"] = tmpdir
            try:
                fn(**kwargs)
                print(f"  PASS  {name}")
                passed.append(f"{mod_name}::{name}")
            except Exception:                                    # noqa: BLE001
                print(f"  FAIL  {name}")
                traceback.print_exc()
                failed.append(f"{mod_name}::{name}")
            finally:
                if tmpdir is not None:
                    shutil.rmtree(tmpdir, ignore_errors=True)
    print(f"\n{'=' * 60}\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:] or None))
