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
    - ../../basics/faq.md
  advanced: []
icon: material/atom
---

# 物理与化学用例

物理、化学和材料相关任务常见特点是输入文件多、运行时间长、输出文件需要后处理。平台适合承接可脚本化、可复现的计算流程。

## 适合的任务

- 物理仿真和数值求解。
- 化学、材料或结构相关计算。
- 实验数据批量处理。
- 多组参数或多组输入文件的重复运行。

## 使用建议

- 每组输入文件单独建目录，避免输出互相覆盖。
- 正式运行前先用小体系、小步数或短时间任务测试。
- 把软件版本、输入文件、提交脚本和输出日志一起保存。
- 如果使用专门软件，先确认平台是否预装，或是否允许用户自行安装。

## 示例目录

```text
simulation-project/
  inputs/
    case-001/
    case-002/
  scripts/
  logs/
  outputs/
  analysis/
```

## 最小提交模板

运行环境：平台 Shell，保存为 `scripts/run-case.sbatch`。

```bash
#!/bin/bash
#SBATCH -J sim-case
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err
#SBATCH -t 02:00:00

set -euo pipefail

cd ~/projects/simulation-project

CASE=case-001
mkdir -p "outputs/${CASE}"

# TODO: 替换为实际软件命令
echo "run ${CASE}"
```

检查点：作业结束后，日志中有明确的开始和结束信息，输出目录中有本次 case 的结果。

## 后续扩展方向

- 平台预装科学计算软件清单。
- 常用输入输出文件说明。
- 多 case 批量提交模板。
- 后处理和可视化结果下载建议。
