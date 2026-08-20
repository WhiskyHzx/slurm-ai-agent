# Slurm 命令全量说明（来源：① 服务器本机软件包 ② 官方文档/man 手册）

本文面向 `107.ustc.edu.cn`（常州超算 SCC），汇总 **所有可用的 Slurm 命令行工具** 并逐一解释用途。信息来源严格限定为：

1. **① Slurm 集群本机安装的软件包**：`/usr/bin` 下，归属 `slurm-smd-client` 等包；
2. **② Slurm 官方文档 / man 手册**：本机 `man <命令>`，在线文档 `https://slurm.schedmd.com/`。

---

## 一、信息来源核实

### ① 本机软件包（包名 → 命令清单）

在 `107.ustc.edu.cn` 上执行 `dpkg -l | grep slurm` 得到已装包：

```
slurm-smd
slurm-smd-client
slurm-smd-slurmctld
slurm-smd-slurmd
slurm-smd-slurmdbd
slurm-smd-slurmrestd
```

其中 **客户端命令** 集中在 `slurm-smd-client` 包，`/usr/bin/` 共 20 个命令。

### ② 命令行与 man 手册

每个命令的用法与含义定义在：
- 本机：`man <命令>`（如 `man sacctmgr` / `man squeue`），位于 `/usr/share/man/man1/`
- 在线：`https://slurm.schedmd.com/<命令>.html`

二者内容一致（官网即 man 的网页版）。下文每个命令按其 man NAME 语义 + 常见用法整理。

---

## 二、Slurm 客户端命令总览（20 个）

| 命令 | 一句话作用 | 章节 |
|------|-----------|------|
| `sacct` | 查看已完成作业的记账/历史数据 | §1 |
| `sacctmgr` | 查看/修改账号、关联、QoS 管理数据 | §2 |
| `salloc` | 申请节点并进入交互式 shell | §3 |
| `sattach` | 附加到运行中的作业步骤 | §4 |
| `sbatch` | 提交批处理脚本作业 | §5 |
| `sbcast` | 向作业节点传输文件 | §6 |
| `scancel` | 取消/信号作业或作业步骤 | §7 |
| `scontrol` | 查看/修改 Slurm 配置与状态 | §8 |
| `scrontab` | 管理 Slurm 定时作业表 | §9 |
| `scrun` | OCI 容器运行时代理 | §10 |
| `sdiag` | 调度诊断工具 | §11 |
| `sh5util` | 合并 HDF5 性能文件 | §12 |
| `sinfo` | 查看节点与分区信息 | §13 |
| `sprio` | 查看作业调度优先级构成 | §14 |
| `squeue` | 查看队列中作业信息 | §15 |
| `sreport` | 从记账数据生成报表 | §16 |
| `srun` | 运行并行作业 | §17 |
| `sshare` | 列出关联份额（fairshare） | §18 |
| `sstat` | 显示运行作业/步骤状态 | §19 |
| `strigger` | 设置/获取/清除触发器 | §20 |

此外还有 `srun` 配套的并行执行说明、以及当前**未**作为单独命令出现的（如 `srun` 等）。

---

## §1 `sacct` — 作业/步账记账数据

`man` 描述：`sacct - displays accounting data for all jobs and job steps in the Slurm job accounting account database.`

- 用途：查看**已结束**作业的记账数据（CPU 时间、内存、结束时间、退出码等）。
- 常用：
```bash
sacct -j <jobid>                      # 查看指定作业
sacct --starttime=08-01 --endtime=now # 按时间段查
sacct -o JobID,Elapsed,MaxRSS,State   # 自定义输出列
```

---

## §2 `sacctmgr` — 账号/关联/QoS 管理

`man` 描述：`sacctmgr - Used to view and modify Slurm account information.`

- 用途：管理用户、账号（account）、关联（association）、QoS、集群等记账实体，数据存于 slurmdbd 数据库。
- 与"当前用户 QoS 权限"直接相关：
```bash
sacctmgr -p show assoc user=$(whoami) format=User,Account,QOS,DefaultQOS
sacctmgr -n show qos format=name
scontrol show qos <qos名>
```
- 常用实体：`account` `association` `cluster` `coordinator` `qos` `reservation` `user` `tres`。

---

## §3 `salloc` — 申请节点进入交互 shell

`man` 描述：`salloc - Obtain a Slurm job allocation (a set of nodes), execute a command.`

- 用途：申请独占节点资源，分配后进入交 `.shell` 或执行指定命令；适合交互式调试。
```bash
salloc -N 1 -n 4 -p P107-RTX5090 --time=01:00:00
```
分配后通常配合 `srun` 运行并行命令，用时 `exit` 释放。

---

## §4 `sattach` — 附加到作业步骤

`man` 描述：`sattach - Attach to a Slurm job step.`

- 用途：把终端附加到一个运行中的 job step（需 `srun --input=none` 等支持），查看其输出或发送输入。

---

## §5 `sbatch` — 提交批处理脚本

`man` 描述：`sbatch - Submit a batch script to Slurm.`

- 用途：**最常用提交方式**。提交一个脚本文件，Slurm 将其作为作业运行，脚本内用 `#SBATCH` 指定资源需求。
```bash
sbatch --qos qos_p107-rtx5090 --cpus-per-task 4 script.sh
```
脚本头部示例：
```bash
#!/bin/bash
#SBATCH --partition=P107-RTX5090
#SBATCH --qos=qos_p107-rtx5090
#SBATCH --gres=gpu:1
#SBATCH --time=02:00:00
#SBATCH --output=out_%j.log
```

---

## §6 `sbcast` — 向节点传输文件

