---
page_type: overview
audience: beginner
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: normal
  next:
    - ai/deep-learning-homework.md
    - math/index.md
    - phy-che/index.md
    - others.md
  related:
    - ../quickstart.md
    - ../basics/jobs.md
  advanced: []
icon: material/map-search-outline
---

# 分专业与需求指引

这一部分按真实任务组织工作流，而不是重复所有基础教程。每个场景指南都应该回答：这个任务适合用平台做什么、需要准备哪些文件和环境、怎样提交任务、怎样检查结果、失败时先看哪里。

## 使用方式

- 第一次用平台：先读[快速开始](../quickstart.md)。
- 已经知道要跑什么任务：选择最接近的场景指南。
- 场景指南里没有覆盖的命令和概念：回到[基础使用](../basics/gui.md)相关页面。

## 当前场景

- [深度学习作业训练任务](ai/deep-learning-homework.md)：适合 PyTorch、GPU 训练、课程实验迁移。
- [数学与统计用例](math/index.md)：适合数值计算、模拟、参数扫描。
- [物理与化学用例](phy-che/index.md)：适合仿真、材料/化学计算、实验数据处理。
- [其他专业与通用场景](others.md)：适合通用 Python 脚本、批量处理和临时计算需求。

## 写作约定

后续新增场景页时，建议按这个顺序组织：

1. 任务目标和适用场景。
2. 输入文件、代码和环境准备。
3. 最小测试任务。
4. 正式作业脚本。
5. 日志、结果和性能检查。
6. 常见失败原因。
7. 后续优化方向。
