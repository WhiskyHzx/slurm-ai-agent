# 107 算力平台 Slurm 命令行能力实测报告

日期：2026-08-21  
测试方式：Mac 本地通过 SSH（`ssh 107.ustc.edu.cn`，ControlMaster 复用连接）在登录节点远程执行  
本地项目：`/Users/mac/work/slurm-ai-agent`（与远程 `/home/scc/pb25111697/slurm-ai-agent` 绑定）

## 1. 测试环境

| 项目 | 值 |
|---|---|
| 登录节点 | `tradmin-02` |
| 测试用户 | `pb25111697` |
| 测试时间 | 2026-08-21 02:45 CST |
| Slurm 版本 | `slurm 25.11.2`（smd 发行版） |
| 已安装包 | `slurm-smd` `slurm-smd-client` `slurm-smd-slurmctld` `slurm-smd-slurmd` `slurm-smd-slurmdbd` `slurm-smd-slurmrestd` |
| 集群名（记账库） | `training` |

## 2. 测试方法

- **只读命令**（sinfo、squeue、scontrol show、sacctmgr show、sprio、sshare、sreport、sdiag、sacct、strigger --get、scrontab -l）：直接实测执行，每条命令 `timeout 25` 秒。
- **有副作用的命令**（salloc、sbatch、srun、scancel、sbcast、sattach、scrun、sstat）：仅做 `--help` / `--version` 级验证，**不真正申请资源或提交作业**。
- 判定标准：`command -v` 存在 + 实测退出码 + 输出内容。

## 3. 总览：20 个客户端命令实测状态

| 命令 | 路径 | 状态 | 实测结论 |
|---|---|---|---|
| `sacct` | `/usr/bin/sacct` | ⚠️ 可执行但查询为空 | rc=0，但所有查询（按用户/按时间段/按 jobid/`--allusers`）均返回空，见 §4.2 |
| `sacctmgr` | `/usr/bin/sacctmgr` | ✅ 可用 | `show assoc` 正常返回账号/QoS 授权数据 |
| `salloc` | `/usr/bin/salloc` | ✅ 存在 | help 验证通过（未实际申请资源） |
| `sattach` | `/usr/bin/sattach` | ✅ 存在 | help 验证通过 |
| `sbatch` | `/usr/bin/sbatch` | ✅ 可用 | help 验证通过（实际提交走 REST 已验证） |
| `sbcast` | `/usr/bin/sbcast` | ✅ 存在 | help 验证通过 |
| `scancel` | `/usr/bin/scancel` | ✅ 可用 | help 验证通过（实际取消走 REST 已验证） |
| `scontrol` | `/usr/bin/scontrol` | ✅ 可用 | `show partition` 返回真实分区配置 |
| `scrontab` | `/usr/bin/scrontab` | ❌ **集群已禁用** | `scrontab: fatal: scrontab is disabled on this cluster` |
| `scrun` | `/usr/bin/scrun` | ✅ 存在 | help 验证通过（OCI 运行时代理，冷门） |
| `sdiag` | `/usr/bin/sdiag` | ✅ 可用 | 返回真实调度统计 |
| `sh5util` | `/usr/bin/sh5util` | ⚠️ 存在 | help 退出码 255，依赖 HDF5 性能采集配置，当前无使用场景 |
| `sinfo` | `/usr/bin/sinfo` | ✅ 可用 | 返回 7 个分区的真实节点状态 |
| `sprio` | `/usr/bin/sprio` | ✅ 可用 | 全局查询返回真实作业优先级 |
| `squeue` | `/usr/bin/squeue` | ✅ 可用 | 返回全集群真实作业列表 |
| `sreport` | `/usr/bin/sreport` | ✅ 可用 | `cluster Utilization` 返回 30 天真实用量数据 |
| `srun` | `/usr/bin/srun` | ✅ 存在 | `--version` 验证通过（未实际运行作业） |
| `sshare` | `/usr/bin/sshare` | ✅ 可用 | 返回真实 fairshare 数据 |
| `sstat` | `/usr/bin/sstat` | ✅ 存在 | help 验证通过（需运行中作业才能实际使用） |
| `strigger` | `/usr/bin/strigger` | ✅ 可用 | `--get` rc=0，当前无触发器 |

