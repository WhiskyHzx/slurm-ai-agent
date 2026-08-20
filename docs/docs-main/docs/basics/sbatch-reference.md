# sbatch 作业提交脚本参考

编写 Slurm 作业提交脚本（.sh）的语法规则和注意事项。依据：集群本机 man sbatch（Slurm 25.11.2）与平台实测。

## #SBATCH 指令语法规则

- `#SBATCH` 指令写在脚本顶部，每行一条，格式：`#SBATCH --选项=值`。
- 解析在遇到第一行非注释、非空白的代码行时停止：所有 `#SBATCH` 必须位于任何命令之前。
- `#SBATCH` 行由 Slurm 直接解析，**不经过 shell，不展开变量**：`--output=$HOME/log.out` 无效，必须写死路径或在命令行传参。
- 同名指令后写的覆盖先写的；命令行 `sbatch --time=...` 的参数优先级最高。
- 选项写法：`--partition=P107-RTX5090`（等号）或 `--partition P107-RTX5090`（空格）均可。

## 常用指令清单（本平台实测值）

- `--job-name`：作业名，建议英文短名（影响日志文件名 %x）。
- `--partition`：分区，区分大小写：P107-RTX5090、P107-A100、GPU-RTX5090、GPU-A100、CPU-6530、CPU-8358P、Students。
- `--account`：计费账户，与分区匹配：P107 系列 → competition；Students → stu；其他 → demo_admin/cmet。
- `--qos`：QoS：qos_p107-rtx5090、qos_p107-a100、qos_stu_default。缺账户/QoS 是 P107 分区提交被拒的常见原因。
- `--nodes`：节点数，单机训练写 1。
- `--ntasks`：任务数，通常 1（每节点一个进程）；多进程作业用任务数组。
- `--cpus-per-task`：每任务 CPU 核数，默认配额 4。
- `--gpus`：GPU 数量（与 `--gres=gpu:N` 等价），默认配额 1，写 0 表示不要 GPU。
- `--time`：时限，分钟数（如 240）或 HH:MM:SS；默认配额 4 小时，超时作业被杀。
- `--output` / `--error`：日志文件，推荐 `logs/%x-%j.out` 与 `logs/%x-%j.err` 分开。
- `--array`：任务数组，如 `1-4`，配合 `$SLURM_ARRAY_TASK_ID` 区分子任务。
- `--chdir`：工作目录（也可在脚本正文 cd）。

## 日志文件与符号替换

文件名模式中可用的符号：

| 符号 | 含义 |
|---|---|
| `%j` | 作业 ID |
| `%x` | 作业名 |
| `%A` / `%a` | 数组作业主 ID / 子任务下标 |
| `%N` | 节点名 |
| `%u` | 用户名 |

注意：日志相对路径基于提交时的工作目录，不是脚本所在目录。推荐写绝对路径，或在脚本开头 `mkdir -p logs` 防止目录不存在导致日志丢失。字面 `%` 需写成 `%%`。

## 脚本正文编写规范

- 开头：`#!/bin/bash` 与 `set -euo pipefail`（出错即停，避免错误被吞后继续跑）。
- 显式 `cd` 到工作目录（`cd $HOME/project`），不要依赖提交时的目录。
- 激活 conda 环境：conda 的激活脚本对未定义变量敏感，用 `set +u` / `set -u` 包裹：

```bash
set +u
source ~/miniconda3/etc/profile.d/conda.sh
conda activate myenv
set -u
```

- python 命令加 `-u`（关闭输出缓冲，日志实时可见）。
- 多步任务用 `srun` 前缀执行计算命令，便于 Slurm 记录 step。
- 脚本必须是 LF 换行（不能是 Windows CRLF），UTF-8 无 BOM 编码。
- stdin 默认是 /dev/null，需要交互输入的程序要重定向。

## 提交与验证

```bash
sbatch myjob.sh              # 提交，返回 Submitted batch job <ID>
squeue -j <ID>               # 查看状态（PD=排队 R=运行）
squeue -j <ID> -o "%.18i %.9P %.8u %.2t %.10M %R"   # R 列即排队原因
sacct -j <ID>                # 本集群受限，历史作业建议用 agent 的 get_jobs_history
cat logs/<作业名>-<ID>.out   # 查看输出日志
scancel <ID>                 # 取消作业
```

## 常见报错与排查

- `Batch job submission failure: Invalid account or account/partition combination`：`--account` 与分区不匹配，核对账户映射。
- `Requested node configuration is not available`：GPU 数量/CPU 超出该分区节点上限。
- `Job violates accounting/QOS policy`：超出 QoS 配额（CPU/GPU/时限），检查 `--time`、`--gpus`、`--cpus-per-task`。
- 作业秒退（COMPLETED 但很快结束）：先看 `logs/*err`，常见是 conda 未激活、路径错误、CRLF 换行符。
- 日志文件不存在：输出目录未创建（`mkdir -p logs`）或相对路径基准不对。
- 排队很久：用 `squeue -o "%R"` 看原因（Resources=等资源释放、Priority=优先级不够、QOS* 达配额上限）。
