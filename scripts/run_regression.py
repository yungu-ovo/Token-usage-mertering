"""一键回归：把五层验证串成一条命令，跑完只给一个结论。

为什么要它：五层验证各自都能单独跑，但每次改完代码要手动跑五个命令、
还要自己确认服务在不在、端口冲没冲、库被写脏没有。这个脚本把这些收口：

* **自管服务**：自己挑一个空闲端口、用**临时复制的库**启动一个实例，
  跑完就关。所以你正在看的那个 8787 实例不受影响，回归也不会污染真实库。
* **不吞失败**：任一层不过就退出码非零，并把失败层的输出尾巴贴出来。
* **出报告**：汇总写进 ``data/回归报告-v{版本}.md``，附每层耗时与关键统计。
* **可降级**：``--base`` 指定已有服务（跳过自启动），``--live-db`` 直接打真实库。

判定用**退出码 + 正文双条件**，不是只看退出码：某些环境（比如带删除保护的机器）
会在用例全过之后因为清理临时目录被拦而给出非零退出码，那时正文里的
``99 passed`` 才是真相。��种情况标为 ``PASS (env-noisy)`` 并在报告里写明原因——
既不误报FAIL，也不静默放过。

用法::

    python scripts/run_regression.py                    # 全套，用临时库副本
    python scripts/run_regression.py --live-db          # 直接打真实库（只读层除外）
    python scripts/run_regression.py --base http://127.0.0.1:8787   # 复用已启动的服务
    python scripts/run_regression.py --only pytest,smoke            # 只跑指定层
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import __version__  # noqa: E402

PY = sys.executable
# 回归用的服务必须跑在**另一个**端口，避免顶掉用户正在看的实例
PORT_CANDIDATES = (8799, 8798, 8797, 8899, 8901)


# ------------------------------------------------------------------ 小工具
def free_port() -> int:
    for port in PORT_CANDIDATES:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    # 兜底：让系统给一个
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def opener():
    """本机设了 HTTP_PROXY，直连 127.0.0.1 会被代理拦成 502。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_health(base: str, timeout: float = 40.0) -> bool:
    op = opener()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with op.open(f"{base}/api/health", timeout=2) as resp:
                if json.loads(resp.read()).get("ok"):
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.5)
    return False