`man` 描述：`sbcast - transmit a file to the nodes allocated to a Slurm job.`

- 用途：把本地文件传到作业分配的所有节点的本地磁盘，加速共享介质场景。

---

## §7 `scancel` — 取消/信号作业

`man` 描述：`scancel - Used to signal jobs or job steps that are under the control of Slurm.`

- 用途：取消或给作业发送信号。
```bash
scancel <jobid>              # 取消作业
scancel -u $USER             # 取消当前用户所有作业
scancel --signal=<sig> <jobid>
```

---

## §8 `scontrol` — 查看/修改 Slurm 配置

`man` 描述：`scontrol - view or modify Slurm configuration and state.`

- 用途：诊断与控制的瑞士军刀，可查看作业、节点、分区、QoS，也可（有权限时）修改。
```bash
scontrol show job <jobid>       # 作业详情（含 QoS）
scontrol show node <nodename>   # 节点详情
scontrol show qos <qos名>       # QoS 详情（配额）
scontrol show partition <名称>
scontrol token lifespan=86400   # 生成 JWT（本项目自动刷新用）
```

---

## §9 `scrontab` — 定时作业

`man` 描述：`scrontab - manage Slurm crontab files.`

- 用途：把 cron 语法转换并注册为 Slurm 周期作业，语法类似 `crontab -e` / `scrontab -l`。

---

## §10 `scrun` — OCI 容器运行时代理

`man` 描述：`scrun - an OCI runtime proxy for Slurm.`

- 用途：作为 OCI runtime 兼容层，让容器运行时（docker/podman 的 runc）把请求转成 Slurm 作业。高级用法。

---

## §11 `sdiag` — 调度诊断

`man` 描述：`sdiag - Scheduling diagnostic tool for Slurm.`

- 用途：输出调度器统计（运行中/挂起作业数、平均等待时间、回填效率等），对应项目 REST 的 `get_diag`。
```bash
sdiag
```

---

## §12 `sh5util` — 合并 HDF5 文件

`man` 描述：`sh5util - Tool for merging HDF5 files from the acct_gather_profile plugin.`

- 用途：合并按节点分布的 HDF5 性能采集文件。仅在用 acct_gather_profile 且节点较多时用。

---

## §13 `sinfo` — 节点与分区信息

`man` 描述：`sinfo - View information about Slurm nodes and partitions.`

- 用途：查看分区/节点当前状态（idle/alloc）、资源总量、节点数。
```bash
sinfo                         # 全景
sinfo -p P107-RTX5090 -o "%n %t %c %G"  # 自定义列
sinfo -N -r                    # 只列可用的
```

---

## §14 `sprio` — 调度优先级构成

`man` 描述: `sprio - view the factors that comprise a job's scheduling priority.`

- 用途: 查看作业调度优先级各因子（age、fairshare、QOS、partition 等）。
```bash
sprio -j <jobid>
sprio -w                        # 简写权重
```

---

## §15 `squeue` — 队列中的作业信息

`man` 描述: `squeue - view information about jobs located in the Slurm scheduling queue.`

- 用途: **最常用查看命令**, 列出排队/运行作业。
```bash
squeue                           # 所有
squeue -u $USER                  # 只看自己
squeue -j <jobid>
squeue -o "%.22i %.27j %.20Q %T" # 含 QoS 列(Q)
```

状态: `PD`(排队) `R`(运行) `S`(挂起) `CG`(完成中)。

---

## §16 `sreport` — 记账报表

`man` 描述: `sreport - Generate reports from the slurm accounting data.`

- 用途: 按用户/账号/集群生成 CPU 时间、利用率等统计报表。
```bash
sreport cluster Utilization
```

---

## §17 `srun` — 运行并行作业

`man` 描述: `srun - Run parallel jobs.`

- 用途: 在前台直接运行并行作业，常配合 `salloc` 或直接使用。
```bash
srun -N 2 -n 8 ./myprogram
srun --qos qos_p107-rtx5090 --gres=gpu:1 python train.py
```

---

## §18 `sshare` — 关联份额

`man` 描述: `sshare - Tool for listing the shares of associations to a cluster.`

- 用途: 显示各 association 的 fairshare（公平份额）与使用情况, 配合公平调度。
```bash
sshare -u $USER
```

---

## §19 `sstat` — 运行作业状态

`man` 描述: `sstat - Display the status information of a running job/step.`

- 用途: 显示**运行中**作业/步骤的实时状态（内存、CPU、能耗）。
```bash
sstat -j <jobid>
```

---

## §20 `strigger` — 触发器

`man` 描述: `strigger - Used to set, get or clear Slurm trigger information.`

- 用途: 注册/查看/删除当节点 down/drain、作业超时等事件发生时的回调命令。
```bash
strigger --set --nodetype down --nodes=node01 --program=script
```

---

## 三、本项目 AI agent 常用命令的对应关系

| AI agent 能力 | 底层命令 | 项目 REST 封装 |
|--------------|---------|--------------|
| 查看资源配额 | `scontrol show qos` / `sacctmgr show qos` | `get_qos` |
| 查看作业 | `squeue` | `list_jobs` |
| 查看作业状态 | `scontrol show job` | `get_job` |
| 提交作业 | `sbatch` | `submit_job` |
| 取消作业 | `scancel` | `cancel_job` |
| 集群状态 | `sdiag` / `sinfo` | `get_diag` / `list_nodes` |
| 认证 | `scontrol token` | `scontrol token lifespan=86400` |

---

*数据采集于 107.ustc.edu.cn: `dpkg -L slurm-smd-client`、`man <命令>`、`https://slurm.schedmd.com/`。*
