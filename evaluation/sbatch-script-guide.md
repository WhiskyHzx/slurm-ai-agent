# sbatch 作业提交脚本：语法与注意事项（查验报告）

日期：2026-08-21  
适用集群：107.ustc.edu.cn（tradmin-01/02，Slurm 25.11.2 smd 发行版，记账集群名 training）  
本地项目：`/Users/mac/work/slurm-ai-agent`（与远程 `/home/scc/pb25111697/slurm-ai-agent` 绑定）

## 0. 权威来源（本目录随附文件）

| 文件 | 来源 | 权威性 |
|---|---|---|
| `sbatch-manual-107-local.txt` | **107 集群本机** `man sbatch` 导出（2775 行） | ★★★ 对本集群最权威——与安装版本 25.11.2 完全一致 |
| `sbatch-manual-official.html` | https://slurm.schedmd.com/sbatch.html 下载 | ★★ 官方在线版，但当前对应 26.05（比集群新一个大版本）；25.11 归档暂未上网，作交叉参考 |
| 本报告 | 四源交叉验证：上述两份手册 + 平台文档（`docs/docs-main/docs/basics/jobs.md`、`slurm.md`）+ 项目模板（`config/templates/`）+ 集群实测 | 综合结论 |

凡两份手册有出入之处，以集群本机版（25.11.2）为准。

---

## 1. `#SBATCH` 指令的四条硬规则（man 原文验证）

1. **格式**：一行一条，以 `#SBATCH` 开头，后接任意 sbatch 命令行选项：
   ```bash
   #SBATCH --partition=P107-RTX5090
   #SBATCH --gres=gpu:1
   ```
2. **位置**：必须在遇到**第一行非注释、非空白的命令行**之前——之后的 `#SBATCH` 会被 Slurm 忽略。全部放在 shebang（`#!/bin/bash`）之后、脚本正文之前；写进函数、`if` 块内均无效。
3. **不展开变量**：`#SBATCH` 由 Slurm 直接解析，**shell 语法按字面处理**。`#SBATCH --output=$HOME/logs/%j.out` 里的 `$HOME` 不会被展开（会生成字面 `$HOME` 目录或直接失败）。需要主目录时用 `~`（Slurm 会展开 `~`，不会展开 `$HOME`）。
4. **优先级**：同一选项多次出现时**后面的覆盖前面的**；命令行参数最后处理，因此 `sbatch --time=1:00:00 x.sh` 覆盖脚本内 `#SBATCH --time`。

## 2. 常用指令清单（已标注本集群实测值）

| 指令 | 含义 | 本集群注意事项 |
|---|---|---|
| `-J, --job-name` | 作业名 | 不写则默认用脚本文件名（stdin 提交时为 `sbatch`） |
| `-p, --partition` | 分区 | `P107-RTX5090`、`P107-A100`、`GPU-RTX5090`、`GPU-A100`、`CPU-6530`、`CPU-8358P`、`Students`，**区分大小写** |
| `--qos` | QoS（配额策略） | 当前用户可用：`qos_p107-rtx5090`、`qos_p107-a100`、`qos_stu_default`；不写则用账户的 DefaultQOS |
| `-A, --account` | 计费账户 | 当前用户有两个：`competition`（P107 系列分区使用）、`stu`（Students 分区使用）；多账户用户**建议显式写明**，避免走错默认账户 |
| `--gres=gpu:N` 或 `-G, --gpus=N` | 申请 GPU | 两种写法等价（man 确认 `-G, --gpus=[type:]<number>`）；指定型号：`--gres=gpu:5090:1` 或 `--gpus=5090:1`；GPU 型号是动态事实，以 `sinfo` 实时查询为准 |
| `--cpus-per-task` | 每任务 CPU 核数 | 学生默认 QoS 上限 4 核，超出报 `QOSMaxCpuPerUserLimit` |
| `--mem` | 内存 | `--mem=0` 表示申请节点全部内存；不写通常按节点默认分配 |
| `-t, --time` | 最长运行时间 `hh:mm:ss` | 学生默认 QoS 上限 4h，超出报 `QOSMaxWallDurationPerJobLimit`；到点被杀状态为 `TO` |
| `-N, --nodes` / `-n, --ntasks` | 节点数/任务数 | 单机任务 `1/1` 即可 |
| `-o, --output` / `-e, --error` | 日志文件 | 符号替换见 §3；默认两路合并写入 `slurm-%j.out` |
| `-a, --array` | 作业数组 | 数组作业默认输出 `slurm-%A_%a.out` |
| `-d, --dependency` | 依赖 | 如 `afterok:12345`；依赖永不满足时作业会一直挂起（除非 `--kill-on-invalid-dep=yes`） |
| `-x, --exclude` / `-w, --nodelist` | 排除/指定节点 | 一般无需使用 |

