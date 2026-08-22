---
page_type: how-to
audience: beginner
status: stable
maintainers:
  - name: docs-team
graph:
  next:
    - reference/slurm-commands.md
icon: material/file-document-edit
---

# sbatch 作业脚本指南

本页面说明 `#SBATCH` 指令语法、常用参数、推荐脚本骨架与常见报错。内容依据集群本机 man 手册（与安装版本完全一致）整理。

## `#SBATCH` 指令的硬规则

1. **格式**：一行一条，以 `#SBATCH` 开头，后接任意 sbatch 命令行选项：
   ```bash
   #SBATCH --partition=P107-RTX5090
   #SBATCH --gres=gpu:1
   ```
2. **位置**：必须出现在**第一行非注释、非空白的命令行**之前——之后的 `#SBATCH` 会被忽略。全部放在 shebang 之后、脚本正文之前；写进函数、`if` 块内均无效。
3. **不展开变量**：`#SBATCH` 由 Slurm 直接解析，shell 语法按字面处理。`#SBATCH --output=$HOME/logs/%j.out` 里的 `$HOME` 不会被展开。需要主目录时用 `~`（Slurm 会展开 `~`，不会展开 `$HOME`）。
4. **优先级**：同一选项多次出现时后面的覆盖前面的；命令行参数最后处理，因此 `sbatch --time=1:00:00 x.sh` 覆盖脚本内 `#SBATCH --time`。

## 常用指令

| 指令 | 含义 | 注意事项 |
|---|---|---|
| `-J, --job-name` | 作业名 | 不写则默认用脚本文件名 |
| `-p, --partition` | 分区 | `P107-RTX5090`、`P107-A100`、`GPU-RTX5090`、`GPU-A100`、`CPU-6530`、`CPU-8358P`、`Students`，**区分大小写** |
| `--qos` | QoS（配额策略） | 可用值见资源说明；不写则用账户的 DefaultQOS |
| `-A, --account` | 计费账户 | 多账户用户**建议显式写明**，避免走错默认账户 |
| `--gres=gpu:N` 或 `-G, --gpus=N` | 申请 GPU | 两种写法等价；指定型号：`--gres=gpu:5090:1`；GPU 型号以 `sinfo` 实时查询为准 |
| `--cpus-per-task` | 每任务 CPU 核数 | 学生默认 QoS 上限 4 核，超出报 `QOSMaxCpuPerUserLimit` |
| `--mem` | 内存 | `--mem=0` 表示申请节点全部内存；不写通常按节点默认分配 |
| `-t, --time` | 最长运行时间 `hh:mm:ss` | 学生默认 QoS 上限 4h；到点被杀状态为 `TO` |
| `-N, --nodes` / `-n, --ntasks` | 节点数/任务数 | 单机任务 `1/1` 即可 |
| `-o, --output` / `-e, --error` | 日志文件 | 符号替换见下表；默认两路合并写入 `slurm-%j.out` |
| `-a, --array` | 作业数组 | 数组作业默认输出 `slurm-%A_%a.out` |
| `-d, --dependency` | 依赖 | 如 `afterok:12345`；依赖永不满足时作业会一直挂起 |

## 日志文件名替换符号

| 符号 | 替换为 |
|---|---|
| `%j` | 作业 ID |
| `%x` | 作业名 |
| `%A` / `%a` | 数组主作业 ID / 数组下标 |
| `%N` | 节点名（首节点） |
| `%u` | 用户名 |
| `%%` | 百分号本身 |
| `\` | 其后不处理任何替换符号 |

平台约定写法：`logs/%x-%j.out` 与 `logs/%x-%j.err`。

## 推荐脚本骨架

```bash
#!/bin/bash
#SBATCH --job-name=my-train          # 作业名
#SBATCH --partition=P107-RTX5090     # 分区（区分大小写）
#SBATCH --account=<你的账户>          # 账户：显式写明，不依赖默认值
#SBATCH --qos=<账户对应的qos>         # QoS：显式写明
#SBATCH --gres=gpu:1                 # GPU 数量（指定型号: gpu:5090:1）
#SBATCH --cpus-per-task=4            # CPU 核数（默认 QoS 上限 4）
#SBATCH --time=04:00:00              # 最长运行时间（默认 QoS 上限 4h）
#SBATCH --output=logs/%x-%j.out      # 标准输出
#SBATCH --error=logs/%x-%j.err       # 标准错误

