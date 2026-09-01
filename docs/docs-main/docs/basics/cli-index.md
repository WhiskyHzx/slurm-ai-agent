---
page_type: reference
audience: beginner
status: draft
last_verified: 2026-05-21
maintainers:
  - name: docs-team
    contact: TODO
graph:
  priority: core
  next:
    - cli.md
    - slurm.md
  related:
    - jobs.md
    - faq.md
  advanced: []
icon: material/format-list-bulleted-square
---

# CLI 命令索引

本页按用途整理常用命令。第一次使用不需要全部记住，可以先复制最小示例，遇到问题时再回到这里查含义。

## 先确认在哪里

| 命令 | 用途 | 备注 |
| --- | --- | --- |
| `hostname` | 查看当前节点名 | 登录节点和计算节点不同。示例中登录节点可能显示为 `tradmin-02`，计算节点可能显示为 `anode08` 等。 |
| `pwd` | 查看当前目录 | 平台用户目录通常形如 `/home/scc/<账号>`。写文档和求助时应遮挡个人账号。 |
| `whoami` | 查看当前用户名 | 不建议把完整用户名直接贴到公开文档或截图中。 |
| `date` | 查看当前系统时间 | 排查日志时间、作业提交时间时有用。 |

## 文件和目录

| 命令 | 用途 | 备注 |
| --- | --- | --- |
| `ls -lh` | 查看当前目录文件 | `-l` 显示详细信息，`-h` 用易读单位显示大小。 |
| `mkdir -p logs outputs` | 创建目录 | `-p` 表示目录已存在时不报错。 |
| `cd ~/projects/my-project` | 进入项目目录 | 作业脚本中建议显式 `cd` 到项目目录。 |
| `cp a.txt b.txt` | 复制文件 | 大文件复制前先确认空间是否足够。 |
| `mv old.txt new.txt` | 移动或重命名 | 也可用于把结果移入 `outputs/`。 |
| `rm file.txt` | 删除文件 | 删除不可恢复，初学者慎用；批量删除前先 `ls` 确认。 |
| `tar -czf project.tar.gz project/` | 打包压缩目录 | 适合本地打包后上传。 |
| `tar -xzf project.tar.gz` | 解压压缩包 | 解压前先确认当前目录。 |
| `sha256sum file.tar.gz` | 生成校验值 | 上传大文件后可对比本地和平台文件是否一致。 |

## 查看日志和文本

| 命令 | 用途 | 备注 |
| --- | --- | --- |
| `cat file.txt` | 输出整个文件 | 只适合小文件。 |
| `less file.txt` | 分页查看文件 | 按 `q` 退出。 |
| `tail -n 50 logs/job.out` | 查看日志末尾 50 行 | 适合快速看任务是否报错。 |
| `tail -f logs/job.out` | 持续追踪日志 | 按 `Ctrl+C` 停止追踪。 |
| `sed -n '1,80p' train.sbatch` | 查看前 80 行 | 不会修改文件，适合检查脚本头部。 |
| `grep -n "Error" logs/job.err` | 搜索错误关键词 | `-n` 会显示行号。 |

## 编辑文件

| 命令 | 用途 | 备注 |
| --- | --- | --- |
| `nano train.sbatch` | 用 nano 编辑脚本 | 更适合初学者。底部会显示保存和退出提示。 |
| `vim train.sbatch` | 用 vim 编辑脚本 | 有模式概念，不熟悉时不要作为首选。 |
| `python -m py_compile src/train.py` | 检查 Python 语法 | 不运行训练，只检查语法。 |

## Python 和环境

| 命令 | 用途 | 备注 |
| --- | --- | --- |
| `python -V` | 查看 Python 版本 | 当前登录环境示例输出为 Python 3.12.3，项目环境可能不同。 |
| `which python` | 查看当前 Python 路径 | 用于确认是否进入了 conda 环境。 |
| `conda env list` | 查看 conda 环境 | 如果没有 conda，需要先安装或初始化。 |
| `conda activate py310` | 激活环境 | `py310` 换成自己的环境名。 |
| `pip list` | 查看已安装 Python 包 | 输出较长时可配合 `grep` 搜索。 |
| `pip freeze > requirements.lock.txt` | 保存当前包版本 | 适合记录可复现环境快照。 |

## Slurm 作业

| 命令 | 用途 | 备注 |
| --- | --- | --- |
| `sbatch scripts/train.sbatch` | 提交批处理作业 | 提交后记录作业 ID。 |
| `squeue -u "$USER"` | 查看自己的作业 | 没有输出通常表示当前没有排队或运行作业。 |
| `scontrol show job <job_id>` | 查看作业详情 | 排查等待原因、资源申请和运行节点。 |
| `scancel <job_id>` | 取消作业 | 脚本写错或资源申请错误时及时取消。 |
| `scontrol show part` | 查看分区配置 | 输出很长，重点看 `PartitionName`、`AllowQos`、`Nodes`、`State`、`TRES`。 |
| `sinfo` | 查看分区和节点状态 | `idle` 表示空闲，`mix` 表示部分占用，`comp` 表示资源较满，`down` 表示不可用。 |

## 短期进入计算节点

CPU 调试：

```bash
srun -p Students --qos=qos_stu_default -c 1 -t 00:10:00 --pty bash
hostname
exit
```

GPU 调试：

```bash
srun -p Students --qos=qos_stu_default --gres=gpu:1 -c 1 -t 00:10:00 --pty bash
nvidia-smi
exit
```

说明：

- `srun --pty bash` 会申请一个交互式 Shell。
- `-t 00:10:00` 表示最长 10 分钟。
- `exit` 会退出并释放资源。
- 如果出现排队，说明资源没有立即分配。
- 如果 `nvidia-smi` 报 `Driver/library version mismatch`，记录节点名、作业 ID 和完整报错后反馈平台维护者。

## 当前平台信息采集

以下命令适合维护文档时采集平台状态，不建议读者每次都运行：

```bash
hostname
pwd
python -V
squeue -u "$USER"
scontrol show part
sinfo
```

2026-05-21 的一次采集显示：登录 Shell 位于 `tradmin-02`，用户目录形如 `/home/scc/<账号>`，登录环境 Python 为 `3.12.3`；`Students` 分区覆盖 `anode[05-17]`，可用 QOS 包括 `qos_stu_default`、`qos_stu_small`、`qos_stu_medium`、`qos_stu_medium_2gpu`、`qos_stu_long`、`qos_stu_cpu_long` 等。以上信息具有即时性，提交作业前仍以当前命令输出和平台页面为准。
