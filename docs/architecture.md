# Science Innovation Exam 实验框架架构

本文描述 `science_innovation_exam` 当前实际实现，不是规划图。它只跑一次五段任务包：隔离 Agent、允许中途反馈、冻结提交、断网评分、把结果写到磁盘目录。Matrix 计算库在同一包装里，但不参与这次实验主流程。

## 1. 它解决什么问题

研究 Agent 需要真实计算环境和可选联网，但评分材料、隐藏标签和最终分数不能被它看到或改写。框架因此拆成三层信任边界：

| 层 | 谁在跑 | 能看见什么 | 不能看见什么 |
|---|---|---|---|
| 宿主机控制器 | `sie run`，可信 | 配置、任务快照、Docker、动作服务、冻结提交、结果目录 | 不把模型密钥写入 `config.json` 或启动参数 |
| Agent 容器 | 被测脚本或 Codex | `/instruction.json`、只读 `/input`、可写 `/submission`、自己的 `/state`、动作通道 | Docker socket、`/verifier`、`VerifyContext`、隐藏测量、最终分数 |
| Verifier 容器 | 任务自己的 `main.py`，临时、断网 | `/input`、`/verifier`、`/verify-context`、`/output`，validation 另挂 `/request`，test 另挂 `/submission` | Agent 进程、外网、未声明的宿主机路径 |

Agent 有三种动作：`validation`（要任务定义的反馈）、`test`（接受一次最终提交并结束实验）、`abort`（报告实验设置问题并结束实验，不评分）。没有额外的 `finish`。

## 2. 一次实验怎么走

入口是 `science_innovation_exam.runner.run`，由 `sie run --config FILE` 调用。

```text
读 YAML 配置
  → 加载并检查五段任务包
  → 配置资源不得超过任务 limitation（当前不支持磁盘配额）
  → 新建 storage/runs/<时间-随机>/
  → 记录任务包指纹，不复制任务包
  → 构建运行镜像
  → 把 has_template 的模板拷到 /submission/<相对路径>
  → 创建 VerifyContext（本次实验私有的 JSON 键值库）
  → 如需联网则启动内部代理
  → 启动 Agent 容器并监听动作
  → Agent 退出、超时，或发出 test / abort
  → 先停掉 Agent 的全部写入进程
  → 仅 test 进入提交检查和最终评分
  → 断网容器执行 verifier/test/run/main.py
  → 写 result.json，清理本次容器和网络
```

失败不补做提交，也不伪造零分。状态包括：`scored`、`invalid_submission`、`incomplete`、`aborted`、`validation_failed`、`scorer_failed`、`infrastructure_failed`。abort 跳过提交冻结和最终评分，仍停止 Agent 并清理本次资源。

## 3. 组成部分

### 3.1 目录

```text
science_innovation_exam/
├── src/science_innovation_exam/   框架代码
├── examples/                      无隐藏科学答案的流程自检示例
├── configs/                       单次实验 YAML
├── tests/                         单元测试与脚本 Agent
├── tools/                         sie / 测试入口
├── containers/base-images/        CPU / GPU 基础镜像配方与构建脚本
├── docs/                          架构、验收、基础镜像、Matrix
├── data/                          镜像归档、资产、Codex 程序包（归档不进 Git）
└── storage/                       每次实验的磁盘结果；含隐藏资料
```

正式任务在框架仓库外独立管理，通过 YAML 的 `task` 路径加载；框架没有任务注册表。见[任务分发说明](task-packages.md)。参考方法的记录字段见[任务包合同](../contributor/build-sie-task-package/task-contract.md)，框架没有参考方法的注册表或执行器。

### 3.2 代码模块

| 模块 | 职责 |
|---|---|
| `cli.py` | `sie check` / `run` / `show` |
| `config.py` | 一份 YAML；密钥字段名直接拒绝；路径相对配置文件解析 |
| `task.py` | 五段合同：恰好五个顶层项、schema、输入声明、提交模板、只读快照、冻结提交 |
| `runner.py` | 单次实验生命周期；只通过 `container_service.interface` 接触容器，调用顺序固定为 check → publish → open_channel → start_agent → run_verifier* → stop → run_verifier(test) → close → cleanup |
| `images.py` | 在开发机用本地 Docker 构建本次镜像（基础镜像 + 可选任务层 + 框架接口层），返回镜像 ID 与本地标签 |
| `container_service/interface.py` | 容器服务接口：`MountSpec`（运行目录相对路径，或带 `shared` 的共用内容）、`ContainerSpec`、`Handle`、`Channel` |
| `container_service/docker.py` | 默认实现：`docker run` 只读根、丢权限、CPU/内存/PID、有上限的日志、显式删除；含出口代理（原 `network.py`） |
| `container_service/channel_socket.py` | 动作传输一：每次实验一个私有 Unix socket，位于 RAM 目录，是唯一不在运行目录内的容器可见文件 |
| `container_service/channel_mailbox.py` | 动作传输二：运行目录内 `control/{requests,responses,done}` 目录信箱，控制器按 `poll_seconds` 轮询 |
| `container_service/__init__.py` | `load(config, run_dir)`：`type: docker` 用内置实现，否则从可选 YAML `module_path` 目录按 `module` 名导入插件；不假定仓库相邻 |
| `agents.py` | 拼接通用、环境、程序三层提示词。小脚本复制进 `run/agent/`。Codex 程序包按指纹挂载，并申报要收回的日志路径 |
| `model_gateway.py` | 模型调用的本机转发。记录元数据，不记录题目原文和回复。`codex_adapter.py` 只修补 Codex 的请求格式 |
| `action_client.py` | 容器内 `sie-action`：按 `SIE_ACTION_TRANSPORT` 走 socket 或 mailbox |
| `actions.py` | 动作协议：串行、按 `request_id` 重试、test/abort 后关闭；`serve_bytes` 是所有传输的唯一入口 |
| `verifier.py` | 为 validation/test 各描述一个离线 `ContainerSpec` 交给容器服务；失败则回滚 Context |
| `verify_context.py` | 本次实验私有 SQLite JSON 键值空间；Agent 不可见 |
| `network_proxy.py` | 出口代理程序本体：HTTP/CONNECT、拒域名、拒私网、记元数据、不计费 |
| `common/host.py` | 宿主机文件规则：容器输出目录归容器用户；结果目录必须在磁盘 |
| `contracts/` | JSON Schema 与校验 |
| `matrix/` | V1 展示变换与 V2 Arena 纯计算；`sie run` 不调用 |
| `Containerfile` | 在基础镜像上加入 SDK 和 `sie-action` |