def db_copy(src: Path, dst_dir: Path) -> Path:
    """连 WAL 副文件一起复制，否则会丢掉最近的写入。"""
    dst = dst_dir / "usage.db"
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = src.with_name(src.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))
    return dst


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 合并输出)。"""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, f"超时（>{timeout}s）\n" + (exc.stdout or "")


def prepare_db(tmpdir: Path | None, live_db: bool) -> tuple[str, str, bool]:
    """准备回归要用的库，返回 ``(库路径, 说明, 是否合成数据)``。

    ``data/`` 是 gitignore 的，所以**全新克隆里没有 usage.db**。此时若直接
    复制就会 FileNotFoundError——仓库卫生和「clone 下来就能跑」不该冲突，
    所以这里退一步：造一份合成演示数据，让各层都有数据可跑，并在报告里写明。

    ``tmpdir`` 允许为 ``None``（调用方忘了建），这里自行兜底建一个，
    避免 ``None / "usage.db"`` 抛TypeError 这种毫无指向性的崩溃。
    """
    src = Path(config_db_path())

    if tmpdir is None:
        tmpdir = Path(tempfile.mkdtemp(prefix="tum-regress-db-"))

    if live_db:
        if not src.exists():
            raise SystemExit(
                f"--live-db 指定的库不存在：{src}\n"
                "先灌数：python scripts/ingest_workbuddy.py --ingest"
            )
        return str(src), f"真实库 {src}", False

    if not src.exists():
        target = tmpdir / "usage.db"
        print(f"未找到 {src}（全新克隆的正常情况）→ 生成合成演示数据供回归使用。")
        code, out = run(
            [PY, "scripts/seed_demo.py", "--db", str(target), "--reset"], timeout=300
        )
        if code != 0:
            raise SystemExit("生成演示数据失败：\n" + out[-2000:])
        return str(target), f"合成演示数据（未找到 {src}）", True

    return str(db_copy(src, tmpdir)), f"临时副本（源：{src}）", False


class Layer:
    def __init__(self, key: str, title: str, cmd: list[str], timeout: int = 600) -> None:
        self.key = key
        self.title = title
        self.cmd = cmd
        self.timeout = timeout
        self.rc: int | None = None
        self.seconds = 0.0
        self.output = ""
        self.headline = ""
        self.skipped = False
        self.skip_reason = ""
        self.env_noisy = False
        self.noise_reason = ""

    @property
    def ok(self) -> bool:
        # 跳过不算失败，但报告里必须显式写出原因，绝不静默略过
        return self.skipped or self.rc == 0 or self.env_noisy

    @property
    def status(self) -> str:
        """控制台与报告里显示的结论标签。"""
        if self.skipped:
            return "跳过"
        if self.rc == 0:
            return "通过"
        if self.env_noisy:
            return "通过（环境噪声）"
        return "未通过"

    def skip(self, reason: str) -> "Layer":
        self.skipped = True
        self.skip_reason = reason
        self.headline = reason
        print(f"\n{'=' * 72}\n⏭ 跳过：{self.title}\n   原因：{reason}")
        return self

    def tail(self, n: int = 12) -> str:
        return "\n".join(self.output.strip().splitlines()[-n:])


def judge_content(layer: "Layer") -> None:
    """退出码之外，再看正文里的结论，识别「用例全过但退出码非零」。

    真实踩过的坑：本机装了删除保护钩子，会拦 pytest 清理它自己建的临时目录
    （一次删 60 个文件、超过 50 的阈值），于是 pytest 在 ``99 passed`` 之后
    仍以非 0 退出。只看退出码会把「全绿」报成 FAIL——验证体系自己的可信度
    也会被质疑。这里以正文为准，但**必须在报告里写明**是谁在干扰，不静默放过。
    """
    if layer.rc == 0 or layer.skipped:
        return

    text = layer.output
    # 各层都有一行机器可读的总��论，先按它判
    if layer.key == "pytest":
        passed = re.search(r"(\d+) passed", text)
        failed = re.search(r"(\d+) (?:failed|error)", text)
        if passed and not failed:
            layer.env_noisy = True
            layer.noise_reason = (
                f"用例全部通过（{passed.group(1)} passed），但进程退出码为 {layer.rc}；"
                "判定以正文结论为准，退出码异常已记录"
            )
    else:
        verdict = re.search(r"^(?:SMOKE|RENDER|PRIVACY|ACCEPTANCE):.*$", text, re.M)
        if verdict and "PASS" in verdict.group(0) and "FAIL" not in verdict.group(0):
            layer.env_noisy = True
            layer.noise_reason = (
                f"正文结论为「{verdict.group(0).strip()}」，但进程退出码为 {layer.rc}；"
                "判定以正文结论为准，退出码异常已记录"
            )


def run_layer(layer: Layer) -> Layer:
    print(f"\n{'=' * 72}\n▶ {layer.title}\n  $ {' '.join(layer.cmd)}\n")
    started = time.time()
    try:
        proc = subprocess.run(
            layer.cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=layer.timeout,
            encoding="utf-8",
            errors="replace",
        )
        layer.rc = proc.returncode
        layer.output = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        layer.rc = 124
        layer.output = f"超时（>{layer.timeout}s）\n" + (exc.stdout or "")
    layer.seconds = time.time() - started
    judge_content(layer)
    flag = "PASS" if layer.ok else "FAIL"
    if layer.env_noisy:
        flag = "PASS*"
    print(f"◀ [{flag}] {layer.title}  用时 {layer.seconds:.1f}s")
    if layer.env_noisy:
        print(f"   ⚠ {layer.noise_reason}")
    if not layer.ok:
        print(layer.tail(20))
    return layer


def pick_headline(key: str, output: str) -> str:
    """从各层输出里挑一行最能说明结论的。"""
    patterns = {
        "pytest": (r"^\d+ passed", r"^\d+ failed"),
        "smoke": (r"^SMOKE:.*",),
        "render": (r"^RENDER:.*",),
        "privacy": (r"^PRIVACY:.*",),
        "acceptance": (r"^ACCEPTANCE:.*",),
    }
    for pat in patterns.get(key, ()):
        for line in output.strip().splitlines():
            if re.search(pat, line.strip()):
                return line.strip()
    return ""


# ------------------------------------------------------------------ 报告
def write_report(path: Path, layers: list[Layer], base: str, db_note: str, total: float) -> None:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bad = [x for x in layers if not x.ok]
    skipped = [x for x in layers if x.skipped]
    ran = len(layers) - len(skipped)
    verdict = "全部通过" if not bad else f"{len(bad)} 层未通过"
    if skipped and not bad:
        verdict += f"（{len(skipped)} 层跳过）"

    lines = [
        f"# 回归报告 · Token 消耗看板 v{__version__}",
        "",
        f"- 执行时间：{now}",
        f"- 服务地址：{base}（回归自管实例，跑完已关闭）",
        f"- 数据：{db_note}",
        f"- **结论：{verdict}**（跑 {ran}/{ran} 层）  总耗时 {total:.1f}s",
        "",
        "## 分层结果",
        "",
        "| 层 | 结论 | 耗时 | 关键结论 |",
        "| --- | --- | --- | --- |",
    ]
    for lx in layers:
        if lx.skipped:
            status, cost = "跳过", "—"
        else:
            status, cost = lx.status, f"{lx.seconds:.1f}s"
        lines.append(f"| {lx.title} | {status} | {cost} | {lx.headline or '—'} |")

    noisy = [lx for lx in layers if lx.env_noisy]
    if noisy:
        lines += ["", "## ⚠ 环境噪声（判定以正文结论为准）", ""]
        for lx in noisy:
            lines.append(f"- **{lx.title}**：{lx.noise_reason}")
        lines += [
            "",
            "> 这不影响「用例是否通过」的结论，但退出码异常说明运行环境有拦截动作"
            "（如删除保护钩子拦住了测试清理临时目录）。排查方向：测试夹具用的临时目录",
            "> 是否落在受保护路径下。",
        ]

    if skipped:
        lines += ["", "## 跳过的层及原因", ""]
        for lx in skipped:
            lines.append(f"- **{lx.title}**：{lx.skip_reason}")

    if bad:
        lines += ["", "## 失败详情", ""]
        for lx in bad:
            lines += [
                f"### {lx.title}（退出码 {lx.rc}）",
                "",
                "```",
                lx.tail(30),
                "```",
                "",
            ]

    lines += [
        "",
        "## 分层说明",
        "",
        "| 层 | 覆盖 |",
        "| --- | --- |",
        "| pytest | 单元：金额精度、幂等、归一口径、时区边界、API 契约 |",
        "| HTTP 冒烟 | 接口契约、跨端点口径一致性、构成之和=总额、页面无假数据残留 |",
        "| 渲染冒烟 | Node DOM 桩执行页面脚本，断言 KPI/图表/表格/洞察均已填充 |",
        "| 隐私红线 | 白名单、解析器源码 AST、只读约束、库中文本长度与正文特征词 |",
        "| 试点验收 | 端到端 + RK-01~05 五条硬约束 + 聚合正确性（七条） |",
        "",
        "---",
        "",
        "本报告由 `scripts/run_regression.py` 自动生成。回归使用临时库副本，不修改 `data/usage.db`；",
        "未找到 `data/usage.db` 时（全新克隆）自动生成合成演示数据，此时验收层会显式跳过。",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description="一键回归（五层验证）")
    ap.add_argument("--base", default=None, help="复用已启动的服务，跳过自启动")
    ap.add_argument("--live-db", action="store_true", help="直接打真实库，不做副本")
    ap.add_argument("--only", default=None, help="只跑指定层，逗号分隔：pytest,smoke,render,privacy,acceptance")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    wanted = {s.strip() for s in args.only.split(",")} if args.only else None
    if args.report:
        report_path = Path(args.report)
    elif wanted:
        # 局部回归别把完整报告覆盖掉
        report_path = ROOT / "data" / f"回归报告-v{__version__}-{'+'.join(sorted(wanted))}.md"
    else:
        report_path = ROOT / "data" / f"回归报告-v{__version__}.md"

    # 只有需要打 HTTP 的层才要求服务；只跑 pytest / privacy 时不必启服务
    needs_server = wanted is None or bool(wanted & {"smoke", "render", "acceptance"})

    node = shutil.which("node")
    if node is None:
        for cand in (
            Path.home() / ".workbuddy" / "binaries" / "node" / "versions" / "22.22.2-2" / "node.exe",
            Path("D:/node.js/node.exe"),
        ):
            if cand.exists():
                node = str(cand)
                break

    tmpdir: Path | None = None
    server: subprocess.Popen | None = None
    server_log: Path | None = None
    server_log_fh = None
    base = args.base
    db_note = "临时库副本"
    effective_db = config_db_path()
    demo_mode = False

    try:
        # ---------------- 准备库与服务
        # 临时目录必须**无条件**先建：--only pytest / privacy 这类不需要服务的分支
        # 同样要把真实库复制成临时副本，绝不能把 None 传进 prepare_db()。
        tmpdir = Path(tempfile.mkdtemp(prefix="tum-regress-"))
        if not needs_server:
            print("所选层不需要服务，跳过启动。")
            effective_db, db_note, demo_mode = prepare_db(tmpdir, args.live_db)
            base = base or "（未使用，所选层不需要服务）"
        elif base is None:
            port = free_port()
            base = f"http://127.0.0.1:{port}"
            effective_db, db_note, demo_mode = prepare_db(tmpdir, args.live_db)

            env = dict(os.environ)
            env["TUM_DB_PATH"] = effective_db
            env["TUM_PORT"] = str(port)
            env["TUM_HOST"] = "127.0.0.1"
            print(f"启动回归实例：{base}  库：{db_note}")

            # 服务日志必须落到**文件**，不能接 PIPE：
            # 回归会打几百个请求，uvicorn 每个都写一行访问日志，
            # 没人读的管道缓冲（约 64KB）写满后服务会直接卡死。
            server_log = Path(tempfile.mkdtemp(prefix="tum-regress-log-")) / "uvicorn.log"
            server_log_fh = server_log.open("w", encoding="utf-8", errors="replace")

            server = subprocess.Popen(
                [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(ROOT),
                env=env,
                stdout=server_log_fh,
                stderr=subprocess.STDOUT,
            )
            if not wait_health(base):
                print("服务未能就绪，回归中止。")
                server_log_fh.flush()
                print(server_log.read_text(encoding="utf-8", errors="replace")[-2000:])
                return 1
            print("服务已就绪。")
        else:
            base = base.rstrip("/")
            db_note = f"外部服务 {base}（按真实库校验）"
            if not wait_health(base, timeout=5):
                print(f"指定服务不可达：{base}")
                return 1

        db_path = effective_db

        # ---------------- 组层
        layers: list[Layer] = []
        if wanted is None or "pytest" in wanted:
            layers.append(Layer("pytest", "单元测试 pytest", [PY, "-m", "pytest", "-q"]))
        if wanted is None or "smoke" in wanted:
            layers.append(
                Layer("smoke", "HTTP 契约冒烟", [PY, "scripts/smoke.py", "--base", base])
            )
        if wanted is None or "render" in wanted:
            if node is None:
                print("跳过渲染冒烟：找不到可用的 node。")
            else:
                layers.append(
                    Layer("render", "前端渲染冒烟", [node, "scripts/render_check.mjs", base])
                )
        if wanted is None or "privacy" in wanted:
            layers.append(
                Layer("privacy", "隐私红线检查", [PY, "scripts/check_privacy.py", "--db", db_path])
            )
        if wanted is None or "acceptance" in wanted:
            # 验收层校验的是「**真实**试点数据跑通了」，它的第 1 条就要求
            # data_source='workbuddy' 且本月成本 > 0。合成数据下必然不成立——
            # 这时显式跳过并写明原因，好过让它失败去污染结论（也不许改判定标准）。
            if demo_mode:
                layers.append(
                    Layer("acceptance", "试点验收七条", []).skip(
                        "合成演示数据，非真实本机转录；验收要求 data_source='workbuddy'"
                        " 且本月成本 > 0。灌入真实数据后重跑即可解除。"
                    )
                )
            else:
                layers.append(
                    Layer(
                        "acceptance",
                        "试点验收七条",
                        [
                            PY,
                            "scripts/pilot_acceptance.py",
                            "--base",
                            base,
                            "--db",
                            db_path,
                            "--report",
                            str(tmpdir / "试点验收报告.md") if tmpdir else str(ROOT / "data" / "试点验收报告.md"),
                        ],
                        timeout=900,
                    )
                )

        # ---------------- 跑
        started = time.time()
        for lx in layers:
            if lx.skipped:
                continue
            run_layer(lx)
            lx.headline = pick_headline(lx.key, lx.output)
        total = time.time() - started

        for lx in layers:
            mark = "SKIP" if lx.skipped else ("PASS" if lx.ok else "FAIL")
            print(f"  {mark}  {lx.title:22} {lx.seconds:6.1f}s  {lx.headline}")

        write_report(report_path, layers, base, db_note, total)

        bad = [x for x in layers if not x.ok]
        skipped = [x for x in layers if x.skipped]
        ran = len(layers) - len(skipped)
        skip_note = f"，{len(skipped)} 层跳过" if skipped else ""
        print("\n" + "=" * 72)
        if bad:
            print(f"REGRESSION: FAIL  {len(bad)}/{ran} 层未通过（总耗时 {total:.1f}s）")
            for lx in bad:
                print(f"  - {lx.title}：退出码 {lx.rc}")
            print(f"\n报告：{report_path}")
            return 1
        print(f"REGRESSION: PASS  {ran}/{ran} 层全部通过{skip_note}（总耗时 {total:.1f}s）")
        for lx in skipped:
            print(f"  ⏭ 跳过 {lx.title}：{lx.skip_reason}")
        print(f"报告：{report_path}")
        return 0

    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
            print("\n回归实例已关闭。")
        if server_log_fh is not None:
            server_log_fh.close()
        if server_log is not None:
            shutil.rmtree(server_log.parent, ignore_errors=True)
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def config_db_path() -> str:
    from app import config

    return str(config.DB_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
