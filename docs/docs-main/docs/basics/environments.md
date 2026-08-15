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
    - basics/jobs.md
    - guides/ai/deep-learning-homework.md
  related:
    - basics/files.md
    - basics/faq.md
  advanced: []
icon: material/package-variant-closed
---

# 环境配置

环境指程序运行所需的软件、解释器、库和版本。对 Python 项目来说，最常见的是为每个项目创建独立 conda/mamba 环境。下面以在 VS Code 终端安装 Miniconda，并创建 Python 3.10 环境为例。

## 必须知道

- 不同项目尽量使用不同环境，避免依赖互相污染。
- 环境建好后，必须在作业脚本里显式激活。
- GPU 程序除了 Python 包，还可能受 CUDA、驱动和 PyTorch 版本影响。
- 不要只记录“我装过”，要保留安装命令或环境文件。

!!! note "是否有统一环境"
    当前平台通常需要用户自行配置 conda 环境。建议使用 Miniconda/Miniforge，并为每个项目创建独立环境。

## 安装 Miniconda

如果平台没有现成的 conda/mamba 环境，可以在用户主目录安装 Miniconda。

运行环境：VS Code 终端或平台 Shell。

```bash
cd ~
wget https://mirrors.ustc.edu.cn/anaconda/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p "$HOME/miniconda3"
~/miniconda3/bin/conda init bash
source ~/.bashrc
```

如果 conda 提示需要接受 Anaconda 服务条款，可以执行：

```bash
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r
```

## 配置软件源

运行环境：已初始化 conda 的平台 Shell。

```bash
cat > ~/.condarc <<'EOF'
channels:
  - https://mirrors.ustc.edu.cn/anaconda/cloud/conda-forge
  - https://mirrors.ustc.edu.cn/anaconda/pkgs/main
  - https://mirrors.ustc.edu.cn/anaconda/pkgs/r
show_channel_urls: true
EOF

pip config set global.index-url https://mirrors.ustc.edu.cn/pypi/web/simple
```

??? note "命令解释"
    这会让 conda 和 pip 优先使用中科大镜像源，通常能改善下载速度和稳定性。已有个人配置的同学，修改前应先确认是否会覆盖自己的 `.condarc`。

## 创建 Python 环境

运行环境：平台 Shell。

```bash
conda create -n py310 python=3.10 -y
conda activate py310
python -V
```

这里使用 `py310` 作为环境名。你可以换成项目名称，例如 `dl-homework`；后续激活环境时要使用同一个名字。

检查点：`python -V` 输出符合项目要求，`which python` 指向你的环境目录。

## 安装依赖

运行环境：已激活项目环境的平台 Shell。

```bash
pip install -r requirements.txt
```

如果项目没有 `requirements.txt`，可以先记录手动安装的包：

```bash
pip freeze > requirements.lock.txt
```

!!! note "建议"
    `requirements.lock.txt` 适合记录当前能跑通的环境快照，但不一定适合跨平台直接复用。长期项目建议维护更干净的 `requirements.txt` 或 `environment.yml`。

不要直接把所有项目都装进 `base` 环境。不同实验应创建相互隔离的环境，便于后续管理和排错。

## 在作业脚本中激活环境

运行环境：作业脚本。

```bash
set +u
source ~/miniconda3/etc/profile.d/conda.sh
conda activate py310
set -u

python src/train.py
```

??? note "为什么这样写"
    一些 conda 激活脚本会读取未定义变量。作业脚本使用 `set -u` 时，临时 `set +u` 可以避免激活过程失败。

如果作业日志显示找不到 `conda`，通常不是因为需要重新安装环境，而是作业脚本没有加载 conda 初始化脚本。请确认 `source` 后面的路径与实际安装位置一致。

## 安装 GPU 版 PyTorch

登录节点通常没有 GPU。如果在没有 GPU 的登录节点上安装，conda 可能默认解析出 CPU 版 PyTorch。

如果确认需要安装 GPU 版 PyTorch，可以在干净环境中强制指定 CUDA 版本。例如：

```bash
CONDA_OVERRIDE_CUDA="12.4" conda install pytorch torchvision torchaudio pytorch-cuda=12.4 -c pytorch -c nvidia -y
```

这里的 `12.4` 只是示例。CUDA 版本应按当前平台驱动、PyTorch 官方安装建议和课程要求选择。安装后请在已申请 GPU 的作业中运行 GPU 检查，而不是只在登录节点判断。

## GPU 环境检查

运行环境：已申请 GPU 的计算环境或 GPU 作业脚本。

```bash
nvidia-smi
python - <<'PY'
import torch
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("gpu:", torch.cuda.get_device_name(0))
PY
```

检查点：`nvidia-smi` 能看到 GPU，Python 输出 `cuda available: True`。如果在登录节点执行，可能看不到 GPU，这是正常的，关键是要在已申请 GPU 的环境中检查。

## Jupyter Notebook

VS Code 可以使用 Jupyter Notebook。使用前通常需要：

- 在 VS Code 插件中安装 Jupyter 插件。
- 在当前 conda 环境中安装 `jupyter` 或 `ipykernel`。
- 打开 `.ipynb` 后选择已创建的 Python 环境作为 kernel。

运行环境：已激活项目环境的平台 Shell。

```bash
pip install ipykernel
python -m ipykernel install --user --name py310 --display-name "Python py310"
```

Notebook 适合交互式探索和展示，不适合长时间占用交互式资源。正式训练和批量实验仍建议提交 Slurm 作业。
