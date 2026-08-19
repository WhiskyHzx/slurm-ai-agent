# 107 Slurm REST API 能力确认报告

日期：2026-08-19  
本地项目：`/Users/mac/work/slurm-ai-agent`  
访问方式：Mac 本地运行后端，通过 VS Code Remote-SSH 的 SOCKS 隧道访问 107 API  
SOCKS 代理：`socks5h://127.0.0.1:50697`

## 1. 当前结论

本地运行方案已经跑通：

- `.env` 已包含 `SLURM_JWT`
- `.env` 已包含 `LLM_API_KEY`
- `.env` 已增加 `SLURM_API_PROXY=socks5h://127.0.0.1:50697`
- 本地 Python 虚拟环境 `.venv` 已安装依赖
- 本地 FastAPI 服务已启动在 `http://127.0.0.1:8080`
- 聊天接口实测成功调用 `get_diag`，能通过 LLM 自动查询集群实时状态

注意：`50697` 是 VS Code Remote-SSH 当前生成的 SOCKS 端口。如果重连 SSH，这个端口可能变化，需要重新检查 VS Code Remote-SSH 的 `socksPort` 并更新 `.env`。

## 2. 认证方式

107 的 slurmrestd 实测支持单独使用以下两种认证 header：

```http
X-SLURM-USER-TOKEN: <SLURM_JWT>
```

或：

```http
Authorization: Bearer <SLURM_JWT>
```

但实测发现：**两个 header 同时发送会返回 401**。

因此当前项目已改为只发送：

```http
X-SLURM-USER-TOKEN: <SLURM_JWT>
```

## 3. OpenAPI 描述

107 slurmrestd 暴露了 OpenAPI 描述：

```text
GET /openapi.json
GET /openapi/v3
```

实测：

- OpenAPI 版本：`3.0.3`
- API 标题：`Slurm REST API`
- Slurm 版本：`Slurm-25.11.2`
- 路径数量：`157`
- 方法统计：
  - `GET`: 136
  - `POST`: 69
  - `DELETE`: 44
- 标签统计：
  - `slurm`: 98
  - `slurmdb`: 149
  - `util`: 2

OpenAPI 文件已下载到：

```text
competition-evaluation/107-openapi.json
```

## 4. 实测可用的只读能力（详细说明 + 分类）

以下 endpoint 已用当前 `SLURM_JWT` + SOCKS 隧道实测。每个指令的具体作用、适合面向的视角，以及和用户个人/集群整体的关系，都做了说明。

> **视角说明——看「总体情况」还是「个人情况」：**
> - **总体情况**：反映整个集群的实时状态（所有节点、所有用户、整个分区的负载）。适合做 Dashboard / 资源监控、集群概况问答。
> - **个人情况**：只反映当前 token 对应的用户（自己）的作业/使用，不牵涉他人数据。适合做「我的作业」「我的配额」等个人中心功能。
> - 有些 endpoint 两者都涉及，会在说明中分别指出。

---

### 4.1 集群总体情况（Dashboard / 资源监控）

这一类 endpoint 反映**全局/集群尺度**的信息，适合用作主界面的资源监控 Dashboard、集群概况问答，以及判断「当前是否拥挤、该往哪个分区提交作业」。

#### `/slurm/v0.0.41/ping/`
- **作用**：探测 slurmctld 控制守护进程是否在线。
- **返回**：`pings=1`（数字表示探测成功的次数）。
- **实际价值**：作为**前提判断**——任何其他 Slurm 查询如果失败，都先用它确认控制节点是否活着。适合放在「刷新 Slurm」成功后的自检步骤里。

#### `/slurm/v0.0.41/diag/`
- **作用**：返回 slurmctld 的**调度统计与集群整体运行指标**，例如当前正在运行/排队/完成的作业数量、每秒调度次数、后台线程数量、以及每个线程/各时间段的统计（statistics 数据）。
- **返回**：`statistics=yes`，即完整统计信息。
- **视角**：**总体情况**。它能直接回答「现在集群里有多少作业、排了多长队、调度忙不忙」——非常适合做集群状态问答和 Dashboard 顶部统计卡片。

