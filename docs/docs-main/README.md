# `107.ustc.edu.cn` Help File Project

本仓库用于编写、管理中国科学技术大学本科生算力平台 [107.ustc.edu.cn](https://107.ustc.edu.cn/) 的使用文档。

项目当前处于非常早期，欢迎对项目感兴趣的大家参与完善。具体的贡献方式见 [参与贡献](#参与贡献) 小节。

## 目标

使用文档旨在介绍 [107.ustc.edu.cn](https://107.ustc.edu.cn/) 的相关基础信息，同时帮助对相关操作不了解的同学能够快速上手使用本平台提交、运行、完成科学计算任务。



## 结构

### 概览

下方展示了本项目的组织结构

```
docs\
│   index.md
│   quickstart.md
│
├───assets
│
├───basics
│   │   cli.md
│   │   environments.md
│   │   faq.md
│   │   files.md
│   │   gui.md
│   │   jobs.md
│   │   slurm.md
│   │
│   └───assets
│
├───contributing
│       writing-guide.md
│
├───guides
│   │   index.md
│   │   others.md
│   │
│   ├───ai
│   │   │   deep-learning-homework.md
│   │   │
│   │   └───assets
│   │           cp4u-term.png
│   │           cp4u-work.png
│   │
│   ├───math
│   │   │   index.md
│   │   │
│   │   └───assets
│   │
│   └───phy-che
│       │   index.md
│       │
│       └───assets
│
└───overview
    │   index.md
    │   resources.md
    │
    └───assets
```


## 参与贡献

### 本地预览

本项目使用 MkDocs + Material for MkDocs 构建静态文档站点。

首次使用建议创建 Python 虚拟环境后，通过 `requirements.txt` 安装依赖：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

本地预览：

```bash
mkdocs serve
```

严格构建检查：

```bash
mkdocs build --strict
```

如果本机端口被占用，可以指定地址：

```bash
mkdocs serve --dev-addr 127.0.0.1:8001
```

如果使用 `uv` 管理本地虚拟环境：

```bash
uv venv
uv pip install -r requirements.txt
uv run mkdocs serve
uv run mkdocs build --strict
```

### 写作流程

1. 先确定要修改的页面属于哪一类：
   - `quickstart.md`：最小上手闭环。
   - `overview/`：平台概念、资源和整体说明。
   - `basics/`：GUI、命令行、环境、文件、作业、Slurm、FAQ。
   - `guides/`：按专业或真实任务组织的场景指南。
   - `contributing/`：维护者和协作者说明。
2. 新增页面后，同时更新 `mkdocs.yml` 中的 `nav`。
3. 涉及动态事实时，优先写“如何确认”和“以平台页面为准”，不要把未经核验的节点名、GPU 型号、QOS、配额写成永久结论。
4. 修改后运行 `mkdocs build --strict`，确认链接、图片和 Markdown 语法没有问题。

### 内容风格

- 面向初学者，优先写清“在哪里操作、输入什么、如何检查是否成功”。
- 保留可复制命令，但不要要求读者一次理解所有 Linux、Slurm 或 conda 细节。
- 对命令示例标注运行环境，例如“本地电脑”“平台 Shell”“作业脚本”“已申请 GPU 的计算环境”。
- FAQ 先给判断逻辑和处理步骤，再解释原因。
- 避免在正式文档中出现“聊天记录显示”“补充资料显示”“当前截图显示”这类整理过程描述。

### 图片与隐私

截图放在对应章节的 `assets/` 目录中，例如 `docs/basics/assets/`。

提交截图前请检查并遮挡：

- 姓名、学号、账号、邮箱。
- 个人目录路径、课程群信息、浏览器个人信息。
- token、密钥、下载链接中的临时凭证。
- 不适合公开的作业内容或数据。

截图说明应只解释读者需要看的区域，例如入口、字段、按钮和状态。截图中的 GPU 型号、分区、QOS、作业 ID、节点名通常只是示例，不应让读者照抄。

站点使用 `mkdocs-glightbox` 为正文图片提供点击放大浏览。普通 Markdown 图片无需额外标记：

```markdown
![平台登录页示例](assets/login-page.png)
```

如需关闭单张图片的放大效果，可使用 `attr_list` 写法添加 `off-glb` 类：

```markdown
![平台登录页示例](assets/login-page.png){ .off-glb }
```

### 协作资料

仓库中可能存在用于整理材料的目录，例如：

- `legacy/`：历史聊天记录、旧草稿或原始材料。
- `docs-agent/`：文档整理过程中的方案、事实记录和待核验问题。
- `tmp/`、`site/`、`output/playwright/`：构建、预览或测试产生的临时文件。

这些目录通常不作为正式文档发布内容。正式站点内容以 `docs/` 和 `mkdocs.yml` 为准。

### 提交前检查清单

- [ ] 页面能从 `mkdocs.yml` 导航访问，或确实只是被其他页面链接引用。
- [ ] 新增图片已放入合适的 `assets/` 目录，并完成脱敏。
- [ ] 命令示例标注了运行环境。
- [ ] 动态平台信息没有写成不可变事实。
- [ ] `mkdocs build --strict` 通过。
- [ ] 没有提交 `site/`、`tmp/`、本地虚拟环境或测试输出。
