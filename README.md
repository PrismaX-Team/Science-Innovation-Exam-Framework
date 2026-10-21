# Science Innovation Exam

运行被测 AI 程序（Agent）、收集中途验证请求与最终成果，再用任务自带的程序离线评分。默认使用本机 Docker；站点适配器可以作为外部插件加载。

框架代码采用 [MIT 许可证](LICENSE)。正式科学任务独立提供，通过 YAML 的 `task` 路径加载；仓库只保留不含隐藏科学答案的[流程自检示例](examples/README.md)。见[任务分发说明](docs/task-packages.md)。

先跑不需要模型密钥的 [CPU 自检](#cpu-quickstart)，再配置 [真实模型](#codex-setup)。维护代码的 AI 请先读 [AI_README.md](AI_README.md)；[AGENTS.md](AGENTS.md) 是仓库内的 AI 指令入口。

## 1．安装在什么环境

所有安装、配置和启动命令都在**控制器所在的 Linux 宿主机**执行。控制器是运行 `sie run` 的 Python 进程；它负责创建 Agent 和评分容器。

| 位置 | 需要准备什么 |
|---|---|
| Linux 宿主机 | 新部署建议 Python 3.11（含 venv/pip）、Git、`findmnt`、Docker CLI 和可用的 Docker Engine |
| 宿主机权限 | 控制器需要修改输出文件属主为 UID/GID 65532，通常以 root 运行；仅有 Docker 组权限不一定够 |
| 磁盘 | 仓库、Python 环境、Docker 数据、缓存和实验输出放在磁盘；不要放在 `/tmp`、`/dev/shm` 等内存盘 |
| Agent 与评分容器 | 由框架创建，默认使用非 root 用户；不需要用户进入容器设置模型环境变量 |

本机 Docker 后端要求控制器和 Docker Engine 能看到相同的宿主机文件路径。不要只把 `DOCKER_HOST` 指向另一台不共享这些路径的机器。

以下命令从 clone 后的仓库根目录执行。先确认文件系统不是 `tmpfs` 或 `ramfs`，再创建 Python 环境：

```bash
findmnt -T .
mkdir -p storage/tmp storage/pip-cache
python3.11 -m venv .venv
TMPDIR="$PWD/storage/tmp" PIP_CACHE_DIR="$PWD/storage/pip-cache" .venv/bin/python -m pip install -e .
```

Python 依赖由 [pyproject.toml](pyproject.toml) 安装。Docker、`findmnt` 和可选的 GPU 驱动属于宿主机依赖，不由 pip 安装。下面用 `sudo` 运行需要属主修改权限的命令；如果当前已经是 root，去掉 `sudo`。

<a id="cpu-quickstart"></a>

## 2．先跑 CPU 自检

**不需要 API 密钥、Codex 程序包、GPU 或集群账号。** 该自检用普通 Python 脚本扮演 Agent，核对输入挂载、中途验证、交卷、评分和资源回收；不是模型科学能力的评测。

```bash
sudo docker info
sudo docker pull python:3.11-slim-bookworm
.venv/bin/python -m science_innovation_exam check examples/integer_echo
sudo .venv/bin/python -m science_innovation_exam run --config configs/quickstart-cpu.yaml
```

镜像首次拉取需要宿主机联网；自检中的 Agent 和评分程序不联网。新配置只用公开的 `python:3.11-slim-bookworm`，不依赖开发者本机已有的 `sie-cpu-base` 镜像。

启动命令输出 `run_directory`、`status` 和评分结果。正常结束为 `status: scored`；分数由任务定义，不要求等于某个历史数值。查看这次实际输出的目录：

```bash
sudo .venv/bin/python -m science_innovation_exam show storage/runs/REPLACE_WITH_RUN_ID
```

`REPLACE_WITH_RUN_ID` 替换为刚才输出的编号，不是固定目录名。`sie check` 只检查任务结构；通过不等于科学问题已获准入，也不等于实验已经跑通。

自检资源上限为 Agent 2 CPU / 2048 MiB / 600 秒、每次评分 1 CPU / 512 MiB / 120 秒；这是流程测试配置，不是科学任务的最低资源估计。自检只复制一个公开整数，不需要正式任务数据。

<a id="codex-setup"></a>

## 3．运行 Codex 模型实验

### 先准备程序包和配置

Codex 程序包**不随 Git 分发，也不会由框架自动下载**。先准备一个与当前启动参数兼容、能在所选 Linux 容器中运行的程序包，目录中必须有可执行的 `bin/codex`。它的运行依赖也必须随包提供或已存在于基础镜像中。已有验收使用 Linux x86_64 的 0.147.0 包；任意全局安装的最新版不代表已经兼容。详见 [程序包要求](data/agent_bundles/README.md)。

```bash
cp configs/codex-cpu.example.yaml configs/my-codex.local.yaml
```

编辑新文件中的 `agent.bundle`（程序包目录）、`agent.model`（供应商支持的模型名）、`agent.reasoning_effort` 和资源预算。程序包路径可为绝对路径，或相对这份 YAML；模型名称写在 YAML，不通过另一个模型环境变量设置。示例采用公开的 Python 基础镜像；任务需要额外软件时，先按 [基础镜像说明](docs/base-images.md) 构建自己的镜像，再修改 `base_image`。

示例默认仍执行公开整数自检。正式实验另行取得任务包，再修改 `task` 指向其目录，并按该任务配置资源和网络策略；无需把任务复制进框架仓库。

### 模型环境变量放在哪里

在**将要启动控制器的同一个终端、作业脚本或服务环境**中设置。只在其他终端、开发工具配置或已经启动的容器里设置，不会改变控制器收到的环境。

| 变量 | 何时必需 | 谁读取 |
|---|---|---|
| `OPENAI_BASE_URL` | `agent.type: codex` 时必需；HTTP(S) 地址，必须以 `/v1` 结尾，服务需支持 Responses API | 宿主机控制器读取，容器内模型网关访问 |
| `OPENAI_API_KEY` | `agent.type: codex` 时必需；脚本自检不需要 | 宿主机控制器读取并传给模型客户端；不写入 YAML |
| `HTTP_PROXY` / `HTTPS_PROXY`、小写形式及 `NO_PROXY` | 仅宿主机下载或镜像构建需要代理时按环境配置 | pip、Docker CLI/构建等工具；不代表 Agent 的联网策略 |
| `SIE_ACTION_*`、`SIE_MAILBOX`、`SIE_VERIFY_SOCKET`、`SIE_VERIFY_CONTEXT`、`SIE_MODEL_GATEWAY_CREDENTIAL` | 用户不手动配置 | 框架为动作通道、评分状态和模型客户端自动设置 |

主框架**不会自动读取 `.env`**。可直接 export；也可使用模板，手动在当前 shell 加载：

```bash
cp .env.example .env
chmod 600 .env
# 在本机编辑 .env，填写实际地址和密钥；不要提交或粘贴密钥到日志。
set -a
. ./.env
set +a
```

`set -a` 使文件中的赋值导出给子进程；只写一个 `.env` 文件，或者仅定义未导出的 shell 变量，都不够。后台运行或服务启动时，也要在那个启动环境中加载变量。

普通 `sudo` 经常会清除环境变量。需要提权时，只保留本次必需的两个模型变量：

```bash
sudo --preserve-env=OPENAI_BASE_URL,OPENAI_API_KEY .venv/bin/python -m science_innovation_exam run --config configs/my-codex.local.yaml
```

已是 root 时，直接在已设置变量的终端运行 `.venv/bin/python -m science_innovation_exam run ...`。如果 sudo 策略不允许保留变量，应在允许运行控制器的环境中设置它们；不要把密钥改写进 YAML、启动参数或容器镜像。

### 区分三种联网

- **宿主机安装与镜像拉取**：pip 和 Docker 自己的联网配置。Docker Engine 的镜像拉取代理不等同于当前 shell 的代理。
- **Agent 与模型调用**：`network.mode: proxy` 通过本次实验的出口代理访问网络。模型地址必须从容器网络可达；地址中的 `localhost` 指容器本身，不是宿主机。确需访问宿主机服务时使用可达地址并显式配置 `network.allowed_private_hosts`；代理端口默认只允许 80/443，可用 `network.ports` 扩展。
- **验证和最终评分**：始终离线，不使用模型服务。

`network.upstream_proxy` 仅在所在环境需要上游 HTTP 代理时填写，示例默认不设置。它不是模型 API 地址，也不是 API 密钥。规则只覆盖配置的已知目标，不能宣称阻止所有数据镜像或未知泄漏路径。

## 4．配置、镜像与外部后端

[默认值](src/science_innovation_exam/config.py) 由代码定义。常用配置：

| YAML 字段 | 含义 |
|---|---|
| `task`、`output` | 五段任务包目录、实验结果根目录，必填 |
| `base_image`、`image` | 基础镜像、每次实验生成的镜像标签前缀 |
| `agent` | `scripted` 或 `codex`；程序、模型和可选附加提示词 |
| `resources`、`verifier` | Agent 和每次验证/评分的 CPU、内存、时限等上限 |
| `build` | 镜像构建时限及联网方式；默认构建步骤不联网 |
| `network` | Agent 的联网模式、允许端口和来源限制等 |
| `limits` | 日志、请求、反馈和评分状态的字节上限 |
| `container_service` | 默认 `{type: docker}`；或加载外部插件 |

`task`、`output`、`agent.bundle/script/prompt`、`container_service.module_path` 相对实验 YAML 解析，结果中的 `config.json` 保存解析后的路径。插件自有字段的路径规则由插件定义。

每次实验会在宿主机用 Docker 构建框架运行层，因此即使选择外部容器后端，当前控制器也仍需要本机 Docker。任务声明额外 Containerfile 时先构建任务环境；需要联网安装软件的构建步骤必须显式设置 `build.network: default`。Agent 程序包安装在宿主机，任务所用 Python 库则安装在容器镜像里，两者不共享 Python 环境。

外部插件通过配置加载，无需修改主框架，也无需仓库相邻：

```yaml
container_service:
  type: external
  module: my_container_backend
  module_path: ../../my_container_backend
```

`module_path` 指向**包含模块或包的目录**，不是具体 `.py` 文件；已安装的插件可省略。框架加载该模块的 `create(config, run_dir)`，后续通过 `ContainerService` 接口调用。插件依赖主框架接口，需使用兼容版本；其账号、凭据和站点命令按插件文档准备。本机 Docker 不需要集群账号或 SSH 密钥。

团队使用的 [D 集群适配器](https://github.com/PrismaX-Team/Science-Innovation-Exam-D-Cluster-Adapter) 单独保存在 private 仓库，需要授权才能克隆。其 `AGENTS.md`、`AI_README.md` 和 `README.md` 说明站点变量及 D/H 配置；总框架及本机 Docker 不依赖该私有仓库。

GPU 任务以 `instruction.json.execution.gpu_count` 显式请求设备；仅写 `limitation.gpu.count` 上限不会分配 GPU。本机 Docker 使用宿主机 GPU，需要额外准备 NVIDIA 驱动和支持 GPU 的 Docker 环境；远端 GPU 由外部后端管理。当前评分程序仍只分配 CPU。见 [GPU 说明](docs/gpu-tasks.md)。

## 5．Agent 如何交付

Agent 看到只读的 `/instruction.json`、`/input` 和可写的 `/submission`；看不到评分代码、隐藏评分输入或私有评分状态。最终成果写到 `/submission` 中任务声明的相对路径。框架自己的提示词按通用说明、后端环境说明、可选 `agent.prompt` 三层组合。

```bash
sie-action validation --request /tmp/validation.json
sie-action test
sie-action abort --reason "required experiment input is unavailable"
```

`validation` 提供任务定义的中途反馈；业务拒绝不一定结束实验，执行故障则结束实验并回滚该次评分状态。`test` 接受最终提交并结束 Agent，不把最终分数返回给 Agent。`abort` 以自由文本报告实验设置问题，中止、不评分，仍回收资源；普通代码错误和结果不佳应先自行处理。

停止 Agent 后，框架只复制 `submission[].path` 声明的文件或目录到独立只读挂载的 `frozen-submission/`。目录递归收取全部内容并保留空目录；声明范围外的缓存和备份留在原目录，不导致拒收。声明路径缺失、路径中或目录内含符号链接及特殊文件时拒收。远端 GPU 方案中，Agent 负责先把最终产物送回当前 Agent 机器的 `/submission`。

目录提交可声明 `{"path": "model", "format": "directory", "description": "Model artifacts", "has_template": false}`，由 Agent 创建 `/submission/model`。路径不带末尾斜杠；不增加文件或目录的类型字段，具体结构由任务评分程序检查。

## 6．实验记录与排错

Codex 默认启用[模型请求监督](docs/run-supervision.md)：首响应、连续无进展、单次请求总时限与整场预算分开，均可在 YAML 的 `model_monitor` 覆盖。控制器独立告警、停止和回收；保活不算有效进展，观测失联单独归因，基础设施故障不记零分。

所有 Agent 另有 `sandbox_monitor` 监督沙箱状态：确认删除与无法观测分开，连续/间歇失联有独立期限，137 无证据时保留未知原因。配置及后端接入见同一[运行监督说明](docs/run-supervision.md#沙箱与进程监督第二阶段)。

控制器被强杀后，可在原开发机、原路径执行 `sie cleanup --run /absolute/run/directory` 补做回收。运行中的实验拒绝清理；只回收登记且核验属于本次实验的资源，保留成果。创建结果不明、删除或下载未确认会记录待处理，允许重试。无需模型 API 密钥；Docker 权限和外部平台凭据仍需配置。完整边界见[异常回收](docs/run-supervision.md#控制器异常退出后的回收第三阶段)。没有自动扫描服务，也不续跑模型。

每次运行生成独立的 `output/<时间-编号>/`：

| 路径 | 内容 |
|---|---|
| `config.json`、`task-package.json` | 实际配置、任务标识和文件指纹 |
| `result.json` | 最终状态、评分、耗时及错误信息 |
| `resources.json` / `controller.json` | 持久资源归属、创建意图及控制器管理记录，不挂给 Agent |
| `cleanup-result.json` | 补做回收的当前结果；原实验结果通过 `recovery_cleanup` 引用 |
| `model-monitor.jsonl`、`model-activity-observed.json` | 模型请求告警、终止原因、控制器最后读到的状态 |
| `agent-logs/`、`agent-state/` | 运行日志、提示词、模型调用元数据和会话记录 |
| `actions/` | 动作请求、答复和每次验证的日志 |
| `submission/`、`frozen-submission/` | Agent 原始输出、最终评分用的声明文件副本 |
| `context/state.sqlite3` | 单次实验的验证/评分共享状态；不是跨实验索引数据库 |
| `test/`、`context-before-test.json`、`context-final.json` | 最终评分和状态快照 |

`preparation_seconds` 与 `agent_seconds` 分别记录准备和 Agent 执行耗时；实验预算从 Agent 启动后开始，`wall_seconds` 包含整个流程。`diagnostic_errors` 表示日志/元数据处理问题，结果仍会保存；`cleanup_errors` 表示资源回收失败，状态为 `infrastructure_failed`、`cleanup_status: pending`。模型故障另见 `failure_phase` / `failure_reason`，无分数。

| 状态 | 含义 |
|---|---|
| `scored` | 任务评分返回有效结果 |
| `invalid_submission` | 提交文件非法，或任务评分明确判为无效 |
| `incomplete` | 超时、提前退出或被中断，且未提交 |
| `aborted` | Agent 主动中止，原因见 `abort_reason`，分数为 null |
| `validation_failed` / `scorer_failed` | 中途验证 / 最终评分执行失败 |
| `infrastructure_failed` | 容器、镜像、文件操作或回收失败 |

CLI 对 `scored`、`package_valid` 返回 0，对其他已记录结果返回 1；配置解析或任务结构检查报错返回 2。先看 `result.json`，再看 `controller-error.log`、`agent-logs/` 和具体动作日志。

| 常见问题 | 检查方向 |
|---|---|
| 缺模型变量 | 是否在控制器的启动环境 export；sudo 是否丢失了变量 |
| `chown` / `Operation not permitted` | 宿主机权限、共享文件系统限制；Docker 可用不等于允许修改文件属主 |
| 找不到 `sie-cpu-base` | 首次运行改用 `quickstart-cpu.yaml`，或自行构建操作方镜像 |
| 找不到 `bin/codex` | `agent.bundle` 是程序包目录，路径相对 YAML，不是相对当前终端 |
| 模型连接失败 | 容器能否访问该地址、证书、代理、端口和私网允许项；不要靠打印密钥排查 |
| 提示输出位于内存盘 | 把 YAML 的 `output` 改到磁盘目录 |
| 插件无法导入 | 检查 `module_path` 指向的目录、模块名、版本及其依赖 |

运行目录可能包含隐藏评分资料、模型会话和操作方信息，默认不进入 Git，也不能整体作为下一次 Agent 的输入。框架没有跨实验数据库、排行榜服务或中断续跑入口。

## 7．开发与进一步阅读

安装后，在同样具备文件属主修改权限的环境运行：

```bash
sudo .venv/bin/python -B tools/test.py
sudo .venv/bin/python -B tools/test-docker.py
```

第一条不运行真实 Docker；第二条启动真实容器和本地 HTTPS 模拟服务，需要 Docker 与 `openssl`，不调用付费模型。测试临时文件放在仓库 `storage/` 的磁盘目录。

- [架构与实现位置](docs/architecture.md)
- [给维护代码的 AI 的指南](AI_README.md)
- [任务包合同](contributor/build-sie-task-package/task-contract.md)、[共建模板](contributor/README.md)
- [任务独立分发与发布历史注意事项](docs/task-packages.md)
- [基础镜像](docs/base-images.md)、[GPU 能力与验收边界](docs/gpu-tasks.md)
- [历史 CPU 验收记录](docs/cpu-acceptance.md)、[版本记录](docs/version-management.md)

结构可加载、执行成功、科学上有意义、最终分数可计算是不同结论。历史本地分数不是新安装的通过标准；每个科学任务的分数含义见任务自身说明，不由框架写死。