## 3. 日志文件名替换符号（man 验证）

| 符号 | 替换为 |
|---|---|
| `%j` | 作业 ID |
| `%x` | 作业名 |
| `%A` / `%a` | 数组主作业 ID / 数组下标 |
| `%N` | 节点名（首节点） |
| `%u` | 用户名 |
| `%%` | 百分号本身 |
| `\` | 其后不处理任何替换符号 |

默认输出文件：`slurm-%j.out`（普通作业）、`slurm-%A_%a.out`（数组作业）。平台文档约定写法：`logs/%x-%j.out` 与 `logs/%x-%j.err`。

## 4. 推荐脚本骨架（含逐条解释）

```bash
#!/bin/bash
#SBATCH --job-name=my-train          # 作业名
#SBATCH --partition=P107-RTX5090     # 分区（区分大小写）
#SBATCH --account=competition        # 账户：P107 系列用 competition
#SBATCH --qos=qos_p107-rtx5090       # QoS：显式写明，不赌默认值
#SBATCH --gres=gpu:1                 # GPU 数量（指定型号: gpu:5090:1）
#SBATCH --cpus-per-task=4            # CPU 核数（默认 QoS 上限 4）
#SBATCH --time=04:00:00              # 最长运行时间（默认 QoS 上限 4h）
#SBATCH --output=logs/%x-%j.out      # 标准输出
#SBATCH --error=logs/%x-%j.err       # 标准错误

set -euo pipefail          # ① 出错即停，防止错误静默传染

cd ~/projects/my-project   # ② 显式 cd 绝对路径（见 §5 第 1 条）

set +u                      # ③ conda activate 在 set -u 下报 unbound variable
source ~/miniconda3/etc/profile.d/conda.sh
conda activate py310
set -u

