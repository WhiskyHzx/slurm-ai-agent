#!/usr/bin/env python3
"""
slurm_client.py — 算力平台 REST API 统一封装 + JWT Token 自动管理。

所有对算力平台 (http://107.ustc.edu.cn:6820) 的 HTTP 请求都必须通过本模块，
禁止在其他模块中散落裸 requests 调用。

Token 安全：
  - Token 仅从环境变量 SLURM_JWT 读取，绝不硬编码。
  - 禁止将 Token 提交到 Git 或传入外部大模型消息中。

API 版本：Slurm REST API v0.0.41 (slurmrestd)
"""

import os
import subprocess
import logging
import re
from typing import Optional, Dict, Any

import requests

from config.settings import (
    BASE_URL,
    API_PREFIX as CONFIG_API_PREFIX,
    AUTO_REFRESH_TOKEN,
    DEFAULT_TOKEN_LIFESPAN as CONFIG_DEFAULT_TOKEN_LIFESPAN,
    REQUEST_TIMEOUT,
)

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（可通过 config/settings.py 覆盖）
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = BASE_URL
API_PREFIX = CONFIG_API_PREFIX
DEFAULT_TOKEN_LIFESPAN = CONFIG_DEFAULT_TOKEN_LIFESPAN  # 默认 1 天

# ---------------------------------------------------------------------------
# Token 管理
# ---------------------------------------------------------------------------


def _get_token_from_env() -> str:
    """从环境变量 SLURM_JWT 读取 Token，缺失时抛出明确错误。"""
    token = os.environ.get("SLURM_JWT")
    if not token:
        raise RuntimeError(
            "环境变量 SLURM_JWT 未设置。请先在登录节点执行：\n"
            "  $ scontrol token lifespan=86400\n"
            "  $ export SLURM_JWT=<上面输出的 token 值>"
        )
    return token


