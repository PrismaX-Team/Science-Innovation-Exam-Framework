# 运行故障监督

Codex 仍在 Agent 沙箱内自主调用模型和工具；本地模型网关仍在同一个沙箱。宿主机控制器读取网关的小型状态快照，独立判断等待是否超限、停止 Agent、保存证据并回收本次资源。没有新增外部网关或逐轮驱动 Agent。

## 配置

以下为可覆盖的运营默认值，不是模型思考时长的科学判断。不同模型、服务应在实验 YAML 中配置；完整定义在 [model_monitor_config.py](../src/science_innovation_exam/model_monitor_config.py)。各时间以秒计，告警不能晚于对应终止阈值。

```yaml
model_monitor:
  enabled: true
  check_seconds: 5                 # 控制器轮询及网关内部超时检查间隔
  query_timeout_seconds: 5         # 每次读取远端状态的上限
  progress_write_seconds: 2        # 中间进度/空闲状态更新间隔
  observation_warning_seconds: 30
  observation_timeout_seconds: 120
  first_response_warning_seconds: 120
  first_response_timeout_seconds: 600
  idle_warning_seconds: 120
  idle_timeout_seconds: 600
  request_warning_seconds: 900
  request_timeout_seconds: 1800
  connect_timeout_seconds: 30
  retry_attempts: 0                # 额外尝试次数；默认不自动重放请求
  retry_backoff_seconds: 1         # 启用重试时指数退避的起点
```

首响应时限从请求开始到首个有效协议事件；无进展时限从最近有效事件开始，仅在已经收到首事件后使用；总时限跨越该请求的全部尝试和退避。三者都与整场 `resources.timeout_seconds` 独立，准备时间仍不计入 Agent 预算。不会因收到保活或重试而延长请求总截止时间。

默认不重试。显式启用后，只对连接错误、HTTP 429/502/503/504 在交付响应前有限重试；开始交付的流不自动重试。它不会重放 Agent 工具、validation 或 test。原模型请求可能已经被服务接收，重试可能重复消耗推理资源；连接断开不保证服务端取消，记录为 `upstream_cancellation: unknown`。服务端取消 API 尚未接入。

`enabled: false` 关闭自动监督及网关的三类运行截止时间，仍保留调用元数据；连接建立仍受连接超时约束。默认开启仅作用于框架已接入的 Codex 网关，scripted Agent 不启动此监督。

## 观测与结果

- 请求开始、结束触发快照写入；中间进度合并写入。写入在独立线程，模型数据转发不等待共享存储写盘。状态写入失败会表现为外部观测失联。
- Responses SSE（同一 HTTP 连接上的连续事件）识别首次 `response.created`、非空增量、输出结构事件和 `response.completed`；注释、ping、反复 `response.in_progress` 不算有效进展。Chat SSE 识别内容/工具增量及 `finish_reason`。不要求输出内部思考。
- HTTP 200、收到字节和协议完整结束分别记录。SSE 缺少完成事件便断开会报 `upstream_incomplete_stream`；显式失败事件报 `upstream_protocol_error`。
- JSON 响应可识别 `status: completed/failed/incomplete`。其他协议只提供传输证据，不能声称已校验业务完成；单个待解析事件/JSON 缓冲有 256 KiB 上限。大块非流式 JSON 不提供持续进展证据，优先使用 Responses SSE。
- 只在确有在途请求时检查 API 时限。空闲网关的正常状态更新不会导致长时间训练或本地计算被误判为 API 停滞。
- 外部读不到状态、状态格式非法或序号长期不变时，告警并按观测时限结束，归为 `model_observation`，不声称服务端死亡。偶尔读回同一个旧快照不会重置宽限。
- 两端各用自身单调时钟。控制器使用快照中的持续时间，加上本机观察后经过的时间，不直接比较跨机器时钟。首次观察前的传输延迟无法精确推断；实际发现时间还包含轮询和状态传输延迟。

结果沿用 `infrastructure_failed`、`score: null`；`failure_phase` 区分 `model_api` 与 `model_observation`，`failure_reason` 给出具体原因。超限表示触发运营规则，不证明模型服务损坏，也不记为模型零分。主动取消连接保留记录，但不单独当作上游故障。

| 证据 | 内容 |
|---|---|
| `agent-state/model-activity.json` | 最新在途状态、最近 32 次结束记录及首个未恢复故障；仅元数据 |
| `agent-state/model-calls.jsonl` | 调用开始/结束、HTTP 状态、结束原因、响应字节数；不保存提示/回答/密钥 |
| `model-activity-observed.json` | 控制器最后成功读取的快照，清理后仍保留 |
| `model-monitor.jsonl` | 控制器去重告警和终止判定 |
| `result.json` | 故障阶段、原因、请求 ID、监测摘要及 `cleanup_status` |

`cleanup_status: completed` 表示后端回收调用完成且无未处理资源登记；`pending` 表示回收或相关收尾出错，具体见 `cleanup_errors`。异常退出后的补做回收见第三阶段。

## 后端与能力边界