#### `/slurm/v0.0.41/jobs/`
- **作用**：列出**当前**（尚未结束的）作业，包含每个作业的 ID、名称、所属用户、分区、状态、申请/占用的节点与 CPU、内存、提交时间、运行时长限制等详细信息。
- **返回**：`jobs=28`（28 个当前作业）。
- **视角**：**总体与个人兼具**。不传参数时返回全集群所有用户的作业（总体）；但可以认为**当前任务集中在集群层面**。若只想看自己，需要在代码里按 `user_id` / `user_name` 过滤，或配合 `/jobs/state/` 使用。

#### `/slurm/v0.0.41/jobs/state/`
- **作用**：一个**更轻量的作业状态列表**——只返回每个作业的 ID、状态等少量字段，比 `/jobs/` 返回更快、数据量更小，适合高频轮询。
- **返回**：作业状态列表（实测有返回，数量与 `/jobs/` 口径可能有差异）。
- **视角**：**总体与个人兼具**，适合 Dashboard 上「作业情况」表格快速轮询（15 秒自动刷新场景下优先用它，负载更小）。

#### `/slurm/v0.0.41/nodes/`
- **作用**：返回每个**计算节点**的实时状态：节点名、所属分区、状态（idle/alloc/down/drain 等）、CPU 总数与已分配数、内存总量与已分配量、GPU/TRES 资源。
- **返回**：`nodes=28`（28 个节点）。
- **视角**：**总体情况**。天然用于「节点情况」表格和「分区资源利用率」进度条（按节点汇总出每个分区的 CPU/内存利用率）。

#### `/slurm/v0.0.41/partitions/`
- **作用**：列出所有**分区（队列）**的名称、节点数、配额上限、默认时间限制、可用性状态（up/down/inactive）等。
- **返回**：`partitions=7`（7 个分区）。
- **视角**：**总体情况**。用于「分区情况」表格——说明有哪些队列可选、每个队列能开多少资源，帮助用户在提交作业时选择合适分区。

#### `/slurm/v0.0.41/licenses/`
- **作用**：查询集群中登记的**许可证（license）**及其剩余数量。
- **返回**：`licenses=0`，即当前集群配置了 0 个许可证。
- **视角**：**总体情况**，目前价值有限（因为集群没有许可证）。

#### `/slurm/v0.0.41/reservations/`
- **作用**：查询**资源预约**信息——管理员或高级用户预先为某段时间预留的一批节点。
- **返回**：空（`reservations=0`），当前没有预约。普通展示意义不大，但可作为一种「近期是否有人预定大量资源」的提示。

#### `/slurmdb/v0.0.41/diag/`
- **作用**：SlurmDB（记账数据库）的诊断/统计端点。
- **返回**：`500`（服务端错误）。
- **实测结论**：当前**不稳定，不建议依赖**。

#### `/slurmdb/v0.0.41/clusters/`
- **作用**：列出 SlurmDB 中登记的**集群**信息（集群名、关联的节点/记账配置等）。
- **返回**：`clusters=1`（1 个集群）。
- **视角**：**总体情况**，属于基础元信息，展示价值较低。

---

### 4.2 个人情况（我的作业 / 我的配额）

这一类 endpoint 主要反映**当前 token 对应用户（自己）**的信息，适合做个人中心、通知、限额提醒等。

#### `/slurmdb/v0.0.41/users/`
- **作用**：返回**当前 token 对应用户**的信息（用户名、所属账号、默认分区、QoS、配额等）。
- **返回**：`users=1`（1 个用户，即你自己）。
- **视角**：**个人情况**。用于把「当前是谁在操作」「这个人属于哪个账号/哪个 QoS」展示给用户。

#### `/slurmdb/v0.0.41/jobs/`
- **作用**：查询**历史作业**（包括已完成、失败、被取消的作业），通过 slurmdbd 记账库查询。
- **返回**：`jobs=0`（当前默认查询为空——需要加查询参数，如 `job_id=`、`submit_time=`、`user=`，才返回有意义的历史记录）。
- **视角**：**个人情况**（但也能按用户/账号维度查询他人，默认查自己最常用）。用于「历史作业」「我的作业」「报错诊断——看看之前失败的作业日志」。
- **注意**：默认空结果是因为没带参数。要真正可用，需要在查询 URL 上带 `?user=<自己>` 或 `?job_id=<具体ID>` 等参数。