def _refresh_token(lifespan: int = DEFAULT_TOKEN_LIFESPAN) -> str:
    """
    通过 scontrol token 命令重新生成 JWT Token，并刷新当前进程环境变量。

    注意：这仅在能执行 scontrol 的节点（登录节点）上有效。
    返回新 Token 字符串。
    """
    logger.info("Token 已过期或即将过期，正在通过 scontrol token 重新生成...")
    try:
        result = subprocess.run(
            ["scontrol", "token", f"lifespan={lifespan}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scontrol token 执行失败: {result.stderr.strip()}")

        # scontrol token 输出格式: "SLURM_JWT=eyJhbG..."
        output = result.stdout.strip()
        if "=" in output:
            new_token = output.split("=", 1)[1].strip()
        else:
            # 某些版本可能直接输出 token
            new_token = output

        if not new_token:
            raise RuntimeError("scontrol token 返回了空的 Token")

        # 刷新当前进程环境变量
        os.environ["SLURM_JWT"] = new_token
        logger.info("Token 刷新成功。")
        return new_token

    except FileNotFoundError:
        raise RuntimeError(
            "未找到 scontrol 命令。请在登录节点上运行本程序，"
            "或手动设置 SLURM_JWT 环境变量。"
        )


def token_preview(token: str) -> str:
    """Return a short non-secret token preview for UI/status messages."""
    if not token:
        return "missing"
    if len(token) <= 12:
        return f"present len={len(token)}"
    return f"{token[:6]}...{token[-4:]} len={len(token)}"


def refresh_slurm_token(lifespan: int = DEFAULT_TOKEN_LIFESPAN) -> str:
    """Public wrapper for refreshing SLURM_JWT on the login node."""
    return _refresh_token(lifespan)


# ---------------------------------------------------------------------------
# 内部 HTTP 请求封装
# ---------------------------------------------------------------------------


class SlurmClient:
    """
    Slurm REST API 客户端。

    统一封装 GET / POST / DELETE 请求，自动携带 Bearer Token，
    支持 Token 过期自动刷新（可通过 auto_refresh 开关控制）。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        auto_refresh_token: bool = AUTO_REFRESH_TOKEN,
        token_lifespan: int = DEFAULT_TOKEN_LIFESPAN,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_prefix = API_PREFIX
        self.auto_refresh = auto_refresh_token
        self.token_lifespan = token_lifespan

    # ---- 内部方法 ----

    @property
    def _token(self) -> str:
        """获取当前 Token（优先环境变量，支持自动刷新）。"""
        try:
            return _get_token_from_env()
        except RuntimeError:
            if self.auto_refresh:
                return _refresh_token(self.token_lifespan)
            raise

    def _headers(self) -> Dict[str, str]:
        """构造带 Bearer Token 的请求头。"""
        token = self._token
        return {
            # 107 的 slurmrestd 实测不能同时接收 Bearer 和 X-SLURM-USER-TOKEN。
            "X-SLURM-USER-TOKEN": token,
            "Content-Type": "application/json",
        }

    def _url(self, path: str, use_slurmdb: bool = False) -> str:
        """拼接完整 API URL。

        参数:
            path: API 路径 (如 /jobs)
            use_slurmdb: 是否使用 slurmdbd 前缀（/slurmdb/v0.0.41 替代 /slurm/v0.0.41）
        """
        if use_slurmdb:
            db_prefix = self.api_prefix.replace("/slurm/", "/slurmdb/")
            return f"{self.base_url}{db_prefix}{path}"
        return f"{self.base_url}{self.api_prefix}{path}"

    def _check_errors(self, data: Dict[str, Any]) -> None:
        """
        检查 Slurm REST API 返回的 errors 字段。
        如果存在错误，抛出 RuntimeError。
        """
        if isinstance(data, dict) and "errors" in data and data["errors"]:
            errors = data["errors"]
            logger.error("API 返回错误: %s", errors)
            raise RuntimeError(f"Slurm API 错误: {errors}")

    def _request(
        self,
        method: str,
        path: str,
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        use_slurmdb: bool = False,
    ) -> Dict[str, Any]:
        """
        统一 HTTP 请求方法，自动处理 Token 过期（401）重试。

        参数:
            method: HTTP 方法 (GET/POST/DELETE)
            path: API 路径 (如 /jobs)
            json_data: JSON 请求体（POST 时使用）
            params: URL 查询参数
            use_slurmdb: 是否使用 slurmdbd 端点
        返回:
            解析后的 JSON 字典
        """
        url = self._url(path, use_slurmdb=use_slurmdb)

        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=self._headers(),
                json=json_data,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            # Token 过期自动刷新（仅尝试一次）
            if resp.status_code == 401 and self.auto_refresh:
                logger.warning("收到 401，尝试刷新 Token 后重试...")
                _refresh_token(self.token_lifespan)
                resp = requests.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    json=json_data,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )

            # 检查 HTTP 状态码
            if resp.status_code == 401:
                raise RuntimeError(
                    "认证失败 (401)。Token 可能已过期且自动刷新失败。"
                    "请在登录节点手动执行 scontrol token 并重新设置 SLURM_JWT。"
                )
            if resp.status_code == 404:
                raise RuntimeError(f"资源未找到 (404): {method} {url}")
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"HTTP {resp.status_code}: {resp.text[:500]}"
                )

            data = resp.json()
            self._check_errors(data)
            return data

        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"无法连接到算力平台 API ({self.base_url})。"
                "请确认网络可达且地址正确。"
            )
        except requests.exceptions.Timeout:
            raise RuntimeError(f"API 请求超时: {method} {url}")

    def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """GET 请求。"""
        return self._request("GET", path, params=params)

    def _post(
        self, path: str, json_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """POST 请求。"""
        return self._request("POST", path, json_data=json_data)

    def _delete(self, path: str) -> Dict[str, Any]:
        """DELETE 请求。"""
        return self._request("DELETE", path)

    # ==================================================================
    # 公开 API — 对应开发计划 2.1 节要求的所有函数
    # ==================================================================

    def list_jobs(self, partition: Optional[str] = None) -> Dict[str, Any]:
        """
        查询作业列表。

        参数:
            partition: 可选，分区名（如 "P107-RTX5090"）。
                       注意：v0.0.41 下 ?partition= 参数可能被接口忽略，
                       此时会先取全量再在代码内过滤。

        返回:
            Slurm jobs API 的完整 JSON 响应。
            作业列表在 data["jobs"] 中。
        """
        # 先尝试带 partition 参数请求
        params = {"partition": partition} if partition else None
        data = self._get("/jobs", params=params)

        # v0.0.41 下 partition 参数可能被忽略，在代码内做二次过滤
        if partition and "jobs" in data:
            original_count = len(data["jobs"])
            data["jobs"] = [
                j for j in data["jobs"]
                if j.get("partition", "").lower() == partition.lower()
            ]
            filtered_count = len(data["jobs"])
            if original_count != filtered_count:
                logger.info(
                    "服务端未按 partition 过滤，代码内过滤: %d → %d 条",
                    original_count, filtered_count,
                )

        return data

    def get_job(self, job_id: int) -> Dict[str, Any]:
        """
        查询单个作业详情。

        参数:
            job_id: 作业 ID（整数）

        返回:
            单个作业的 JSON 响应。
        """
        return self._get(f"/job/{job_id}")

    def submit_job(
        self,
        script: str,
        partition: str = "P107-RTX5090",
        name: str = "api-job",
        nodes: int = 1,
        time_limit: int = 60,
        *,
        account: str = "",
        qos: str = "",
        cpus_per_task: int = 1,
        gpus_per_node: int = 0,
        memory_mb: Optional[int] = None,
        working_directory: Optional[str] = None,
        standard_output: Optional[str] = None,
        standard_error: Optional[str] = None,
        extra_job_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        提交作业。

        参数:
            script:        作业脚本内容（完整的 #!/bin/bash ... 字符串）
            partition:     目标分区名
            name:          作业名称
            nodes:         申请节点数
            time_limit:    运行时间上限（分钟）
            account/qos:   计费账户与 QoS
            cpus_per_task: 每个任务申请的 CPU 核数
            gpus_per_node: 每个节点申请的 GPU 数
            memory_mb:     每个节点申请的内存（MB）；None 表示使用平台默认值
            working_directory: 作业运行目录
            standard_output/standard_error: 标准输出与错误日志路径
            extra_job_params: 兼容旧调用的额外参数；受控提交入口不使用它

        返回:
            提交结果 JSON，通常包含 job_id。

        注意：
            - API 端点是 /job/submit（单数 job），不是 /jobs/submit。
            - Slurm 25.11 要求必须提供 current_working_directory。
            - nodes 字段在 OpenAPI 中定义为 string 类型。
            - time_limit 字段名不是 time（后者会被忽略）。
            - 作业名会先经过安全清洗：去除所有路径分隔符（/ 反斜杠）、".."、
              以及文件系统不安全的字符，保证不会写出到子文件夹或越权目录。
        """
        # 清洗作业名：只能包含字母、数字、下划线、短横线和点，
        # 去掉可能构成路径穿越（/ \ ..）或建立子文件夹的字符
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "", str(name or "")).strip(".-")
        if not safe_name:
            safe_name = "api-job"

        nodes = max(1, int(nodes))
        cpus_per_task = max(1, int(cpus_per_task))
        gpus_per_node = max(0, int(gpus_per_node))
        job_spec: Dict[str, Any] = {
            "name": safe_name,
            "partition": str(partition or "").strip(),
            "nodes": str(nodes),          # OpenAPI 要求 string 类型
            "minimum_nodes": nodes,
            "tasks": 1,
            "cpus_per_task": cpus_per_task,
            "minimum_cpus": nodes * cpus_per_task,
            "time_limit": max(1, int(time_limit)),
            "current_working_directory": working_directory or os.getcwd(),
            "standard_output": standard_output or f"{safe_name}-%j.out",
            "standard_error": standard_error or f"{safe_name}-%j.err",
            # 当前平台的 data_parser 接受键值映射；缺少 environment 会报 I/O error。
            "environment": {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            },
        }
        if account:
            job_spec["account"] = str(account).strip()
        if qos:
            job_spec["qos"] = str(qos).strip()
        if gpus_per_node:
            # Slurm OpenAPI 对应 --tres-per-node=gres/gpu:N。
            # GPU 必须进入 REST job description，不能只依赖脚本中的 #SBATCH。
            job_spec["tres_per_node"] = f"gres/gpu:{gpus_per_node}"
        if memory_mb is not None:
            job_spec["memory_per_node"] = max(1, int(memory_mb))
        if extra_job_params:
            job_spec.update(extra_job_params)

        payload = {
            "script": script,
            "job": job_spec,
        }
        # 正确路径: /job/submit（单数），不是 /jobs/submit（复数）
        return self._post("/job/submit", json_data=payload)

    def cancel_job(self, job_id: int) -> Dict[str, Any]:
        """
        取消（删除）指定作业。

        参数:
            job_id: 作业 ID

        返回:
            取消结果 JSON。
        """
        return self._delete(f"/job/{job_id}")

    def get_diag(self) -> Dict[str, Any]:
        """
        查看集群诊断/统计信息。

        返回:
            集群统计 JSON，包含 statistics 等字段。
        """
        return self._get("/diag")

    def get_nodes(self) -> Dict[str, Any]:
        """
        查询集群节点信息（扩展接口，方便后续阶段使用）。

        返回:
            节点列表 JSON。
        """
        return self._get("/nodes")

    # ---- slurmdb 端点（数据库/历史/QoS） ----

    def get_qos(self) -> Dict[str, Any]:
        """
        查询所有 QoS 配置及配额限制。

        通过 slurmdbd REST API 获取，包含每个 QoS 的最大 CPU/GPU/内存等限制。

        返回:
            QoS 列表 JSON，数据在 data["qos"] 中。
        """
        return self._request("GET", "/qos", use_slurmdb=True)

    def get_jobs_history(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        查询历史作业（含已完成、失败、取消的作业）。

        通过 slurmdbd REST API 获取，可用于报错诊断和资源分析。

        参数:
            params: 可选查询参数，如 {"job_id": 12345} 或 {"submit_time": "2026-08-01"}

        返回:
            历史作业列表 JSON。
        """
        return self._request("GET", "/jobs", use_slurmdb=True, params=params)

    def read_job_log(
        self,
        job_id: int,
        log_type: str = "stdout",
        tail_lines: int = 100,
    ) -> str:
        """
        读取作业的输出日志文件内容。

        先从 REST API 获取作业详情，提取 standard_output 或 standard_error
        文件路径，再从本地文件系统读取文件尾部内容。

        参数:
            job_id:     作业 ID
            log_type:   日志类型，"stdout"（标准输出）或 "stderr"（标准错误）
            tail_lines: 读取文件尾部行数，默认 100

        返回:
            日志文件尾部内容字符串。

        注意：
            - 只能读取当前用户有权限访问的文件（通常是自己的作业日志）。
            - 如果日志文件不存在（作业尚未产生输出），返回提示信息。
        """
        if log_type not in ("stdout", "stderr"):
            raise ValueError(f"log_type 必须是 'stdout' 或 'stderr'，收到: {log_type}")

        # 1. 从 API 获取作业详情，拿到日志路径
        job_data = self.get_job(job_id)
        jobs = job_data.get("jobs", [job_data])
        job = jobs[0] if isinstance(jobs, list) and jobs else job_data

        field = "standard_output" if log_type == "stdout" else "standard_error"
        log_path = job.get(field, "")

        if not log_path:
            return (
                f"作业 {job_id} 未设置 {field} 路径，"
                f"无法读取{'标准输出' if log_type == 'stdout' else '标准错误'}日志。"
            )

        # 2. 从本地文件系统读取
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return (
                f"日志文件不存在: {log_path}\n"
                f"（作业可能尚未运行或尚未产生输出文件）"
            )
        except PermissionError:
            return (
                f"无权限读取日志文件: {log_path}\n"
                f"（该文件属于其他用户，无法访问）"
            )
        except OSError as e:
            return f"读取日志文件失败: {log_path}\n错误: {e}"

        # 3. 截取尾部
        if len(lines) <= tail_lines:
            content = "".join(lines)
        else:
            content = (
                f"...（省略前 {len(lines) - tail_lines} 行，共 {len(lines)} 行）...\n\n"
                + "".join(lines[-tail_lines:])
            )

        log_type_label = "标准输出" if log_type == "stdout" else "标准错误"
        return (
            f"## 作业 {job_id} 的{log_type_label}日志\n"
            f"文件: {log_path}\n"
            f"总行数: {len(lines)}，显示最后 {min(tail_lines, len(lines))} 行\n\n"
            f"{content}"
        )


# =========================================================================
# 模块级便捷函数（使用默认客户端实例，方便快速调用）
# =========================================================================

_default_client: Optional[SlurmClient] = None


def _get_client() -> SlurmClient:
    """获取或创建默认 SlurmClient 实例。"""
    global _default_client
    if _default_client is None:
        _default_client = SlurmClient()
    return _default_client


def list_jobs(partition: Optional[str] = None) -> Dict[str, Any]:
    """查询作业列表（便捷函数）。"""
    return _get_client().list_jobs(partition)


def get_job(job_id: int) -> Dict[str, Any]:
    """查询单个作业（便捷函数）。"""
    return _get_client().get_job(job_id)


def submit_job(
    script: str,
    partition: str = "P107-RTX5090",
    name: str = "api-job",
    nodes: int = 1,
    time_limit: int = 60,
    **kwargs: Any,
) -> Dict[str, Any]:
    """提交作业（便捷函数）。"""
    return _get_client().submit_job(
        script=script,
        partition=partition,
        name=name,
        nodes=nodes,
        time_limit=time_limit,
        **kwargs,
    )


def cancel_job(job_id: int) -> Dict[str, Any]:
    """取消作业（便捷函数）。"""
    return _get_client().cancel_job(job_id)


def get_diag() -> Dict[str, Any]:
    """查看集群统计（便捷函数）。"""
    return _get_client().get_diag()


def get_qos() -> Dict[str, Any]:
    """查询 QoS 配额（便捷函数）。"""
    return _get_client().get_qos()


def get_jobs_history(**kwargs: Any) -> Dict[str, Any]:
    """查询历史作业（便捷函数）。"""
    return _get_client().get_jobs_history(params=kwargs if kwargs else None)


def read_job_log(job_id: int, log_type: str = "stdout", tail_lines: int = 100) -> str:
    """读取作业日志（便捷函数）。"""
    return _get_client().read_job_log(job_id, log_type, tail_lines)


# =========================================================================
# __main__ 测试示例
# =========================================================================

if __name__ == "__main__":
    """
    人肉验证测试 —— 按阶段 1 验收标准逐项测试。

    用法：
        export SLURM_JWT=<你的token>
        python core/slurm_client.py
    """
    import json as _json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    client = SlurmClient()

    print("=" * 60)
    print("阶段 1 验收测试 — slurm_client.py")
    print("=" * 60)

    # 1. 测试 list_jobs
    print("\n[1/4] list_jobs() — 查询所有作业（取前 3 条展示）...")
    try:
        jobs_data = client.list_jobs()
        jobs = jobs_data.get("jobs", [])
        print(f"  ✓ 共 {len(jobs)} 个作业")
        for j in jobs[:3]:
            print(f"    - job_id={j.get('job_id')}, "
                  f"state={j.get('job_state')}, "
                  f"partition={j.get('partition')}, "
                  f"name={j.get('name')}")
    except Exception as e:
        print(f"  ✗ 失败: {e}")

    # 2. 测试 get_diag
    print("\n[2/4] get_diag() — 查看集群统计...")
    try:
        diag = client.get_diag()
        stats = diag.get("statistics", {})
        print(f"  ✓ 运行中作业: {stats.get('jobs_running', 'N/A')}")
        print(f"  ✓ 排队作业:   {stats.get('jobs_pending', 'N/A')}")
        print(f"  ✓ 总节点数:   {stats.get('parts_down', 'N/A')}")
    except Exception as e:
        print(f"  ✗ 失败: {e}")

    # 3. 测试 submit_job（提交一个轻量测试作业）
    print("\n[3/4] submit_job() — 提交测试作业 (srun hostname)...")
    test_script = "#!/bin/bash\nsrun hostname"
    submitted_job_id = None
    try:
        result = client.submit_job(
            script=test_script,
            partition="P107-RTX5090",
            name="slurm-client-test",
            nodes=1,
            time_limit=10,
        )
        submitted_job_id = result.get("job_id") or (
            result.get("result", {}).get("job_id")
        )
        print(f"  ✓ 作业已提交: job_id={submitted_job_id}")
        print(f"    完整响应: {_json.dumps(result, indent=2, ensure_ascii=False)[:500]}")
    except Exception as e:
        print(f"  ✗ 失败（可能分区名不匹配或权限不足）: {e}")

    # 4. 测试 get_job + cancel_job（如果提交成功）
    if submitted_job_id:
        print(f"\n[4a/4] get_job({submitted_job_id}) — 查询刚提交的作业...")
        try:
            job_info = client.get_job(submitted_job_id)
            job_state = "N/A"
            if "jobs" in job_info:
                job_state = job_info["jobs"][0].get("job_state", "N/A")
            print(f"  ✓ 作业状态: {job_state}")
        except Exception as e:
            print(f"  ✗ 失败: {e}")

        print(f"\n[4b/4] cancel_job({submitted_job_id}) — 取消测试作业...")
        try:
            cancel_result = client.cancel_job(submitted_job_id)
            print(f"  ✓ 已取消作业 {submitted_job_id}")
            print(f"    响应: {_json.dumps(cancel_result, indent=2, ensure_ascii=False)[:300]}")
        except Exception as e:
            print(f"  ✗ 失败（作业可能已完成）: {e}")
    else:
        print("\n[4/4] 跳过 get_job / cancel_job 测试（提交未成功）")

    print("\n" + "=" * 60)
    print("测试完成。")
    print("=" * 60)