统一接口为 `ContainerService.read_model_activity(timeout_seconds)`：仅读取至多 1 MiB 状态，不扫描会话日志。Docker 使用本地绑定目录；远程后端必须覆盖该方法并遵守读取时限。控制器还将读取放到有界等待的独立线程；即使后端忽略超时，也只保留一个在途读取，不阻塞停止路径、不反复创建线程。D 适配器通过 rclone/WebDAV 单文件读取，限制字节和整个子进程时限，不继承批量传输的长重试链。共享存储/平台真实延迟尚需站点验收。

本机制不是针对被测 Agent 的防篡改审计；网关与 Agent 仍共处一个沙箱。未接入网关的自定义客户端、额外网络请求、本地模型推理不在 API 观测范围内。

## 沙箱与进程监督（第二阶段）

所有 Agent 默认启用，由控制器检查执行进程和后端的平台证据。API 请求的有效进展与沙箱可达性分别判断，运行训练期间不会因为没有模型调用而触发沙箱故障。沙箱当前无法观测时优先按沙箱规则处理，暂缓控制器的 API 归因，网关自身请求截止时间继续生效。

```yaml
sandbox_monitor:
  check_seconds: 5
  query_timeout_seconds: 5
  warning_seconds: 30
  timeout_seconds: 120
  recovery_seconds: 30
```

这些是可覆盖的运营选择。第一次无法观测开始一次故障周期，超过告警/终止阈值分别告警/结束。仅有一次成功查询不会重置计时；持续健康达到 `recovery_seconds` 才结束这个周期。因此短暂恢复后再失联，不能无限延长实验。查询、轮询间隔以及同期模型监测耗时会计入实际发现延迟，不承诺在阈值到达的瞬间结束。

后端通过 `Handle.observe(timeout_seconds) -> AgentObservation` 返回 `running/exited/missing/failed/unknown`，只读、不执行停止。主控制器最多保留一个在途查询，有界等待，丢弃超时后才返回的旧结果；超时/异常/非法结构都归为无法观测。继承默认接口的其他插件仅观察原有进程 `poll()`，不自动获得管理面确认能力。

- Docker 查询本次容器的状态。Agent 容器退出后先保留到框架回收，以便读取退出码和 `OOMKilled`（Docker 明确报告内存耗尽导致终止）。inspect 失败后用精确名称列表确认缺失；列表查询也失败则保持未知。启动尚未登记的容器先等待，不直接判丢失。不会把整个 Docker inspect 响应写入证据。
- D 插件在执行查询 404、5xx、网络失败或格式异常后，补查管理面的沙箱本体；只有管理面 404 才确认沙箱不存在。平台明确终止也可结束实验。执行接口 404、平台查询失败或平台显示 running 但执行失联，均不等于机器被删除。异常时尽力读取平台事件，仅保留已识别的原因代码，不保存原始消息。
- 状态查询默认每次总预算 5 秒；D 将预算分给执行/管理/事件查询，监测读取每项限制 64 KiB，管理查询不继承长重试。状态未知时停止路径直接删除沙箱，不等待失联执行接口完成日志归档；已有远端成果仍按原有下载及保留规则处理。
- 退出码 137 单独记录 `agent_exit_137_unknown`。它表示终止原因尚未证实，不能直接称为内存不足；D 的 OOM 类平台事件也不单独证明被测进程的死因。Docker 的明确 `OOMKilled` 才记录 `sandbox_oom_killed`。
- 沙箱故障记录 `failure_phase: sandbox`、`infrastructure_failed` 和空分数；普通非零 Agent 退出仍为无 test 的 `incomplete`，附 `failure_phase: agent`，不伪造科学评分。终止动作交接前再观察一次；交接时状态未知则停止评分并记录 `sandbox_status_unavailable_at_handoff`。

`sandbox-observed.json` 保存控制器最近观察，`sandbox-monitor.jsonl` 保存状态变化、去重告警、稳定恢复和终止证据，`result.json` 增加 `sandbox_monitor` 摘要。证据保留在开发机，清理后仍在。D 删除后必须在管理面确认 404 才移除归属记录；默认确认等待 60 秒，可由插件 `container_service.delete_timeout_seconds` 调整。查询失败、仍存在或下载失败保留 `cleanup_status: pending`。

D 的旧 `container_service.status_grace_seconds` 仅保留给旧式 `poll/wait`；控制器运行中监督统一使用顶层 `sandbox_monitor`，迁移阈值时修改这里。需同时升级主框架和 D 插件。

第二阶段不包括运行中的远端 GPU 独立探测，也不代表已通过真实 D 平台故障验收。

## 控制器异常退出后的回收（第三阶段）

在原开发机、原实验路径执行；与运行实验使用相同的 Python 环境和 Docker 权限。外部后端仍需要插件及平台凭据，但不要求原任务包、模型 API 密钥或重新启动 Agent。

```bash
sie cleanup --run /absolute/path/to/run
# 从源码使用同一入口：
python3 -B tools/exam.py cleanup --run /absolute/path/to/run
```

