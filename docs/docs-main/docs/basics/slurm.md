---
page_type: reference
audience: beginner
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: core
  next:
    - basics/jobs.md
    - basics/faq.md
  related:
    - overview/resources.md
  advanced: []
icon: material/sitemap-outline
---

# Slurm 速查

本页是常用命令和脚本字段速查。第一次使用建议先读[提交任务](jobs.md)，再回到这里复制命令。

## 常用命令

运行环境：平台 Shell。

```bash
sbatch scripts/train.sbatch      # 提交批处理作业
squeue -u "$USER"                # 查看自己的作业
scancel <job_id>                 # 取消作业
scontrol show job <job_id>       # 查看作业详情
scontrol show part               # 查看分区限制
```

## 交互式调试命令

交互式命令适合短时间检查环境、GPU、路径和依赖。它仍然会占用调度资源，用完应立即 `exit` 退出。

CPU 调试会话：

```bash
srun -p Students --qos=qos_stu_default -c 1 -t 00:10:00 --pty bash
```

GPU 调试会话：

```bash
srun -p Students --qos=qos_stu_default --gres=gpu:1 -c 1 -t 00:10:00 --pty bash
```

进入交互式会话后常用检查：

```bash
hostname
pwd
python -V
nvidia-smi
exit
```

??? note "参数说明"
    `-p` 是分区，`--qos` 是 QOS，`-c` 是 CPU 核心数，`--gres=gpu:1` 表示申请 1 张 GPU，`-t` 是最长运行时间，`--pty bash` 表示进入交互式 Shell。具体参数必须以当前账号可用选项为准。

如果命令显示作业在排队，说明资源暂时没有立即分配。需要更长运行时间或更多资源时，应按平台流程申请额外算力。

查看当前分区限制：

```bash
scontrol show part
```

可以在“登录集群 - 命令行”中使用这个命令查看当前分区的运行时间等限制。需要更长运行时间时，应按平台流程联系管理员或助教确认。

## 作业状态

常见状态含义：

| 状态 | 含义 | 建议动作 |
| --- | --- | --- |
| `PD` | 排队等待资源 | 查看等待原因，确认资源申请是否过大 |
| `R` | 正在运行 | 查看日志是否持续更新 |
| `CG` | 正在收尾 | 等待输出文件落盘 |
| `CD` | 已完成 | 检查日志和结果 |
| `F` | 失败 | 先读 `.err` 和 `.out` |
| `CA` | 已取消 | 确认是否由自己或策略取消 |

## 常用 SBATCH 字段

```bash
#SBATCH -J my-job
#SBATCH -p Students
#SBATCH --qos=qos_stu_default
#SBATCH --gres=gpu:<gpu_type_or_count>
#SBATCH --cpus-per-task=<cpu_count>
#SBATCH --mem=<memory>
#SBATCH -t <hh:mm:ss>
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err
```

字段说明：

| 字段 | 作用 |
| --- | --- |
| `-J` | 作业名，建议短而可辨认 |
| `-p` | partition，决定进入哪个资源分区 |
| `--qos` | QOS，决定资源限制和调度策略 |
| `--gres` | 申请 GPU 等通用资源 |
| `--cpus-per-task` | 为单个任务申请 CPU 核数 |
| `--mem` | 申请内存 |
| `-t` | 最长运行时间 |
| `-o` / `-e` | 标准输出和错误日志 |

`--gres=gpu:1` 表示申请 1 张 GPU，`-c 1` 表示申请 1 个 CPU 核心。是否需要指定 GPU 类型、分区和 QOS，应以当前平台界面为准。

## 当前默认配置

统一身份认证登录的同学通常默认使用：

```bash
#SBATCH -p Students
#SBATCH --qos=qos_stu_default
```

默认初始配额为 `4CPU/1GPU/4h`。这意味着：

- 默认 QOS 下作业最长运行时间为 4 小时。
- 默认最多申请 1 张 GPU。
- 默认 CPU 上限为 4 核。

如果提交时看到 `QOSMaxWallDurationPerJobLimit`，通常是运行时间超过当前 QOS 限制。如果看到 `QOSMaxCpuPerUserLimit`，通常是申请的 CPU 核数超过当前用户/QOS 限制，先把 `--cpus-per-task` 调到不超过默认上限再试。

如果平台允许指定 GPU 类型，可以使用下面的写法：

```bash
#SBATCH --gres=gpu:5090:1
#SBATCH --gres=gpu:A100:1
```

GPU 型号和可用节点会变化。提交前请以平台界面、`sinfo`、`scontrol show part` 或管理员说明为准。

## 读取分区输出

`scontrol show part` 输出很长，初学者优先看这些字段：

| 字段 | 含义 |
| --- | --- |
| `PartitionName` | 分区名称，例如 `Students`、`CPU-6530`、`GPU-RTX5090`。 |
| `AllowAccounts` | 哪些账户组可使用该分区。 |
| `AllowQos` | 该分区允许的 QOS。 |
| `Nodes` | 分区包含的节点范围。 |
| `State` | 分区整体状态。 |
| `TRES` | 分区总资源量，包括 CPU、内存、GPU 等。 |

`sinfo` 更适合快速看当前节点状态：

| 状态 | 含义 |
| --- | --- |
| `idle` | 节点空闲，通常更容易分配资源。 |
| `mix` | 节点部分资源已被使用。 |
| `comp` | 节点资源较满或正在完成作业。 |
| `down` / `down*` | 节点或分区不可用，不应作为可提交资源。 |
| `drng` | 节点正在 draining，通常不再接收新作业或即将维护。 |

一次采集示例显示，`Students` 分区允许的 QOS 包括 `qos_stu_default`、`qos_stu_small`、`qos_stu_medium`、`qos_stu_medium_2gpu`、`qos_stu_long`、`qos_stu_cpu_long` 等；但账号能否使用某个 QOS，以当前平台授权和提交结果为准。

## 排查顺序

1. `squeue -u "$USER"` 看作业是否还在排队或运行。
2. `scontrol show job <job_id>` 看资源申请和等待原因。
3. `tail -n 80 logs/<file>.err` 看错误日志。
4. `tail -n 80 logs/<file>.out` 看程序输出。
5. 若任务申请了 GPU，在作业中输出 `nvidia-smi` 结果。

## 注意

具体 GPU 型号、节点名、partition 和 QOS 都属于动态事实，提交前应以平台页面、`sinfo`、`scontrol show part` 或管理员说明为准。后续确认后可以补一张“当前平台常用资源配置表”。