**统计：** 20 个命令全部存在于 `/usr/bin`；17 个完全可用；1 个被集群禁用（`scrontab`）；2 个特殊（`sacct` 查询受限、`sh5util` 无使用场景）。

## 4. 关键发现

### 4.1 `scrontab` 被集群禁用（重要）

```text
$ scrontab -l
scrontab: fatal: scrontab is disabled on this cluster
```

**影响：** 定时作业（cron 式周期任务）在本集群不可用。智能体不应把 `scrontab` 作为可推荐方案；用户有周期任务需求时应建议"脚本内 sleep 循环 + 长时限作业"或平台侧方案。

### 4.2 `sacct` 可执行但记账查询返回空（重要）

命令本身存在且 rc=0，但以下所有变体均返回空：

```text
sacct -X -u $USER -S <2天前/30天前>          # 空
sacct -M training -X -u $USER -S <3天前>     # 空
sacct -j 24639                                # 空
sacct --allusers -X -S <昨天>                 # 空
```

而同一时间窗口内：

- `squeue` 能看到大量真实作业（RUNNING/PENDING）
- `sreport cluster Utilization` 能查到真实用量（说明 slurmdbd 里有数据）
- REST 端点 `/slurmdb/v0.0.41/jobs/` 之前实测返回 200（带参数可查）

**结论：** `sacct` CLI 对普通用户的记账可见性受限（疑似 slurmdbd 权限/配置原因）。**历史作业查询应以 REST `/slurmdb/jobs/` 为主**，`sacct` 仅作为备用。B 路线（SSH 白名单直执行）若包含 `sacct`，需注意它可能查不到数据。

### 4.3 `sacctmgr` 是当前查"我的账号/QoS 授权"的唯一可靠途径

```text
$ sacctmgr -n -p show assoc user=pb25111697 format=User,Account,QOS
pb25111697|competition|qos_p107-a100,qos_p107-rtx5090,qos_stu_default
pb25111697|stu|qos_p107-a100,qos_p107-rtx5090,qos_stu_default
pb25111697|stu|qos_p107-a100,qos_p107-rtx5090,qos_stu_default
```

当前用户有两个账号（`competition`、`stu`），集群为 `training`，可用 QoS 为 `qos_p107-a100`、`qos_p107-rtx5090`、`qos_stu_default`。REST 的 `/slurmdb/users/` 也能查到本人信息，但 `sacctmgr show assoc` 输出更直观。

### 4.4 `sreport` 用法要点

- ✅ `sreport cluster Utilization start=... end=now -t hours`：可用，返回真实数据：

```text
Cluster Utilization 2026-07-22T00:00:00 - 2026-08-21T02:59:59 (CPU Hours)
  Cluster Allocated     Down PLND Dow       Idle  Planned   Reported
 training     75459    24988        0    1092092    22548    1215088
```

- ❌ `sreport user Utilization` 不是合法报表名，按用户查询应使用 `sreport user Top`。

### 4.5 `sprio` / `sshare` 均正常可用（B 路线核心候选）

```text
$ sprio | head -3
          JOBID PARTITION   PRIORITY       SITE
          24639 P107-RTX5          1          0
          40866 Students           1          0
```

当前作业优先级均为 1（优先级因子基本扁平），`SITE` 列为站点自定义因子。`sshare` 返回完整 fairshare 表（root 账号 RawUsage 150196793）。

**注意：** REST 的 `/slurm/v0.0.41/shares` 之前实测超时，因此 `sshare` 只能走 CLI。这与 B 路线（SSH 白名单）的价值判断一致：`sprio`、`sshare`、`sstat`、`sreport` 是 REST 覆盖不到、只能靠 CLI 的核心缺口。

## 5. 对 AI Agent 工具覆盖的对照结论

当前 agent 的 8 个 Slurm 工具（REST 封装）与 CLI 实测能力的对照：