成功退出码为 0；仍待处理为 1；记录格式错误、活跃控制器等拒绝操作为 2。可以重复执行，部分成功不会使下次重复删除已确认回收的资源。它是显式补做清理入口，没有安装定时扫描服务，也不恢复模型执行或重新评分。

每场运行在产生资源前创建 `resources.json`，记录创建意图、随机归属标记、请求标识、平台 ID、后端范围和删除确认。创建意图必须先原子写入并同步磁盘，之后才能发送创建请求；即使响应未返回，下一次也可以用该请求的归属标记找回已创建对象。正常收尾与异常回收共用这一登记。

| 对象 | 回收约束 |
|---|---|
| Docker 容器/网络 | 核对实验、归属、请求标签；已取得 ID 时同时核对不可变 ID；按 ID 删除并确认消失。Docker daemon ID 必须与初始记录相同。容器创建等待默认 60 秒，可在 `container_service.creation_timeout_seconds` 覆盖。 |
| 外部沙箱 | 插件必须保存带归属标记的创建意图，响应丢失时有界查找，删除前再次核验，删除后确认不存在。未实现 `recover_resources()` 的插件拒绝回收。 |
| 远端 GPU 及临时输入 | 由外部插件记录准确名字、命名空间及资源 UID（平台为每个对象分配的固定 ID）。确认所有作业与副本消失后才能删除本次 `gpu-input`；不接触原任务输入。 |
| 外部运行存储 | 核对归属文件；停止写入者后下载成果，成功才允许清除远端副本。显式保留策略记录在 `retained_resources`，不伪称已物理删除。 |

`controller.lock` 在整个运行及回收期间持有 Linux `flock` 排他锁。锁仍被持有就拒绝清理，不按心跳超时抢锁，也不凭 PID 杀控制器。`SIGKILL` 或主机重启会释放内核锁；记录中的旧 PID 不影响接管。文件系统必须提供一致的锁语义。当前只支持同一主机名、同一磁盘路径上的回收；跨主机、复制/移动后的实验目录拒绝操作，没有 `--force` 绕过选项。控制器卡死但仍持锁时，操作者先终止该控制器，再回收。

如果创建结果未知且当前也查不到对象，会保留 `intent/pending`，因为不能排除服务端稍后才完成创建。再次运行可重新查找；仍无明确证据时需要人工核对，不通过猜测把意图改成“已删除”。登记损坏、平台范围变化、权限错误、资源 ID 被替换、查询或删除未确认均不越过这一边界。旧版本没有登记的实验不自动推断资源归属。

外部存储写命令还需防止控制器死亡后的孤儿传输。已适配插件的 rclone 写操作通过轻量 Linux 父进程死亡保护执行：登记后才启动、记录进程身份后才执行命令，父进程被杀时子进程也被内核杀死。补做清理会检查进程启动标识和启动批次（用于区分 PID 重用及主机重启），等待确认写进程退出后才清理存储；它不会向重用 PID 的其他进程发送信号。无法确认停止仍保留待处理状态。该保护不泛化为任意第三方后台程序的终止保证。

`cleanup-result.json` 保存最近一次补做结果，未处理资源和每项错误仍保存在 `resources.json`。已有 `result.json` 的原始状态、分数和错误不改写，只附加 `recovery_cleanup`；没有最终结果的中断运行补记 `infrastructure_failed`、`failure_phase: controller`、`failure_reason: controller_lost`、`score: null`。提交文件、评分证据、原始日志均保留；Docker 崩溃时的本地工作副本也先保留为诊断材料。共享镜像、构建缓存和旧临时 socket 不按实验资源登记自动删除。

第三阶段通过本机真实进程强杀、真实 Docker 新进程回收及 D/H 模拟故障验收。没有重启开发机、没有执行真实 D/H 强杀回收；平台分页格式、元数据返回、UID 和删除传播仍需站点验收。远端 GPU 运行中独立故障检测、自动定时扫描及实验续跑仍未实现。

## 验证

```bash
python3 -B tools/test.py
python3 -B tools/test-docker.py tests.test_docker.DockerTests.test_model_fault_stops_and_cleans_real_docker_agent_without_score
python3 -B tools/test-docker.py tests.test_docker.DockerTests.test_platform_deleted_agent_is_detected_and_evidence_survives tests.test_docker.DockerTests.test_exit_137_retains_unknown_cause_and_removes_container
python3 -B tools/benchmark-model-monitor.py --concurrency 4 --calls 40
```

均使用假 API，无付费模型调用。单元故障包括无响应、仅保活、首事件后停滞、持续有效输出触及总时限、流中断、重试耗尽、有限恢复及观测缺失/陈旧/间歇读取失败。Docker 验证真实容器中保活停滞和流中断后的无分数结束、留证及回收。

性能脚本以两个独立进程、相同假流和并发数比较开关状态，输出首字节/请求延迟、吞吐、CPU、进程峰值内存、状态写入和本地查询量至 `storage/model-monitor-benchmark.json`。这是本机测量，平台查询为零，不能替代 D 集群压力测试；短测量有调度和文件系统噪声，不据此承诺开销可忽略。