#### `/slurmdb/v0.0.41/qos/`
- **作用**：列出集群配置的**QoS（服务质量）**——每种 QoS 对应的 CPU/GPU 内存配额上限、优先级、时间限制等。
- **返回**：`qos=23`（23 种 QoS）。
- **视角**：**总体与个人兼具**。QoS 列表本身是全局配置（总体）；但结合 `/slurmdb/users/`（当前用户所属 QoS）可算出「我这个 QoS 还剩多少配额」（个人）。

#### `/slurmdb/v0.0.41/tres/`
- **作用**：列出**TRES（Trackable RESources，可跟踪资源）**的清单及计量单位——CPU、内存、GPU、GRES 等，是 slurm 做计费与统计的基本单位。
- **返回**：TRES 数据（CPU、GPU、内存等类型定义）。
- **视角**：**总体情况（元数据）**。主要用于理解 API 用法和资源字段口径，而不是直接展示给普通用户。

---

### 4.3 稳定 / 不稳定能力速查表

| endpoint | 状态 | 一句话作用 | 视角 |
|---|---:|---|---|
| `/slurm/v0.0.41/ping/` | 200 | 探测 slurmctld 是否在线 | 总体 |
| `/slurm/v0.0.41/diag/` | 200 | 集群整体调度统计 | 总体 |
| `/slurm/v0.0.41/jobs/` | 200 | 当前作业明细列表 | 总体/个人 |
| `/slurm/v0.0.41/jobs/state/` | 200 | 精简作业状态列表 | 总体/个人 |
| `/slurm/v0.0.41/nodes/` | 200 | 节点状态/资源 | 总体 |
| `/slurm/v0.0.41/partitions/` | 200 | 分区/队列信息 | 总体 |
| `/slurm/v0.0.41/licenses/` | 200 | 许可证剩余量 | 总体（少） |
| `/slurm/v0.0.41/reservations/` | 200 | 资源预约（当前无） | 总体 |
| `/slurmdb/v0.0.41/diag/` | 500 | SlurmDB 统计 | 不稳定 |
| `/slurmdb/v0.0.41/accounts/` | 500 | 账号列表 | 不稳定 |
| `/slurmdb/v0.0.41/config` | 500 | SlurmDB 配置 | 不稳定 |
| `/slurmdb/v0.0.41/clusters/` | 200 | 集群信息 | 总体 |
| `/slurmdb/v0.0.41/users/` | 200 | 当前用户信息 | 个人 |
| `/slurmdb/v0.0.41/jobs/` | 200 | 历史作业（需参数） | 个人/总体 |
| `/slurmdb/v0.0.41/qos/` | 200 | QoS 配额列表 | 总体/个人 |
| `/slurmdb/v0.0.41/tres/` | 200 | 可计费资源类型 | 总体（元数据） |

## 5. 实测不稳定或不建议依赖的只读能力

| endpoint | 状态 | 说明 |
|---|---:|---|
| `/slurm/v0.0.41/shares` | timeout | 12 秒无响应，不适合演示依赖 |
| `/slurmdb/v0.0.41/diag/` | 500 | SlurmDB diag 返回服务端错误 |
| `/slurmdb/v0.0.41/accounts/` | 500 | 账号列表返回服务端错误 |
| `/slurmdb/v0.0.41/config` | 500 | SlurmDB 配置返回服务端错误 |

## 6. OpenAPI 中存在但要谨慎使用的写操作

OpenAPI 显示 slurmrestd 支持大量 `POST` 和 `DELETE` 操作。它们可能需要更高权限，也可能会真实修改集群状态。

当前作品可以考虑使用，但必须加确认机制：

| endpoint | 方法 | 能力 | 风险 |
|---|---|---|---|
| `/slurm/v0.0.41/job/submit` | POST | 提交作业 | 会真实提交作业 |
| `/slurm/v0.0.41/job/{job_id}` | DELETE | 取消作业 | 会真实取消作业 |
| `/slurm/v0.0.41/job/{job_id}` | POST | 更新作业 | 可能修改作业属性 |

