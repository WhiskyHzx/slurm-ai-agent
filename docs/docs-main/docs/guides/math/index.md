---
page_type: guide
audience: beginner
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: normal
  next:
    - ../../basics/files.md
    - ../../basics/jobs.md
  related:
    - ../../basics/environments.md
    - ../../basics/slurm.md
  advanced: []
icon: material/function-variant
---

# 数学与统计用例

数学与统计任务通常不一定需要 GPU，但很适合使用平台进行长时间计算、重复模拟、参数扫描和批量实验。

## 适合的任务

- Monte Carlo 模拟。
- 参数扫描和网格搜索。
- 数值优化、微分方程求解。
- 大量独立实验的批量运行。
- Python、R、Julia 脚本的长时间运行。

## 推荐工作流

1. 在本地或平台 Shell 中准备一个最小输入。
2. 用小规模数据运行脚本，确认输出格式正确。
3. 把参数写入配置文件或命令行参数，不要手动改代码跑多组实验。
4. 用 Slurm 脚本提交正式任务。
5. 把每次实验的参数、日志和结果放到独立目录。

## 示例目录

```text
stat-sim/
  src/
    simulate.py
  configs/
    small.yaml
    full.yaml
  logs/
  outputs/
```

## 最小作业脚本

运行环境：平台 Shell，保存为 `scripts/sim.sbatch`。

```bash
#!/bin/bash
#SBATCH -J stat-sim
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err
#SBATCH -t 01:00:00

set -euo pipefail

cd ~/projects/stat-sim
python src/simulate.py --config configs/small.yaml --out outputs/small
```

检查点：日志中能看到程序开始和结束信息，`outputs/small` 中有结果文件。

## 后续扩展方向

- R 和 Julia 环境配置示例。
- Slurm array job 参数扫描模板。
- 随机种子、结果复现和表格汇总规范。