### 3.3 任务包合同

权威字段与目录见[任务包合同](../contributor/build-sie-task-package/task-contract.md)。当前加载器要求任务目录恰好是：

```text
instruction.json    给 Agent 的题面、输入说明、提交产物、环境说明、硬要求、资源上限
input/              只注入给 Agent 的数据与提交模板
environment/        environment.json；可选 Containerfile
verifier/
  validation/run/main.py   --input --request --response
  test/run/main.py         --input --submission --result
meta.json           身份、来源、指标说明
```

`test` 必须写出符合 `verifier_result.schema.json` 的对象：`valid`、`primary_metric`、`metrics`。隐藏分只出现在宿主机 `result.json` 和 `test/output/`。

## 4. 隔离与资源

Agent 与 Verifier 容器都使用：非 root 用户 `65532`、只读根文件系统、丢掉全部 Linux 能力、`no-new-privileges`、绑定 CPU/内存/PID、`/tmp` 为有大小上限的 tmpfs。Agent 默认经内部 `egress` 代理上网；配置 `network.mode: offline` 可关掉。Verifier 始终 `--network none`。只有任务显式请求 `execution.gpu_count` 时才给 Agent 分配 GPU，评分容器仍仅使用 CPU。

控制器执行运行时限。GPU 准备不扣 Agent 的实验预算；仅设置 `limitation.gpu.count` 上限不会分配设备。当前不执行磁盘配额。

## 5. 动作与共享状态

动作信封：

```json
{"schema_version": "1.0", "request_id": "<uuid>", "action": "validation|test|abort", "payload": {}}
```

同一 `request_id` 且内容相同则重放原响应；编号相同、内容不同则拒绝。validation 由框架串行调用。任务返回的业务拒绝（例如轮次或数量错误）仍是一次成功传递的动作，不结束实验。验证进程崩溃、超时或写出非法 JSON 则结束实验，并恢复该次调用前的 Context。

2026-10-14 abort 验证：框架单元检查 61 项通过，8 项 Docker 检查默认跳过；另运行真实 Docker abort 与容器回收检查共 2 项通过，覆盖 socket/mailbox，Agent 请求中止后仍睡眠等待，由控制器主动停止，无评分、无清理错误。结果目录为 `storage/docker-tests/abort-socket-lqaw76j9/runs/20261014T020636Z-78851fea` 和 `storage/docker-tests/abort-mailbox-v540huzc/runs/20261014T020650Z-8711eaa4`。

`abort` 的 payload 为 `{"reason": "Agent 简短描述实验设置问题"}`，只接受非空字符串，不预设原因类型。命令是 `sie-action abort --reason "..."`。原因保存在动作记录和 `result.json.abort_reason`（仍适用现有模型凭据脱敏）；成功回收后状态为 `aborted`，分数为 null。停止或清理失败则记为 `infrastructure_failed`，仍保留中止原因。该文本是 Agent 报告，不是控制器独立诊断；通用提示词向所有后端说明此动作。test/abort 都是终止动作，接受后拒绝新动作，但同一请求的重试仍可重放。

`VerifyContext` 是这次实验私有的持久键值库。validation 和 test 通过 `VerifyContext.current()` 读写同一份状态。Agent 看不到这份状态，也不能提交或修改实验返回值。`context.log(value)` 只写进程标准输出，不回传给 Agent，也不自动计分。

## 6. 结果目录

每次实验一个普通目录，没有结果数据库：

```text
config.json                 实际使用的配置
result.json                 最终状态与分数
task-package.json           题目编号、整包指纹、原目录
agent-state/                提示词、模型调用记录、程序申报的会话日志
agent-logs/                 标准输出和标准错误
actions/                    每轮请求、反馈、验证日志
context/                    私有 Context
submission/                 Agent 原始输出（可含额外文件）
frozen-submission/          仅声明文件的独立只读评分副本
test/                       最终评分输出
network/                    可选联网日志
```

整个目录属于运营方，不能再挂给被测 Agent。

## 7. 明确不做的事

当前主流程不做：GPU 评分、磁盘配额、断点续跑、多实验编排、从结果目录自动生成 Matrix 校准/Arena、正式 baseline bundle 执行、独立于磁盘目录的权威结果库、下载计费、HTTPS 解密或未知镜像识别。