nvidia-smi                  # ④ GPU 作业先自检，日志里留证据
python -u train.py          # ⑤ -u 关闭输出缓冲，保证日志实时可见
```

## 5. 脚本正文注意事项（按重要性排序）

1. **工作目录陷阱**：批处理作业的初始工作目录 = **sbatch 提交命令执行时的目录**（不是脚本所在目录）。不 `cd` 的话，脚本里所有相对路径都基于提交时的位置。务必用绝对路径显式 `cd`。
2. **日志目录陷阱**：`-o logs/%x-%j.out` 是相对 **sbatch 执行时的目录**解析的，脚本内 `cd` 和 `mkdir -p logs` 都不影响它——**提交前必须确保提交目录下已存在 `logs/`**，否则作业启动即失败。这是平台文档"提交前检查"的第一条。
3. **`set -euo pipefail`**：任何命令失败、引用未定义变量、管道任一环节失败都会终止脚本，避免带病运行产出错误结果。
4. **conda 激活与 `set -u` 冲突**：`conda activate` 内部会引用未定义变量，必须用 `set +u ... set -u` 包裹（见骨架 ③）。
5. **Python 输出缓冲**：不加 `python -u`（或不设 `PYTHONUNBUFFERED=1`）时，stdout 按块缓冲，`tail` 日志看不到实时进度，误以为作业卡死。
6. **stdin 是 `/dev/null`**（man 确认）：脚本内任何交互式输入立即 EOF，不能写 `read`。
7. **超时与 checkpoint**：`-t` 到点作业被 SIGTERM 终止（状态 `TO`），长训练必须定期保存断点。
8. **退出码即作业状态**：脚本最终退出码非 0 → 作业状态 `F`。排错顺序：先看 `.err` 再看 `.out`（智能体可用 `read_job_log` 工具）。
9. **文件格式**：Linux 换行符 LF（Windows 编辑器的 CRLF 会报 `bad interpreter: ...^M`）、UTF-8 编码无 BOM。
10. **无需可执行权限**：sbatch 是读取脚本内容执行（经 `/bin/bash`），不要求 `chmod +x`（加了也无害）。
11. **模块环境**：如需系统级依赖，在 conda 之前 `module load <name>`（本集群以 conda 方案为主）。
12. **作业数组与依赖**：批量实验用 `-a 1-10%2`（并发限 2）；串行链用 `-d afterok:<id>`，注意无效依赖会让作业永久挂起。

## 6. 与本项目智能体相关的三个特殊注意点

1. **REST 提交只透传 4 个参数**：`submit_job` 工具只传 script/partition/name/nodes/time_limit；**GPU、CPU、内存、QoS、账户必须写进脚本的 `#SBATCH` 头才生效**（REST 提交时 `#SBATCH` 头同样被解析）。智能体生成脚本时这些行不可省略。
2. **已知模板隐患**：`config/templates/pytorch_single_gpu.json` 生成的脚本**没有 `--qos` 和 `--account` 行**。当前用户默认账户 competition 的 DefaultQOS 恰好是 `qos_p107-rtx5090` 所以能用；一旦默认账户变化或默认账户为 `stu`，提交 P107 分区会出问题。建议模板补上这两行（待修复项）。
3. **输出规范**：平台约定结果放 `runs/<作业名>-%j/`、日志按作业 ID 命名 `.out`/`.err`，与 `%x-%j` 符号体系一致。

## 7. 提交与验证命令

```bash
sbatch scripts/train.sbatch     # 提交，立即返回 job_id（资源不保证立即可用）
squeue -u "$USER"               # 看状态：PD 排队 / R 运行 / F 失败 / TO 超时 / CD 完成
tail -n 50 logs/train-*.out     # 看实时输出
scancel <job_id>                # 写错/卡死及时取消
```

智能体对应工具：`submit_job`（提交，需确认）、`list_jobs`/`get_job`（状态）、`read_job_log`（日志）、`get_job_priority`（排队原因）、`cancel_job`（取消，需确认）。

## 8. 常见报错速查

| 报错 | 原因 | 处理 |
|---|---|---|
| `QOSMaxWallDurationPerJobLimit` | `-t` 超过 QoS 时长上限 | 调小 `-t` 或申请更高 QoS |
| `QOSMaxCpuPerUserLimit` | CPU 核数超配额 | 调小 `--cpus-per-task` |
| `PartitionConfig` / `Invalid account` | 账户与分区不匹配 | P107 系列配 `-A competition`，Students 配 `-A stu` |
| `bad interpreter ...^M` | Windows CRLF 换行 | `dos2unix` 或编辑器改 LF |
| 日志文件不存在、作业直接 F | `logs/` 目录不存在（相对提交时目录） | 提交前 `mkdir -p logs` |
| 作业 `TO` | 运行时间到点被杀 | 加大 `-t`（不超过 QoS 上限）或加 checkpoint |
| 一直 `PD` | 资源不足/优先级低 | `get_job` 看 Reason，`get_job_priority` 看优先级 |

---

*本报告由 107 集群实测 + 双手册交叉验证生成；引用本报告时请同时参考随附的 `sbatch-manual-107-local.txt`（集群本机权威版）。*
