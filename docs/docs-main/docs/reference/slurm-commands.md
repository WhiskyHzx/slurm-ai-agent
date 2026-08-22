---
page_type: reference
audience: beginner
status: stable
maintainers:
  - name: docs-team
icon: material/console
---

# Slurm 命令行工具总览

本页面汇总集群上可用的全部 Slurm 客户端命令并逐一说明用途。信息来源为集群本机安装的软件包与官方 man 手册，两者内容一致（在线文档为 man 的网页版，见 `https://slurm.schedmd.com/<命令>.html`）。

## 命令总览

客户端命令集中在 `slurm-smd-client` 软件包，安装于 `/usr/bin` 下，共 20 个：

| 命令 | 作用 |
|------|-----------|
| `sacct` | 查看已完成作业的记账/历史数据 |
| `sacctmgr` | 查看/修改账号、关联、QoS 管理数据 |
| `salloc` | 申请节点并进入交互式 shell |
| `sattach` | 附加到运行中的作业步骤 |
| `sbatch` | 提交批处理脚本作业 |
| `sbcast` | 向作业节点传输文件 |
| `scancel` | 取消/信号作业或作业步骤 |
| `scontrol` | 查看/修改 Slurm 配置与状态 |
| `scrontab` | 管理 Slurm 定时作业表 |
| `scrun` | OCI 容器运行时代理 |
| `sdiag` | 调度诊断工具 |
| `sh5util` | 合并 HDF5 性能文件 |
| `sinfo` | 查看节点与分区信息 |
| `sprio` | 查看作业调度优先级构成 |
| `squeue` | 查看队列中作业信息 |
| `sreport` | 从记账数据生成报表 |
| `srun` | 运行并行作业 |
| `sshare` | 列出关联份额（fairshare） |
| `sstat` | 显示运行作业/步骤状态 |
| `strigger` | 设置/获取/清除触发器 |

## 各命令说明

### `sacct` — 作业/步骤记账数据

查看**已结束**作业的记账数据（CPU 时间、内存、结束时间、退出码等）：

```bash
sacct -j <jobid>                      # 查看指定作业
sacct --starttime=08-01 --endtime=now # 按时间段查
sacct -o JobID,Elapsed,MaxRSS,State   # 自定义输出列
```

### `sacctmgr` — 账号/关联/QoS 管理

管理用户、账号（account）、关联（association）、QoS、集群等记账实体，数据存于 slurmdbd 数据库。查询本人授权：

```bash
sacctmgr -p show assoc user=$USER format=User,Account,QOS,DefaultQOS
sacctmgr -n show qos format=name
scontrol show qos <qos名>
```

### `salloc` — 申请节点进入交互 shell

申请节点资源，分配后进入交互式 shell；适合交互式调试：

```bash
salloc -N 1 -n 4 -p P107-RTX5090 --time=01:00:00
```

分配后通常配合 `srun` 运行并行命令，用 `exit` 释放资源。

### `sattach` — 附加到作业步骤

把终端附加到一个运行中的 job step，查看其输出或发送输入。

### `sbatch` — 提交批处理脚本

最常用的提交方式。提交一个脚本文件，Slurm 将其作为作业运行，脚本内用 `#SBATCH` 指定资源需求：

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
#SBATCH --output=logs/%x-%j.out
```

### `sbcast` — 向节点传输文件

把本地文件传到作业分配的所有节点的本地磁盘，用于加速共享存储场景下的读取。

### `scancel` — 取消/信号作业

```bash
scancel <jobid>               # 取消作业
scancel -u $USER              # 取消当前用户所有作业
scancel --signal=<sig> <jobid>
```

### `scontrol` — 查看/修改配置

诊断与控制的通用工具，可查看作业、节点、分区、QoS：

```bash
scontrol show job <jobid>       # 作业详情（含 QoS）
scontrol show node <nodename>   # 节点详情
scontrol show qos <qos名>       # QoS 详情（配额）
scontrol show partition <名称>
```

### `scrontab` — 定时作业

把 cron 语法转换并注册为 Slurm 周期作业。**本集群已禁用该功能**，定时需求见《命令可用性参考》中的说明。

### `scrun` — OCI 容器运行时代理

作为 OCI runtime 兼容层，让容器运行时把请求转成 Slurm 作业，属高级用法。

### `sdiag` — 调度诊断

输出调度器统计（运行中/挂起作业数、平均等待时间、回填效率等）：

```bash
sdiag
```

### `sh5util` — 合并 HDF5 文件

合并按节点分布的 HDF5 性能采集文件。仅在启用 acct_gather_profile 采集且节点较多时使用。

### `sinfo` — 节点与分区信息

查看分区/节点当前状态（idle/alloc）、资源总量、节点数：

```bash
sinfo                                     # 全景
sinfo -p P107-RTX5090 -o "%n %t %c %G"    # 自定义列
sinfo -N -r                               # 只列可用节点
```

### `sprio` — 调度优先级构成

查看作业调度优先级各因子（age、fairshare、QOS、partition 等）：

```bash
sprio -j <jobid>
sprio -w          # 简写权重
```

### `squeue` — 队列中的作业信息

最常用的查看命令，列出排队/运行作业：

```bash
squeue                                  # 所有作业
squeue -u $USER                         # 只看自己的
squeue -j <jobid>
squeue -o "%.22i %.27j %.20Q %T"        # 含 QoS 列（Q）
```

常见状态：`PD`（排队）、`R`（运行）、`S`（挂起）、`CG`（完成中）。

### `sreport` — 记账报表

按用户/账号/集群生成 CPU 时间、利用率等统计报表：

```bash
sreport cluster Utilization start=... end=now -t hours
```

按用户查询应使用 `sreport user Top`（`user Utilization` 不是合法报表名）。

### `srun` — 运行并行作业

在前台直接运行并行作业，常配合 `salloc` 使用或在作业脚本内启动作业步骤：

```bash
srun -N 2 -n 8 ./myprogram
srun --gres=gpu:1 python train.py
```

### `sshare` — 关联份额

显示各关联的 fairshare（公平份额）与使用情况：

```bash
sshare -u $USER
```

### `sstat` — 运行作业状态

显示**运行中**作业/步骤的实时状态（内存、CPU、能耗）：

```bash
sstat -j <jobid>
```

### `strigger` — 触发器

注册/查看/删除当节点 down/drain、作业超时等事件发生时的回调命令：

```bash
strigger --get
```

## 智能助手能力与底层命令对应

| 智能助手能力 | 底层命令 |
|--------------|---------|
| 查看资源配额 | `scontrol show qos` / `sacctmgr show qos` |
| 查看作业 | `squeue` |
| 查看作业状态 | `scontrol show job` |
| 提交作业 | `sbatch` |
| 取消作业 | `scancel` |
| 集群状态 | `sdiag` / `sinfo` |