以下能力不建议学生作品默认开放：

| endpoint 类别 | 原因 |
|---|---|
| `/slurm/v0.0.41/node/{node_name}` POST/DELETE | 可能修改节点状态 |
| `/slurm/v0.0.41/reservation*` POST/DELETE | 可能创建/删除预约 |
| `/slurmdb/v0.0.41/accounts*` POST/DELETE | 账号管理风险高 |
| `/slurmdb/v0.0.41/users*` POST/DELETE | 用户管理风险高 |
| `/slurmdb/v0.0.41/qos*` POST/DELETE | QoS 修改风险高 |
| `/slurmdb/v0.0.41/clusters*` POST/DELETE | 集群管理风险高 |

## 7. 对当前项目的功能意义

当前项目已封装的 107 API 能力和实测结果基本匹配：

| 项目工具 | endpoint | 实测情况 | 说明 |
|---|---|---|---|
| `get_diag` | `/slurm/v0.0.41/diag/` | OK | 可用于集群状态问答和 Dashboard 统计 |
| `list_jobs` | `/slurm/v0.0.41/jobs/` | OK | 可查当前作业 |
| `get_job` | `/slurm/v0.0.41/job/{job_id}` | 未单独测 | OpenAPI 存在，可用性应较高 |
| `get_nodes` | `/slurm/v0.0.41/nodes/` | OK | 可查节点状态 |
| `get_qos` | `/slurmdb/v0.0.41/qos/` | OK | 可查配额 |
| `get_jobs_history` | `/slurmdb/v0.0.41/jobs/` | OK 但默认为空 | 需要加查询参数才更有用 |
| `submit_job` | `/slurm/v0.0.41/job/submit` | 未测 | 写操作，演示前必须 dry-run 或确认 |
| `cancel_job` | `/slurm/v0.0.41/job/{job_id}` DELETE | 未测 | 写操作，必须二次确认 |

## 8. 本地运行注意事项

Mac 本地直连 `http://107.ustc.edu.cn:6820` 返回 `502`，因此需要代理。

当前可用方案是复用 VS Code Remote-SSH 自动创建的 SOCKS 隧道：

```env
SLURM_API_PROXY=socks5h://127.0.0.1:50697
```

如果 VS Code 远程连接重启，SOCKS 端口可能变化。可以用下面命令查当前端口：

```bash
python3 - <<'PY'
import json, pathlib
base=pathlib.Path('/Users/mac/Library/Application Support/Code/User/globalStorage/ms-vscode-remote.remote-ssh')
for p in base.rglob('data.json'):
    d=json.loads(p.read_text())
    print(d.get('socksPort'))
PY
```

然后更新 `.env` 里的 `SLURM_API_PROXY`。

## 9. 已完成的本地服务验证

健康检查：

```text
GET http://127.0.0.1:8080/health
```

返回：

```json
{"status":"ok","agent_ready":false}
```

聊天请求：

```text
POST http://127.0.0.1:8080/chat
message = "请查询一下集群整体状态，简短回答"
```

实际行为：

- LLM 自动选择工具：`get_diag`
- 本地后端经 SOCKS 隧道访问 107 Slurm API
- 成功返回集群统计
- LLM 基于实时数据回复运行中/排队作业数量等信息

## 10. 下一步建议

1. 保留本地运行方案：Mac 后端 + VS Code Remote-SSH SOCKS 隧道。
2. 给项目增加自动发现 VS Code SOCKS 端口的能力，减少手动改 `.env`。
3. 增加 Dashboard API：
   - `/api/jobs`
   - `/api/nodes`
   - `/api/partitions`
   - `/api/qos`
   - `/api/cluster-summary`
4. 对写操作加硬性确认：
   - 提交作业前预览脚本和资源。
   - 取消作业前要求用户明确确认 job id。
5. 日志读取目前仍是本地文件读取；本地运行时读不到远端日志。需要改成通过 SSH/SFTP 或远端 helper 读取日志。

