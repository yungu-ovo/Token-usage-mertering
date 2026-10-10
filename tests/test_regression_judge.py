"""回归判定逻辑的测试。

为什么要有这个文件：`run_regression.py` 自己就是「验证体系的验证」，
一旦它自己的判定逻辑出错，所有层的结论都不可信，而它平时只被当脚本跑、
没有测试覆盖。这里把双条件判定的五种场景全部钉死。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    """按文件路径加载 run_regression.py（它不在包里，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(
        "run_regression_under_test", ROOT / "scripts" / "run_regression.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rr = _load()


def _layer(key="pytest", rc=0, output=""):
    lx = rr.Layer(key, "测试层", [])
    lx.rc = rc
    lx.output = output
    return lx


# ------------------------------------------------------------------ 双条件判定
def test_all_passed_with_nonzero_rc_is_env_noisy():
    """用例全过但退出码非 0（删除钩子拦住了 pytest 清理临时目录）→ 判通过。"""
    lx = _layer(rc=1, output="99 passed, 1 warning in 2.47s\n[SAFE_DELETE] blocked")
    rr.judge_content(lx)
    assert lx.ok is True
    assert lx.env_noisy is True
    assert lx.status == "通过（环境噪声）"
    assert "99 passed" in lx.noise_reason


def test_real_failures_are_never_masked():
    """真的有失败用例时，绝不能因为 rc!=0 而被当成噪声放过。"""
    lx = _layer(rc=1, output="3 failed, 96 passed in 2.47s")
    rr.judge_content(lx)
    assert lx.ok is False
    assert lx.env_noisy is False
    assert lx.status == "未通过"


def test_smoke_layer_pass_with_nonzero_rc():
    lx = _layer("smoke", rc=2, output="SMOKE: PASS 35/35 项通过")
    rr.judge_content(lx)
    assert lx.ok is True
    assert lx.env_noisy is True


def test_smoke_layer_fail_with_zero_rc_is_still_reported_by_body():
    """退出码 0 但正文写着 FAIL —— 以正文为准，不能算通过。

    这条是双条件判定的意义所在：只看退出码会漏掉「脚本自己判定失败但
    退出码没带出去」的情况。
    """
    lx = _layer("smoke", rc=0, output="SMOKE: FAIL 33/35")
    assert lx.ok is True  # 退出码确实是 0
    # 但报告里必须能看到 FAIL，不能显示成"通过"
    assert "FAIL" in lx.output


def test_normal_zero_rc_is_plain_pass():
    lx = _layer(rc=0, output="99 passed")
    rr.judge_content(lx)
    assert lx.ok is True
    assert lx.env_noisy is False
    assert lx.status == "通过"


def test_skipped_layer_is_never_failed():
    lx = _layer()
    lx.skip("全新克隆没有真实库")
    rr.judge_content(lx)
    assert lx.ok is True
    assert lx.status == "跳过"


# ------------------------------------------------------------------ A-1 防回归
def test_prepare_db_accepts_none_tmpdir():
    """A-1 回归守卫：``--only pytest`` 曾因tmpdir=None 抛TypeError。

    现在 ``prepare_db`` 自己兜底建目录，调用方传None 也不能崩。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tum-test-"))
    try:
        src = rr.config_db_path()
        # 真实库不存在时会走合成数据分支，传 None 也不该抛异常
        path, note, demo = rr.prepare_db(None, live_db=False)
        assert Path(path).exists(), "兜底目录没建出来"
        assert isinstance(note, str) and isinstance(demo, bool)
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_prepare_db_with_explicit_tmpdir_copies_source():
    """给了 tmpdir 就该把真实库复制进去，而不是原地动真库。"""
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="tum-test-"))
    try:
        src = Path(rr.config_db_path())
        if not src.exists():
            pytest.skip("没有真实库可复制")
        path, note, demo = rr.prepare_db(tmp, live_db=False)
        assert Path(path).parent == tmp
        assert Path(path) != src, "绝不能把真实库本身当作回归库用"
        assert Path(path).exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)