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
    - ../quickstart.md
    - ../basics/jobs.md
  related:
    - ../basics/files.md
    - ../basics/environments.md
  advanced: []
icon: material/school-outline
---

# 其他专业与通用场景

如果你的任务不属于深度学习、数学统计或物理化学，也可以按“脚本化计算任务”来使用平台。

## 适合的任务

- 批量处理文本、图像、音频或表格。
- 长时间运行的 Python、C/C++、R 或 Julia 程序。
- 需要比个人电脑更多 CPU、内存或运行时间的课程实验。
- 需要统一环境和日志记录的竞赛实验。

## 最小流程

1. 把任务缩小成一个能在几分钟内完成的小样例。
2. 上传代码和小样例数据。
3. 在平台 Shell 中直接运行一次，确认依赖和路径。
4. 写成 Slurm 脚本提交。
5. 检查日志和输出。
6. 再扩大到完整数据。

## 通用提交模板

运行环境：平台 Shell，保存为 `scripts/run.sbatch`。

```bash
#!/bin/bash
#SBATCH -J batch-task
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err
#SBATCH -t 01:00:00

set -euo pipefail

cd ~/projects/my-task
python src/main.py --input data/sample --output outputs/sample
```

## 新增场景页所需信息

如果你希望后续新增某个专业或课程的场景页，建议先整理：

- 任务目标。
- 输入文件类型。
- 使用的软件或语言。
- 最小可运行示例。
- 常见报错和结果检查方式。
