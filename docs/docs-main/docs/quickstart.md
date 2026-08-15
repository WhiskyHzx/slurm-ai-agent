---
page_type: quickstart
audience: beginner
status: draft
last_verified:
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: core
  next:
    - basics/gui.md
    - basics/files.md
    - basics/jobs.md
  related:
    - overview/index.md
    - basics/faq.md
  advanced:
    - basics/slurm.md
icon: material/rocket-launch-outline
---

# 快速开始

本页目标是帮你完成一次“能跑起来”的最小闭环。先不追求最佳性能，先确认账号、文件、环境、任务提交和日志查看都可用。

!!! warning "动态信息"
    平台正式入口为 <https://107.ustc.edu.cn/>。默认配额、队列名称、分区和 SSH 状态可能变化；本文使用占位符或通用写法时，请以平台页面、课程通知或管理员说明为准。

## 1. 进入平台

打开平台入口，使用学校账号或课程通知指定的账号方式登录。如果不确定密码或登录方式，请按课程或平台通知联系助教。

登录后先确认你能看到以下区域：

- 文件管理或工作目录入口。
- Shell、终端或登录集群入口。
- 作业、任务或 Slurm 相关入口。
- 资源申请或配额说明入口。
- 应用入口，例如 VS Code/Appainer。

检查点：你能打开一个 Shell，并看到类似 Linux 终端的命令提示符。

![平台登录页示例](basics/assets/login-page.png)

!!! note "截图说明"
    登录页提供账号密码登录和统一认证登录两个入口。验证码会随页面刷新变化，按当前页面显示输入即可。

![平台仪表盘入口示例](basics/assets/gui-dashboard-overview.png)

!!! note "截图说明"
    仪表盘顶部通常能看到“作业”“登录集群”“应用”“文件管理”等入口。第一次使用时，先确认这些入口都能打开；具体资源统计只代表当前账号和当前时刻。

## 2. 选择运行入口

第一次使用有两条常见入口：

- 在“应用”中创建 VS Code/Appainer 应用，适合写代码、编辑文件、用终端配置环境。
- 在“作业 > 提交作业”中直接提交命令，适合已经准备好脚本后运行一次任务。
- 在“登录集群”中打开 Web Shell，适合创建目录、检查文件、提交 Slurm 作业和做短时间调试。

如果没有 GPU 需求，创建应用时优先选择 CPU。需要 GPU 时，应在界面中选择可用分区和 GPU 相关配置；统一身份认证登录的默认分区/QOS 通常为 `Students` / `qos_stu_default`，具体仍以界面可选项为准。

![登录集群 Shell 示例](basics/assets/cli-shell-overview.png)

!!! note "截图说明"
    这张图展示了 Web Shell 的基本样子。图中的 `srun`、`nvidia-smi`、`pip list` 等命令属于调试或排查参考，第一次使用只需要先跟随本文完成最小闭环。

## 3. 建一个工作目录

运行环境：平台 Shell。

```bash
mkdir -p ~/projects/hello-cp4u/{src,logs,outputs}
cd ~/projects/hello-cp4u
pwd
```

??? note "命令解释"
    `mkdir -p` 会创建项目目录和子目录；`pwd` 显示当前目录。后续脚本里的路径建议从这里开始组织，不要把代码、日志和结果混在一起。

检查点：`pwd` 输出的路径在你的用户目录下，`ls` 能看到 `src`、`logs`、`outputs` 三个目录。

## 4. 准备一个最小程序

运行环境：平台 Shell。

```bash
cat > src/hello.py <<'PY'
import os
import sys

print("hello from computing platform")
print("python:", sys.version.split()[0])
print("cwd:", os.getcwd())
PY
python src/hello.py
```

检查点：终端输出 `hello from computing platform`，并显示 Python 版本和当前目录。

## 5. 可选：短期进入计算节点检查

如果你只是想临时进入计算节点测试一两条命令，可以申请一个短时间交互式会话。这个步骤适合检查 GPU、Python 环境或文件路径；正式训练仍建议使用下一节的批处理作业。

运行环境：平台 Shell。

```bash
srun -p Students --qos=qos_stu_default -c 1 -t 00:10:00 --pty bash
```

进入后可以检查自己是否已经在分配到的节点上：

```bash
hostname
python src/hello.py
exit
```

如果需要临时申请 1 张 GPU，可以参考：

```bash
srun -p Students --qos=qos_stu_default --gres=gpu:1 -c 1 -t 00:10:00 --pty bash
nvidia-smi
exit
```

??? note "命令解释"
    `srun --pty bash` 会向 Slurm 申请一个短时间交互式 Shell。`-t 00:10:00` 表示最多使用 10 分钟；`exit` 会退出并释放资源。分区、QOS、GPU 数和时长都要以平台当前允许范围为准。如果命令进入排队状态，说明资源暂时没有立即分配。

更多说明见[命令行使用](basics/cli.md)和[Slurm 速查](basics/slurm.md)。

## 6. 提交第一个批处理任务

运行环境：平台 Shell。

```bash
cat > hello.sbatch <<'SH'
#!/bin/bash
#SBATCH -J hello-cp4u
#SBATCH -o logs/%x-%j.out
#SBATCH -e logs/%x-%j.err
#SBATCH -t 00:05:00

set -euo pipefail

cd ~/projects/hello-cp4u
python src/hello.py
SH

sbatch hello.sbatch
```

??? note "命令解释"
    `sbatch` 会把脚本提交给 Slurm 排队运行。`%x` 会替换为作业名，`%j` 会替换为作业 ID。这个例子不申请 GPU，适合先验证提交流程。

检查点：提交后终端会显示一个作业 ID。可以继续用下面的命令查看状态：

```bash
squeue -u "$USER"
```

## 7. 查看日志和结果

运行环境：平台 Shell。

```bash
ls -lh logs
tail -n 50 logs/hello-cp4u-*.out
```

检查点：`logs` 目录下出现 `.out` 文件，文件中能看到最小程序的输出。如果出现 `.err` 且内容不为空，先读错误信息，再查[常见问题](basics/faq.md)。

![GUI 查看输出文件示例](basics/assets/pdf-gui-job-output-preview.png)

!!! note "截图说明"
    如果不熟悉 `tail`，也可以在文件管理中打开 `.out` 或 `.err` 文件预览输出。排查问题时，优先保存作业 ID 和日志关键内容。

## 下一步

- 想临时进入计算节点调试：读[命令行使用](basics/cli.md)。
- 想用图形界面上传下载文件：读[GUI 使用](basics/gui.md)和[文件与数据](basics/files.md)。
- 想运行 Python、PyTorch 或其他依赖：读[环境配置](basics/environments.md)。
- 想跑更长任务或 GPU 任务：读[提交任务](basics/jobs.md)和[Slurm 速查](basics/slurm.md)。
