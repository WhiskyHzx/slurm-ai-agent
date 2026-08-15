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
    - basics/environments.md
    - basics/jobs.md
  related:
    - basics/gui.md
    - basics/cli.md
  advanced: []
icon: material/folder-outline
---

# 文件与数据

文件组织会直接影响任务能否复现。建议从第一天起就把代码、数据、日志和输出分开放，避免几周后找不到“到底跑的是哪一版”。

## 推荐目录结构

运行环境：平台 Shell。

```bash
mkdir -p ~/projects/my-project/{src,data,logs,outputs,scripts}
```

一个常见项目可以这样组织：

```text
my-project/
  src/       # 代码
  data/      # 数据或数据链接
  scripts/   # sbatch 脚本、启动脚本
  logs/      # 标准输出和错误日志
  outputs/   # 模型、图表、结果表格
```

检查点：提交脚本中的 `cd`、日志路径和输出路径都指向这个项目目录，不依赖“我刚好在某个目录里”。

## 上传项目

小文件可以直接用 GUI 上传。目录较大时，推荐在本地先打包：

运行环境：本地电脑。

```bash
tar -czf my-project.tar.gz my-project/
```

上传后在平台 Shell 中解包：

```bash
tar -xzf my-project.tar.gz
```

??? note "命令解释"
    `tar -czf` 会打包并压缩目录；`tar -xzf` 会解包。大量小文件打包后传输通常更稳定。

## 检查上传是否完整

大压缩包上传后，应先检查远端文件大小是否与本地一致。上传重要文件后，建议先检查完整性，再解压或提交作业。

运行环境：本地电脑和平台 Shell 分别运行。

```bash
ls -lh my-project.tar.gz
sha256sum my-project.tar.gz
```

检查点：本地和平台上的文件大小应一致，`sha256sum` 输出也应一致。如果不一致，说明上传不完整，应重新上传、分卷压缩，或改用平台推荐的传输方式。

## 外部下载失败时

平台上连接 GitHub、Hugging Face 等外部站点可能不稳定。建议：

- 优先使用中科大镜像源、课程提供的下载地址或国内镜像。
- 代码仓库可以先在本地克隆，再打包上传。
- 大数据集和模型权重不要只依赖作业运行时在线下载，尽量提前准备并记录来源。

## 下载结果

正式任务结束后，建议先整理输出：

运行环境：平台 Shell。

```bash
cd ~/projects/my-project
tar -czf outputs-$(date +%Y%m%d).tar.gz outputs logs
```

然后通过 GUI 或平台支持的文件传输方式下载压缩包。

## 不建议的做法

- 把代码、数据、日志和结果都放在主目录根部。
- 覆盖旧结果但不记录参数。
- 在作业脚本里写只对当前终端有效的相对路径。
- 长期堆放无关大文件，占用共享存储。

## 待确认

共享存储容量、性能限制、清理策略和推荐传输方式仍需平台维护者确认。本文先给通用项目组织建议。
