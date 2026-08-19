# SSH 连接准备说明

这份说明精简自“SSH权限开放参考操作”。目标是让本机可以连接 `107.ustc.edu.cn`，并让 `slurm-ai-agent` 能复用这条 SSH 能力刷新 JWT 和访问 107 API。

## 推荐目标

最终希望本机可以运行：

```bash
ssh 107.ustc.edu.cn "hostname"
```

并能成功返回远端主机名，例如：

```text
tradmin-02
```

如果这一步成功，应用里的“刷新 Key”按钮才有机会自动运行：

```bash
ssh 107.ustc.edu.cn "scontrol token lifespan=86400"
```

## 第 1 步：本机生成 SSH 密钥

在 Mac 本地终端运行：

```bash
ssh-keygen -t ed25519 -C "your_email@example.com"
```

一路回车会生成默认密钥：

```text
~/.ssh/id_ed25519
~/.ssh/id_ed25519.pub
```

建议给私钥设置 passphrase。这样电脑丢失时，私钥不至于直接被使用。

查看公钥：

```bash
cat ~/.ssh/id_ed25519.pub
```

复制完整一行，通常以 `ssh-ed25519` 开头，以邮箱或注释结尾。

## 第 2 步：把公钥放到 107 集群账户

先通过 107 网页或已有方式登录集群终端。

在远端集群终端运行：

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
```

然后把刚才复制的公钥追加到 `authorized_keys`：

```bash
echo "这里替换成你自己的完整公钥" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

注意：

- 是两个大于号 `>>`，表示追加，不会覆盖已有 key。
- 公钥要复制完整，包括开头的 `ssh-ed25519` 和末尾注释。
- 不要把私钥 `id_ed25519` 发给任何人。

## 第 3 步：配置本机 SSH Host

编辑本机文件：

```bash
nano ~/.ssh/config
```

加入：

```sshconfig
Host 107.ustc.edu.cn
  HostName 107.ustc.edu.cn
  User 你的集群用户名
  IdentityFile ~/.ssh/id_ed25519
```

用户名一般是你的学号或平台显示的集群用户名。

测试：

```bash
ssh 107.ustc.edu.cn "hostname"
```

第一次连接可能会要求确认 host key，输入 `yes`。

## 第 4 步：建立给本地应用使用的 SOCKS 连接

普通 SSH 登录只打开远端 shell，不会提供本地代理。为了让本地应用访问 107 Slurm REST API，需要 SOCKS 代理。

可以用 VS Code Remote-SSH。应用会自动扫描 VS Code 已经打开的 SOCKS 端口。

也可以手动运行：

```bash
ssh -D 50700 107.ustc.edu.cn
```

这个终端保持打开。然后本地应用可以使用：

```env
SLURM_API_PROXY=socks5h://127.0.0.1:50700
```

实际使用时 `./slurm-agent` 会自动扫描并更新这个值。

## 第 5 步：可选，减少重复输入

如果每次都要输入私钥 passphrase，可以把私钥加入 macOS ssh-agent：

```bash
ssh-add --apple-use-keychain ~/.ssh/id_ed25519
```

如果你的 OpenSSH 版本不支持这个选项，可以使用：

```bash
ssh-add ~/.ssh/id_ed25519
```

然后在 `~/.ssh/config` 里加入：

```sshconfig
Host 107.ustc.edu.cn
  HostName 107.ustc.edu.cn
  User 你的集群用户名
  IdentityFile ~/.ssh/id_ed25519
  AddKeysToAgent yes
  UseKeychain yes
```

`UseKeychain yes` 是 macOS 常用配置；如果你的系统不支持，删除这一行即可。

## Google Authenticator，可选

如果平台要求或你希望开启动态验证码，可以在远端集群终端运行：

```bash
google-authenticator
```

按提示用手机身份验证器 App 扫二维码，保存备用码。

常见选择：

- Google Authenticator
- Microsoft Authenticator
- FreeOTP
- 腾讯身份验证器

配置过程中一般可以按推荐选项：

- time-based token：`y`
- update config：`y`
- disallow repeated token：`y`
- time skew window：通常选 `n`
- rate limiting：`y`

开启后，SSH 登录可能会要求：

1. 私钥 passphrase
2. 6 位动态验证码

如果开启了动态验证码，建议使用一个长期打开的 VS Code Remote-SSH 会话或 `ssh -D` 终端，让本地应用复用已有连接。

## 验证清单

确认下面几条能工作：

```bash
ssh 107.ustc.edu.cn "hostname"
ssh 107.ustc.edu.cn "command -v scontrol"
ssh 107.ustc.edu.cn "scontrol token lifespan=86400"
```

再回到项目目录运行：

```bash
cd /Users/mac/work/slurm-ai-agent
./slurm-agent
```

