---
page_type: how-to
audience: beginner
status: stable
maintainers:
  - name: docs-team
graph:
  next:
    - reference/self-hosted.md
icon: material/key-chain
---

# SSH 连接配置

本页面说明如何配置本机到集群的 SSH 免密连接，以及为本地/远端运行的应用提供访问集群 API 的代理通道。

## 目标

配置完成后，本机可以直接运行：

```bash
ssh 107.ustc.edu.cn "hostname"
```

并成功返回登录节点主机名。该能力同时供控制台的认证刷新功能使用。

## 第 1 步：生成 SSH 密钥

在本地终端运行：

```bash
ssh-keygen -t ed25519 -C "your_email@example.com"
```

按提示完成，将生成默认密钥对：

```text
~/.ssh/id_ed25519      # 私钥，不要交给任何人
~/.ssh/id_ed25519.pub  # 公钥
```

建议为私钥设置 passphrase，设备遗失时私钥不会直接暴露。查看公钥：

```bash
cat ~/.ssh/id_ed25519.pub
```

复制完整一行（以 `ssh-ed25519` 开头）。

## 第 2 步：把公钥放到集群账户

通过平台网页终端或其他方式登录集群，在远端执行：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "粘贴你的完整公钥" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

注意：

- 使用两个大于号 `>>` 追加，单个 `>` 会覆盖已有 key。
- 公钥必须完整，包括开头类型与末尾注释。

## 第 3 步：配置本机 SSH Host

编辑本机 `~/.ssh/config`，加入：

```sshconfig
Host 107.ustc.edu.cn
  HostName 107.ustc.edu.cn
  User 你的集群用户名
  IdentityFile ~/.ssh/id_ed25519
```

用户名一般为学号或平台显示的集群用户名。测试连接：

```bash
ssh 107.ustc.edu.cn "hostname"
```

首次连接需确认 host key，输入 `yes`。

## 第 4 步：建立本地代理通道（本地运行应用时）

普通 SSH 登录只打开远端 shell，不提供本地代理。本地运行的应用要访问 Slurm REST API，需要 SOCKS 代理：

- 使用 VS Code Remote-SSH 时，应用会自动扫描已建立的 SOCKS 端口；
- 或手动建立：

```bash
ssh -D 50700 107.ustc.edu.cn
```

保持该终端打开，应用即可使用：

```env
SLURM_API_PROXY=socks5h://127.0.0.1:50700
```

## 第 5 步（可选）：减少重复输入

macOS 可把私钥加入钥匙串：

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

并在 `~/.ssh/config` 对应 Host 下加入：

```sshconfig
  AddKeysToAgent yes
  UseKeychain yes
```

非 macOS 系统删除 `UseKeychain` 一行即可。

## 动态验证码（可选）

若账户启用了 Google Authenticator 动态验证码，在远端运行 `google-authenticator` 按提示配置（推荐选项：time-based token `y`、update config `y`、disallow repeated token `y`、rate limiting `y`）。开启后 SSH 登录需要依次提供私钥 passphrase 与 6 位动态验证码；建议保持一个长期复用的连接（VS Code Remote-SSH 或 `ssh -D`），避免重复验证。

## 验证清单

```bash
ssh 107.ustc.edu.cn "hostname"
ssh 107.ustc.edu.cn "command -v scontrol"
```

两条命令均正常返回即配置完成。