| Agent 工具 | 对应命令 | CLI 实测状态 | 备注 |
|---|---|---|---|
| `list_jobs` | `squeue` | ✅ | REST `/slurm/jobs/` 稳定 |
| `get_job` | `scontrol show job` | ✅ | 命令可用 |
| `submit_job` | `sbatch` | ✅ | 写操作，走确认流 |
| `cancel_job` | `scancel` | ✅ | 写操作，走确认流 |
| `get_diag` | `sdiag` | ✅ | REST `/slurm/diag/` 稳定 |
| `get_nodes` | `sinfo` | ✅ | 节点明细；分区汇总可补 REST `/slurm/partitions/` |
| `get_qos` | `sacctmgr show qos` | ✅ | QoS 全量可用；**个人授权查询**可用 `sacctmgr show assoc`（CLI）或 REST `/slurmdb/users/` |
| `get_jobs_history` | `sacct` | ⚠️ | **REST 优先**：CLI `sacct` 查询为空，REST `/slurmdb/jobs/` 带参数可用 |

**CLI 独有、REST 无法覆盖的能力（B 路线候选白名单）：**

| 命令 | 价值 | 建议 |
|---|---|---|
| `sprio` | 排队优先级诊断（"为什么排不上"） | 高，建议纳入白名单 |
| `sshare` | fairshare 份额（REST shares 端点超时） | 高，建议纳入白名单 |
| `sstat` | 运行中作业实时资源 | 中，需运行中作业 |
| `sreport` | 用量报表（集群维度可用） | 中，建议纳入白名单 |
| `sacctmgr show assoc` | 个人账号/QoS 授权（比 REST users 端点直观） | 中，只读子命令 |
| `scontrol show` | 分区/节点/QoS 详情兜底 | 中，只读子命令 |
| `scrontab` | ❌ 已禁用 | 不纳入 |
| `sacct` | ⚠️ 查询为空 | 不纳入（用 REST 替代） |

## 6. 附录：Slurm REST API 实测速查（2026-08-19 实测，保留备查）

认证：`X-SLURM-USER-TOKEN: <SLURM_JWT>`（与 `Authorization: Bearer` 同时发送会 401，只发前者）。

| endpoint | 状态 | 一句话作用 |
|---|---:|---|
| `/slurm/v0.0.41/ping/` | 200 | 探测 slurmctld 是否在线 |
| `/slurm/v0.0.41/diag/` | 200 | 集群整体调度统计 |
| `/slurm/v0.0.41/jobs/` | 200 | 当前作业明细列表 |
| `/slurm/v0.0.41/jobs/state/` | 200 | 精简作业状态列表 |
| `/slurm/v0.0.41/nodes/` | 200 | 节点状态/资源 |
| `/slurm/v0.0.41/partitions/` | 200 | 分区/队列信息 |
| `/slurm/v0.0.41/licenses/` | 200 | 许可证剩余量（当前 0） |
| `/slurm/v0.0.41/reservations/` | 200 | 资源预约（当前无） |
| `/slurmdb/v0.0.41/clusters/` | 200 | 集群信息 |
| `/slurmdb/v0.0.41/users/` | 200 | 当前用户信息 |
| `/slurmdb/v0.0.41/jobs/` | 200 | 历史作业（需带 `user=` / `job_id=` 等参数） |
| `/slurmdb/v0.0.41/qos/` | 200 | QoS 配额列表 |
| `/slurmdb/v0.0.41/tres/` | 200 | 可计费资源类型 |
| `/slurm/v0.0.41/shares` | timeout | 12 秒无响应，不可依赖 |
| `/slurmdb/v0.0.41/diag/` | 500 | 不稳定 |
| `/slurmdb/v0.0.41/accounts/` | 500 | 不稳定 |
| `/slurmdb/v0.0.41/config` | 500 | 不稳定 |

写操作（`POST /job/submit`、`DELETE /job/{job_id}` 等）存在但必须配确认机制，管理类端点（accounts/users/qos/clusters 的 POST/DELETE）不建议开放。

## 7. 数据采集方式说明

- 命令可用性：`command -v` + 逐命令实测（只读直接执行，有副作用仅 help 验证），单命令超时 25 秒。
- 环境信息：`hostname`、`whoami`、`sinfo --version`、`dpkg -l | grep slurm-smd`。
- REST 附录数据：沿用 2026-08-19 的实测结论（见本报告 git 历史）。
- 本报告由本地 Mac 通过 `ssh 107.ustc.edu.cn` 远程采集，采集脚本为一次性执行，未在服务器留存。