set -euo pipefail          # 出错即停，防止错误静默传染

cd ~/projects/my-project   # 显式 cd 绝对路径（见注意事项第 1 条）

set +u                      # conda activate 在 set -u 下报 unbound variable
source ~/miniconda3/etc/profile.d/conda.sh
conda activate py310
set -u

nvidia-smi                  # GPU 作业先自检，日志里留证据
python -u train.py          # -u 关闭输出缓冲，保证日志实时可见
```

## 脚本正文注意事项

1. **工作目录陷阱**：批处理作业的初始工作目录 = **sbatch 提交命令执行时的目录**（不是脚本所在目录）。必须用绝对路径显式 `cd`。
2. **日志目录陷阱**：`-o logs/%x-%j.out` 是相对 **sbatch 执行时的目录**解析的，脚本内 `cd` 和 `mkdir -p logs` 都不影响它——**提交前必须确保提交目录下已存在 `logs/`**，否则作业启动即失败。
3. **`set -euo pipefail`**：任何命令失败、引用未定义变量、管道任一环节失败都会终止脚本，避免带病运行产出错误结果。
4. **conda 激活与 `set -u` 冲突**：`conda activate` 内部会引用未定义变量，必须用 `set +u ... set -u` 包裹。
5. **Python 输出缓冲**：不加 `python -u`（或不设 `PYTHONUNBUFFERED=1`）时，stdout 按块缓冲，查看日志看不到实时进度，容易误判作业卡死。
6. **stdin 是 `/dev/null`**：脚本内任何交互式输入立即 EOF，不能写 `read`。
7. **超时与 checkpoint**：`-t` 到点作业被 SIGTERM 终止（状态 `TO`），长训练必须定期保存断点。
8. **退出码即作业状态**：脚本最终退出码非 0 → 作业状态 `F`。排错顺序：先看 `.err` 再看 `.out`。
9. **文件格式**：Linux 换行符 LF（Windows 编辑器的 CRLF 会报 `bad interpreter: ...^M`）、UTF-8 编码无 BOM。
10. **无需可执行权限**：sbatch 读取脚本内容执行，不要求 `chmod +x`。
11. **模块环境**：如需系统级依赖，在 conda 之前 `module load <name>`。
12. **作业数组与依赖**：批量实验用 `-a 1-10%2`（并发限 2）；串行链用 `-d afterok:<id>`，注意无效依赖会让作业永久挂起。

## 提交与验证

```bash
sbatch scripts/train.sbatch     # 提交，立即返回 job_id（资源不保证立即可用）
squeue -u "$USER"               # 状态：PD 排队 / R 运行 / F 失败 / TO 超时 / CD 完成
tail -n 50 logs/train-*.out     # 查看实时输出
scancel <job_id>                # 写错/卡死及时取消
```

## 常见报错速查

| 报错 | 原因 | 处理 |
|---|---|---|
| `QOSMaxWallDurationPerJobLimit` | `-t` 超过 QoS 时长上限 | 调小 `-t` 或申请更高 QoS |
| `QOSMaxCpuPerUserLimit` | CPU 核数超配额 | 调小 `--cpus-per-task` |
| `PartitionConfig` / `Invalid account` | 账户与分区不匹配 | 确认账户与分区对应关系（`sacctmgr show assoc` 查询） |
| `bad interpreter ...^M` | Windows CRLF 换行 | `dos2unix` 或编辑器改 LF |
| 日志文件不存在、作业直接 F | `logs/` 目录不存在（相对提交时目录） | 提交前 `mkdir -p logs` |
| 作业 `TO` | 运行时间到点被杀 | 加大 `-t`（不超过 QoS 上限）或加 checkpoint |
| 一直 `PD` | 资源不足/优先级低 | `scontrol show job` 看 Reason，`sprio` 看优先级 |
