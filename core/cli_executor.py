#!/usr/bin/env python3
"""
cli_executor.py — Slurm 只读命令行工具执行层（REST API 的补充）。

设计边界（与 core/slurm_client.py 的分工）：
- REST 已覆盖的能力（作业/节点/QoS/历史等）继续走 slurm_client，
  不经过本模块，避免同一能力出现两条通道；
- 本模块只封装 REST 覆盖不到的只读诊断命令：
    sprio   → 排队作业调度优先级
    sshare  → fairshare 公平份额与用量
    sstat   → 运行中作业的实时资源占用
    sreport → 集群/用户用量报表
    sacctmgr show assoc → 当前用户的账户与 QoS 授权
- 所有写操作（提交/取消/修改）禁止走本模块，必须走 REST 工具的
  确认流程（submit_job / cancel_job）；
- 使用 argv 数组直接调用 subprocess（shell=False），不经过 shell 拼接，
  参数值经过白名单字符校验，不接受以 "-" 开头的值（防止伪装选项）；
- sacctmgr 有子命令多态（show/list/modify/...），白名单在子命令级
  二次校验，只放行只读子命令；
- 本模块假定部署在登录节点运行（项目部署目标）。本地开发机没有
  Slurm 命令时会返回友好错误提示，不影响其他 REST 工具正常使用。

明确不纳入的命令（2026-08-21 实测，见 evaluation/107-api-capability-report.md）：
- sacct：CLI 记账查询对普通用户返回空，历史作业请用 get_jobs_history（REST）；
- scrontab：集群已禁用（fatal: scrontab is disabled on this cluster）；
- salloc / srun --pty：交互式会话需要 tty，智能体只能指导用户自行使用；
- sattach / sbcast / scrun / sh5util / strigger：依赖运行中作业或集群
  特殊配置，当前无使用场景。
"""

import getpass
import logging
import re
import shutil
import subprocess
from datetime import date, timedelta
from typing import List, Optional

logger = logging.getLogger(__name__)

# =========================================================================
# 配置
# =========================================================================

COMMAND_TIMEOUT = 20      # 单命令超时（秒）
MAX_OUTPUT_LINES = 120    # 输出最多保留行数（防止撑爆 LLM 上下文）

# 只读白名单：命令名 -> 允许的子命令集合（None 表示该命令整体只读）
READONLY_WHITELIST = {
    "sprio": None,
    "sshare": None,
    "sstat": None,
    "sreport": None,
    "sacctmgr": {"show", "list"},
}

# 参数值允许的字符（字母/数字/_.:@-+%），长度 1-64
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:@\-+%]{1,64}$")


def _sanitize_value(value, field: str) -> str:
    """校验外部（LLM）传入的参数值，防止伪装成选项或携带特殊字符。"""
    value = str(value).strip()
    if not value or value.startswith("-") or not _SAFE_VALUE_RE.match(value):
        raise ValueError(f"参数 {field} 含有非法字符: {value!r}")
    return value


# =========================================================================
# 核心执行函数
# =========================================================================


def run_readonly(argv: List[str], timeout: int = COMMAND_TIMEOUT) -> str:
    """
    执行白名单内的只读 Slurm 命令，返回文本输出。

    参数:
        argv: 完整命令数组，如 ["sprio", "-u", "pb25111697"]
        timeout: 超时秒数

    返回:
        命令输出（stdout；失败时附 stderr），已按行数截断。
    """
    if not argv:
        raise ValueError("命令不能为空")

    cmd = argv[0]
    if cmd not in READONLY_WHITELIST:
        raise ValueError(f"命令 {cmd} 不在只读白名单内，拒绝执行")

    policy = READONLY_WHITELIST[cmd]
    if policy is not None:
        # 子命令级校验：第一个非选项参数必须是允许的只读子命令
        sub = next((a for a in argv[1:] if not a.startswith("-")), None)
        if sub not in policy:
            raise ValueError(
                f"命令 {cmd} 只允许子命令 {sorted(policy)}，拒绝执行: {sub!r}"
            )

    if shutil.which(cmd) is None:
        return (
            f"（命令 {cmd} 在当前环境不可用：本工具需要部署在登录节点上运行，"
            f"本地开发环境没有 Slurm CLI。其他 REST 工具不受影响。）"
        )

    logger.info("执行只读命令: %s", " ".join(argv))
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"（命令 {cmd} 执行超时（>{timeout}s），已中止，请稍后重试。）"

    output = proc.stdout or ""
    if proc.returncode != 0 and proc.stderr:
        output = (output + "\n" + proc.stderr).strip()
    output = output.strip()

    if not output:
        return "（命令执行成功，但没有返回数据。可能当前没有相关记录。）"

    lines = output.splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        kept = "\n".join(lines[:MAX_OUTPUT_LINES])
        output = kept + (
            f"\n...（输出共 {len(lines)} 行，已截断至前 {MAX_OUTPUT_LINES} 行）"
        )
    return output


