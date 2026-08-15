---
page_type: task
audience: beginner
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: core
  next:
    - basics/slurm.md
    - basics/faq.md
  related:
    - overview/resources.md
    - basics/environments.md
  advanced: []
icon: material/play-box-outline
---

# 提交任务

平台上的正式计算任务建议通过 Slurm 提交。你可以把 Slurm 理解为“共享服务器的排队系统”：你说明需要多少资源、运行什么命令，系统安排合适的节点执行。

## 交互式任务与批处理任务

交互式任务适合短时间调试：

- 检查 GPU 是否可用。
- 试运行一两条命令。
- 排查环境问题。

批处理任务适合正式运行：

- 训练、仿真、批量实验。
- 需要保留日志的任务。
- 浏览器关闭后仍应继续运行的任务。

如果只是临时检查 Python、GPU 或文件路径，可以先申请一个短时间交互式会话：

```bash
srun -p Students --qos=qos_stu_default -c 1 -t 00:10:00 --pty bash
```

需要 GPU 时再加上 GPU 资源：

```bash
srun -p Students --qos=qos_stu_default --gres=gpu:1 -c 1 -t 00:10:00 --pty bash
```

进入后运行 `hostname`、`python -V`、`nvidia-smi` 等命令检查环境，完成后用 `exit` 退出释放资源。更详细说明见[命令行使用](cli.md)和[Slurm 速查](slurm.md)。

## GUI 提交一次简单命令

也可以不通过 VS Code，直接用 GUI 提交一次简单命令：

1. 进入“作业 > 提交作业”。
2. 把工作目录设置为代码文件所在目录。
3. 在命令区域输入命令，例如：

```bash
python gaussian_elimination.py
```

4. 检查分区、QOS、CPU/GPU、运行时间、标准输出文件和错误输出文件。
5. 点击提交后，在作业列表中等待状态变为运行或完成。

这种方式适合简单脚本。正式训练或需要保留配置的任务，建议继续使用下面的 `sbatch` 脚本。

## 最小作业脚本

运行环境：平台 Shell，保存为 `scripts/hello.sbatch`。

```bash
#!/bin/bash
#SBATCH -J hello
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err
#SBATCH -t 00:05:00

set -euo pipefail

cd ~/projects/my-project
python src/hello.py
```

提交：

```bash
sbatch scripts/hello.sbatch
```

检查：

```bash
squeue -u "$USER"
tail -n 50 logs/hello-*.out
```

## GPU 作业脚本模板

以下模板使用常见默认分区和 QOS 写法。GPU 类型、CPU 数、内存和时间限制仍必须按平台当前说明填写。

运行环境：平台 Shell，保存为 `scripts/train.sbatch`。

```bash
#!/bin/bash
#SBATCH -J my-train
#SBATCH -p Students
#SBATCH --qos=qos_stu_default
#SBATCH --gres=gpu:<gpu_type_or_count>
#SBATCH --cpus-per-task=<cpu_count>
#SBATCH --mem=<memory>
#SBATCH -t <hh:mm:ss>
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err

set -euo pipefail

cd ~/projects/my-project

set +u
source ~/miniconda3/etc/profile.d/conda.sh
conda activate py310
set -u

nvidia-smi
python src/train.py
```

??? note "脚本字段解释"
    `-J` 是作业名；`-o` 和 `-e` 是标准输出和错误日志；`-t` 是最长运行时间；`--gres` 用于申请 GPU；`--cpus-per-task` 和 `--mem` 申请 CPU 和内存。具体上限由平台策略决定。

!!! tip "默认资源限制"
    统一身份认证登录的默认配额通常为 `4CPU/1GPU/4h`。第一次提交建议不要超过这个范围；需要更长时间或更多 GPU 时，先通过平台申请额外算力。

## 提交前检查

- `logs/` 目录已存在。
- `cd` 路径是项目真实路径。
- 环境名称正确，能手动激活。
- 先用小数据或少量迭代跑过 smoke test。
- 资源参数没有超过平台或课程允许范围。
- 如果使用默认 QOS，运行时间不超过 4 小时，GPU 不超过 1 张，CPU 不超过 4 核。

## 取消任务

运行环境：平台 Shell。

```bash
scancel <job_id>
```

取消任务会停止正在排队或运行的作业。发现脚本写错、任务卡死或资源占用异常时，应及时取消。
