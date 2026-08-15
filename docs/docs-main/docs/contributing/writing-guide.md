---
page_type: maintenance
audience: maintainer
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: peripheral
  next: []
  related:
    - ../index.md
    - ../basics/faq.md
  advanced: []
icon: material/pencil-outline
---

# 写作指南

本文面向后续维护者和协作 agent。正式文档的目标是让第一次使用平台的本科生能完成任务，同时为愿意深入学习的同学提供进阶入口。

## 页面类型

每个页面建议在 front matter 中声明：

```yaml
---
page_type: overview
audience: beginner
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: core
  next: []
  related: []
  advanced: []
---
```

常用类型：

- `overview`：总览。
- `quickstart`：快速开始。
- `task`：完成单个任务。
- `concept`：解释概念和使用决策。
- `reference`：命令、参数、FAQ。
- `guide`：真实场景工作流。
- `maintenance`：维护和贡献说明。

## 写作原则

- 先写平台上的实际做法，再补必要背景。
- 主线页面服务初学者，不写成长篇百科。
- 命令必须说明运行环境：本地电脑、平台 Shell、登录节点、计算节点或作业脚本。
- 关键步骤要有检查点，告诉读者怎样确认做对了。
- 未核验的平台事实不要写成结论。
- 成熟外部教程可以链接，但平台关键路径必须在本站闭环。

## 命令规范

命令块前说明运行环境，命令块后补简短解释或折叠说明。会提交作业、改文件、删除文件或申请资源的命令，要说明影响。

示例：

```bash
sbatch scripts/train.sbatch
```

??? note "命令解释"
    `sbatch` 会把作业提交给 Slurm。提交后应记录作业 ID，并用 `squeue -u "$USER"` 查看状态。

## 截图规范

截图只在能显著降低理解成本时使用。要求：

- 遮挡用户名、学号、路径、任务 ID、token、邮箱等个人信息。
- 聚焦操作区域，不截整屏无关内容。
- 放在对应页面目录的 `assets/` 下。
- 文件名表达用途，例如 `gui-file-upload-entry.png`。
- 文字不能只写“如下图”，必须说明关键按钮或字段。
- 不伪造真实平台截图。

## 待采集材料清单

后续补图时，优先采集能帮助初学者定位入口、字段和状态的截图。每张图都要先遮挡个人信息。

建议优先补充：

- 登录页：只展示平台入口和登录方式，不暴露个人账号。
- 创建 VS Code/Appainer 应用：CPU/GPU、分区、QOS、运行时间、额外 sbatch 参数字段。
- 登录集群 Shell：空白 Shell、右上角“命令”帮助入口、右键复制/粘贴菜单。
- 交互式进入计算节点：`srun --pty bash` 排队、进入节点、`exit` 退出的关键状态。
- 作业列表：排队、运行、完成、失败状态各一张，遮挡作业 ID 以外的个人信息。
- 作业详情：标准输出、错误输出、资源申请字段和日志入口。
- 文件管理：上传、解压、预览 `.out/.err`、在 Shell 中打开当前位置。
- 资源申请页面：申请入口、需要填写的任务说明和资源字段。

建议采集的命令输出：

```bash
hostname
pwd
python -V
squeue -u "$USER"
scontrol show part
sinfo
```

在短期交互式计算节点中可额外采集：

```bash
srun -p Students --qos=qos_stu_default -c 1 -t 00:10:00 --pty bash
hostname
exit
```

在 GPU 资源中可额外采集：

```bash
srun -p Students --qos=qos_stu_default --gres=gpu:1 -c 1 -t 00:10:00 --pty bash
nvidia-smi
python - <<'PY'
import torch
print(torch.__version__)
print(torch.cuda.is_available())
if torch.cuda.is_available():
    print(torch.cuda.get_device_name(0))
PY
exit
```

采集命令输出时，只保留文档需要的关键行。提交前应遮挡用户名、节点名中可能关联个人任务的信息、完整个人路径和课程未公开信息。GPU 型号、分区、QOS、配额、节点名都属于动态事实，写入正式文档时要标注“以当前平台为准”。

## FAQ 规范

FAQ 条目建议包含：

- 问题现象。
- 状态：已验证、高频、待验证或可能过期。
- 可能原因。
- 推荐排查顺序。
- 需要求助时应提供的信息。

## 动态事实

以下内容写入正式文档前必须核验：

- 平台入口地址。
- 资源申请入口与流程。
- 默认 CPU、GPU、内存、时间配额。
- GPU 型号、节点资源、partition、QOS。
- SSH 和文件传输策略。
- 软件环境版本和预装工具。