# =========================================================================
# 面向 agent 工具的封装
# =========================================================================


class SlurmCLI:
    """只读诊断命令封装，供 tools_registry 的 CLI 类工具调用。"""

    def priority(
        self, job_id: Optional[int] = None, user: Optional[str] = None
    ) -> str:
        """排队作业的调度优先级（sprio）。默认查当前用户。"""
        argv = ["sprio", "-l"]
        if job_id is not None:
            argv += ["-j", _sanitize_value(job_id, "job_id")]
        else:
            argv += ["-u", _sanitize_value(user or getpass.getuser(), "user")]
        return run_readonly(argv)

    def shares(
        self, user: Optional[str] = None, account: Optional[str] = None
    ) -> str:
        """fairshare 公平份额与使用量（sshare）。默认查当前用户。"""
        argv = [
            "sshare", "-l",
            "-u", _sanitize_value(user or getpass.getuser(), "user"),
        ]
        if account:
            argv += ["-A", _sanitize_value(account, "account")]
        return run_readonly(argv)

    def job_live_stats(self, job_id: int) -> str:
        """运行中作业的实时资源占用（sstat）。作业必须是 RUNNING 状态。"""
        try:
            job_id = int(job_id)
        except (TypeError, ValueError):
            raise ValueError("job_id 必须是整数")
        return run_readonly(["sstat", "-j", f"{job_id}", "--allsteps"])

    def usage_report(
        self, report_type: str = "cluster", days: int = 30
    ) -> str:
        """用量统计报表（sreport）。

        report_type:
            cluster  → 集群整体利用率（Allocated/Down/Idle，CPU 小时）
            user_top → 用量最多的用户排行
        days: 统计最近 N 天（1-365）
        """
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError("days 必须是整数")
        if not 1 <= days <= 365:
            raise ValueError("days 取值范围是 1-365")

        start = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
        if report_type == "user_top":
            argv = ["sreport", "user", "Top",
                    f"start={start}", "end=now", "-t", "hours"]
        else:
            argv = ["sreport", "cluster", "Utilization",
                    f"start={start}", "end=now", "-t", "hours"]
        return run_readonly(argv)

    def my_associations(self) -> str:
        """当前用户的账户（Account）、集群与 QoS 授权（sacctmgr show assoc）。"""
        user = getpass.getuser()
        argv = [
            "sacctmgr", "-n", "-p",
            "show", "assoc", f"user={user}",
            "format=User,Account,Cluster,QOS,DefaultQOS",
        ]
        return run_readonly(argv)


# =========================================================================
# __main__ 测试
# =========================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("=" * 60)
    print("cli_executor.py 测试")
    print("=" * 60)

    # 白名单拦截测试（应被拒绝）
    print("\n[测试] 白名单拦截: run_readonly(['scrontab', '-l'])")
    try:
        run_readonly(["scrontab", "-l"])
        print("  ✗ 未拦截（异常！）")
    except ValueError as e:
        print(f"  ✓ 已拦截: {e}")

    print("\n[测试] 子命令校验: sacctmgr modify")
    try:
        run_readonly(["sacctmgr", "modify", "user=x"])
        print("  ✗ 未拦截（异常！）")
    except ValueError as e:
        print(f"  ✓ 已拦截: {e}")

    print("\n[测试] 参数校验: 伪造选项")
    try:
        SlurmCLI().priority(user="--help")
        print("  ✗ 未拦截（异常！）")
    except ValueError as e:
        print(f"  ✓ 已拦截: {e}")

    # 功能测试（登录节点上返回真实数据；本地开发机返回友好提示）
    cli = SlurmCLI()
    for label, fn in [
        ("priority()", lambda: cli.priority()),
        ("shares()", lambda: cli.shares()),
        ("usage_report()", lambda: cli.usage_report()),
        ("my_associations()", lambda: cli.my_associations()),
    ]:
        print(f"\n[测试] {label}")
        try:
            result = fn()
            print(f"  {result[:200]}")
        except Exception as e:
            print(f"  ✗ 失败: {e}")

    print("\n✓ cli_executor.py 测试完成")
