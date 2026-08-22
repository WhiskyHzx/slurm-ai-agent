---
page_type: reference
audience: intermediate
status: stable
maintainers:
  - name: docs-team
icon: material/clipboard-check
---

# 命令可用性参考

本页面记录集群 Slurm 命令行与 REST API 的可用性结论，供排查"命令是否存在、是否可用"时参考。

## 环境

| 项目 | 值 |
|---|---|
| Slurm 版本 | `slurm 25.11.2`（smd 发行版） |
| 已安装包 | `slurm-smd` `slurm-smd-client` `slurm-smd-slurmctld` `slurm-smd-slurmd` `slurm-smd-slurmdbd` `slurm-smd-slurmrestd` |
| 集群名（记账库） | `training` |

## 命令行可用性总览

只读命令直接执行验证；有副作用的命令（salloc、sbatch、srun 等）做 `--help` / `--version` 级验证，不真正申请资源。

| 命令 | 状态 | 结论 |
|---|---|---|
| `sacct` | ⚠️ 可执行但查询为空 | 返回码 0，但按用户/时间段/作业号查询均返回空 |
| `sacctmgr` | ✅ 可用 | `show assoc` 正常返回账号/QoS 授权数据 |
| `salloc` | ✅ 存在 | 帮助验证通过 |
| `sattach` | ✅ 存在 | 帮助验证通过 |
| `sbatch` | ✅ 可用 | 提交功能正常 |
| `sbcast` | ✅ 存在 | 帮助验证通过 |
| `scancel` | ✅ 可用 | 取消功能正常 |
| `scontrol` | ✅ 可用 | `show partition` 返回真实分区配置 |
| `scrontab` | ❌ 集群已禁用 | `scrontab: fatal: scrontab is disabled on this cluster` |
| `scrun` | ✅ 存在 | 帮助验证通过 |
| `sdiag` | ✅ 可用 | 返回真实调度统计 |
| `sh5util` | ⚠️ 存在 | 依赖 HDF5 性能采集配置，常规场景无使用需求 |
| `sinfo` | ✅ 可用 | 返回全部分区的真实节点状态 |
| `sprio` | ✅ 可用 | 返回真实作业优先级 |
| `squeue` | ✅ 可用 | 返回全集群真实作业列表 |
| `sreport` | ✅ 可用 | 返回真实用量数据 |
| `srun` | ✅ 存在 | 版本验证通过 |
| `sshare` | ✅ 可用 | 返回真实 fairshare 数据 |
| `sstat` | ✅ 存在 | 需运行中作业才能实际使用 |
| `strigger` | ✅ 可用 | 当前无触发器 |

统计：20 个命令全部存在；17 个完全可用；1 个被禁用（`scrontab`）；2 个受限（`sacct`、`sh5util`）。

## 关键结论

### `scrontab` 被禁用

定时作业（cron 式周期任务）在本集群不可用。有周期任务需求时，应采用"脚本内 sleep 循环 + 长时限作业"或平台侧方案。

### `sacct` 查询受限

`sacct` 命令本身可执行，但对普通用户的记账查询均返回空；同一时间窗口内 `squeue` 与 `sreport` 均有真实数据，说明记账库存在数据，属权限/配置限制。**历史作业查询建议以 REST `/slurmdb/jobs/` 为主**，`sacct` 仅作备用。

### `sacctmgr` 是查授权的可靠途径

```bash
$ sacctmgr -n -p show assoc user=$USER format=User,Account,QOS
```

输出本人所属账号与可用 QoS。一个用户可能属于多个账号（如同时有竞赛账号与学生账号），不同分区可能要求不同账号，见《sbatch 脚本指南》。

### `sprio` / `sshare` 正常可用

优先级诊断与 fairshare 份额查询仅能通过命令行获得（对应 REST 端点不可用或超时）。

## Slurm REST API 速查

认证方式：请求头 `X-SLURM-USER-TOKEN: <SLURM_JWT>`（与 `Authorization: Bearer` 同时发送会 401，只发前者）。

| endpoint | 状态 | 作用 |
|---|---:|---|
| `/slurm/v0.0.41/ping/` | 200 | 探测 slurmctld 是否在线 |
| `/slurm/v0.0.41/diag/` | 200 | 集群整体调度统计 |
| `/slurm/v0.0.41/jobs/` | 200 | 当前作业明细列表 |
| `/slurm/v0.0.41/jobs/state/` | 200 | 精简作业状态列表 |
| `/slurm/v0.0.41/nodes/` | 200 | 节点状态/资源 |
| `/slurm/v0.0.41/partitions/` | 200 | 分区/队列信息 |
| `/slurm/v0.0.41/licenses/` | 200 | 许可证剩余量 |
| `/slurm/v0.0.41/reservations/` | 200 | 资源预约 |
| `/slurmdb/v0.0.41/clusters/` | 200 | 集群信息 |
| `/slurmdb/v0.0.41/users/` | 200 | 当前用户信息 |
| `/slurmdb/v0.0.41/jobs/` | 200 | 历史作业（需带 `user=` / `job_id=` 等参数） |
| `/slurmdb/v0.0.41/qos/` | 200 | QoS 配额列表 |
| `/slurmdb/v0.0.41/tres/` | 200 | 可计费资源类型 |
| `/slurm/v0.0.41/shares` | 超时 | 不可依赖，份额查询用 `sshare` 命令 |
| `/slurmdb/v0.0.41/diag/` | 500 | 不稳定 |
| `/slurmdb/v0.0.41/accounts/` | 500 | 不稳定 |
| `/slurmdb/v0.0.41/config` | 500 | 不稳定 |

写操作（`POST /job/submit`、`DELETE /job/{job_id}` 等）存在但必须配合确认机制使用。
