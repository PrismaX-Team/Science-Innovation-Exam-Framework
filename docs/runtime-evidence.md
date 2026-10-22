# 运行记录与输出上限

## 版本与环境

`provenance.json` 在加载后端前保存，随后补入实际加载的后端来源。它记录：

- 主框架、后端、任务所在 Git 仓库的提交号和修改状态，包括未跟踪项数量。后端通过实际 Python 类定位，不要求两个仓库相邻。
- 任务内容指纹。目录不在 Git 中、安装包没有 Git 元数据、或任务是忽略目录中的副本时，提交号为 `null`；不拿父仓库提交号冒充任务版本。
- 控制器的 Python、系统、可见 CPU/内存、cgroup v2 资源限制和部分已安装包版本；配置申请的 Agent/评分资源单独记录。
- 模型名称和推理设置。实际配置见 `config.json`，构建镜像 ID 与执行镜像引用见 `result.json`。

Agent 启动前另写 `agent-state/runtime.json`；每次 validation/test 启动前写该次 `output/runtime.json`。其中记录容器内实际可见的环境；存在 `nvidia-smi` 时，有时限地查询 GPU 名称、UUID、驱动和显存。查询失败不等于没有 GPU。远端 GPU 环境由对应插件提供，不能用 D Agent 容器的环境代替远端机器。

```yaml
provenance:
  timeout_seconds: 5       # 每次 Git / GPU 查询的时限
  maximum_bytes: 1048576   # 每次元数据查询的读取上限
```

主机上的版本记录归入准备时间；容器内的轻量环境记录属于命令启动开销，受实验/评分时限约束。无 Git、查询超时或输出过大时保留未知状态；元数据保存失败只记诊断，不伪造实验失败或分数。此记录不收集环境变量、Git 远端地址、代码差异或未跟踪文件内容。修改标记不是源码快照，精确复现仍应使用已提交版本。容器输出可被容器内程序修改，是诊断记录，不是防篡改证明。

## 日志与控制响应

`limits.log_bytes` 默认 16,000,000，限制每份标准输出/标准错误日志保留的前缀。达到上限后继续排空进程输出，不阻塞计算、不主动改变其退出码。Docker 保持原有捕获方式；外部插件可使用镜像内的 `science_innovation_exam.bounded_output` 写入日志，其 `capture.json` 记录是否截断及原始退出码。

收尾时对普通 `*.log` 文件分块脱敏并限制保留长度，正确处理跨块的密钥和截断边界。跳过符号链接与特殊文件；失败记入 `diagnostic_errors`。评分 JSON 通过 `limits.response_bytes` 控制读取上限，目录信箱请求通过 `limits.request_bytes` 控制读取上限；不依赖读取前的文件大小检查。

这些限制不是整个实验目录的磁盘配额：提交成果、会话 JSONL、评分中间文件和文件总数没有因此获得统一额度。第三方后端的写入与远端查询也须自行落实上限；主框架不会自动改变任意插件的行为。

D 适配器另有 `container_service.api_response_bytes` 与 `storage_response_bytes`，分别限制 HTTP 和 rclone 控制响应，默认均为 8,000,000。超限响应报错，不解析截断前缀；文件正文的批量上传/下载不当作控制响应读入内存。站点配置详见适配器 README。
