# SSH 服务器运行新手使用说明

这份说明面向“项目运行在 107 SSH 服务器上”的方式。用户先通过 SSH 登录服务器，在服务器上启动 `slurm-ai-agent`，然后用浏览器访问服务地址。

## 你需要先准备什么

需要三件事：

1. 能通过 SSH 登录 `107.ustc.edu.cn`。
2. 服务器上的项目目录里有 `.env`。
3. 服务器上的 Python 环境已经安装项目依赖。

SSH 还没配好的同学先看：

```text
evaluation/ssh-setup-guide.md
```

## .env 应该有什么

在服务器上的项目根目录：

```bash
cd ~/slurm-ai-agent
```

`.env` 至少需要：

```env
LLM_API_KEY=你的学校大模型APIKey
LLM_MODEL=deepseek-chat
```

Slurm JWT 不需要提前写死在 `.env`。项目跑在登录节点上时，`core/slurm_client.py` 可以在遇到认证失败时直接调用：

```bash
scontrol token lifespan=86400
```

并把新的 token 写入当前进程环境变量。

可选配置：

```env
SLURM_REMOTE_PROJECTS_BASE=~/projects
SLURM_UPLOAD_MAX_BYTES=2147483648
```

## 第一次安装依赖

只需要做一次：

```bash
cd ~/slurm-ai-agent
source ~/miniconda3/etc/profile.d/conda.sh
conda activate base
python -m pip install -r requirements.txt
```

如果依赖已经在 Miniconda 里安装过，可以跳过这一步。

## 启动应用

在 SSH 服务器上运行：

```bash
cd ~/slurm-ai-agent
source ~/miniconda3/etc/profile.d/conda.sh
conda activate base
PYTHONPATH=. python -m uvicorn server.app:app --host 0.0.0.0 --port 8080
```

如果只想让 SSH 隧道访问，可以把 `--host` 改成：

```bash
--host 127.0.0.1
```

## 浏览器访问

如果服务监听 `127.0.0.1:8080`，在自己电脑上开 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 107.ustc.edu.cn
```

然后浏览器打开：

```text
http://127.0.0.1:8080
```

如果服务器安全策略允许直接访问对应端口，也可以使用服务器提供的访问地址。

## 上传本地文件

页面右上角点击 `上传`，在弹窗中填写作业目录名称，再选择上传入口：

```text
作业目录名称
上传文件
上传文件夹
```

选择后，应用会自动做：

1. 接收你在浏览器里选中的文件。
2. 在服务器临时目录打包成 `.tar.gz`。
3. 计算压缩包 SHA256。
4. 在服务器上创建 `~/projects/作业目录名称`。
5. 保存压缩包到 `~/projects/作业目录名称/.slurm-agent/uploads/`。
6. 校验保存后的压缩包 SHA256。
7. 解压到 `~/projects/作业目录名称`。

上传成功后页面会显示项目目录，例如：

```text
/home/scc/你的用户名/projects/my-training
```

后续提交作业时，可以让智能助手使用这个目录作为代码或数据所在位置。

## 小幅改动怎么同步

当前页面上传适合第一次提交或偶尔整体更新。频繁改代码时，更合适的后续功能是“同步并重启作业”：

```bash
rsync -az --delete --exclude .git --exclude __pycache__ 本地项目/ 107.ustc.edu.cn:~/projects/作业目录名称/
```

然后按项目记录上一次提交的 Slurm job id。点击重启时自动执行：

```bash
scancel 上一次jobid
cd ~/projects/作业目录名称 && sbatch 作业脚本.sh
```

更稳妥的版本应该加两个保护：

1. 如果作业已经跑很久，先提醒用户确认是否取消。
2. 训练任务尽量保存 checkpoint，重启后从 checkpoint 恢复，避免浪费 GPU 时间。

## 常见问题

### 1. Slurm API 认证失败

确认服务确实运行在能执行 `scontrol` 的登录节点上：

```bash
scontrol token lifespan=86400
```

如果这个命令失败，应用也无法自动刷新 Slurm JWT。

### 2. LLM API 失败

检查 `.env`：

```env
LLM_API_KEY=你的学校大模型APIKey
LLM_MODEL=deepseek-chat
```

如果某个模型不可用，换成平台当前支持的模型。

### 3. 文件上传失败

检查服务器上的项目根目录是否可写：

```bash
mkdir -p ~/projects/test-write
```

如果这里没有权限，修改 `.env` 里的：

```env
SLURM_REMOTE_PROJECTS_BASE=~/projects
```
