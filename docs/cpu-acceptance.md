# CPU 框架重构验收（2026-09-08）

实验代码已在本目录独立建立 Git 仓库，初始快照为 `f6f16dc`。新运行路径仅使用五段式任务包；没有修改上游任务包的数据或评分，也没有使用历史固定分数作断言。

| 检查 | 结果 |
|---|---|
| 单元测试 | 33 项通过；另 6 项 Docker 测试由专用命令运行 |
| 真实 Docker 测试 | 6 项通过：动作链路、私有评分、故障分类、超时/提前退出、验证状态回滚、网络及清理 |
| HTTPS 代理与 Codex 适配器 | 本地 HTTPS 模拟服务通过；域名限制生效，测试密钥未写入配置和启动参数 |
| 实际 Codex 二进制 | Python 3.11 slim CPU 容器内 `codex --version` 成功，版本 0.147.0；没有调用真实模型 |
| 新蛋白任务正常提交 | 三个有效反馈轮次、480 次查询和最终 test 成功 |
| 新蛋白任务篡改提交 | 修改已经验证过的第一轮预测后，最终 test 判为无效 |
| Python 分发包 | wheel 构建、运行时 Containerfile 打包及安装后任务检查通过 |

所有测试使用磁盘工作目录。真实任务验收配置为 Agent 2 CPU/2 GiB，验证与评分 1 CPU/512 MiB，无 GPU；这些是测试上限，不是任务最低资源估计。模型接口测试完全使用本地模拟服务。

## 证据

以下是操作方保留的历史本地路径，不随 Git 分发，也不是新用户必须准备的文件。原始绝对路径已省略，运行编号和结果保持不变。

- 单元测试：`storage/refactor-unit-tests.log`
- Docker 测试：`storage/refactor-docker-tests.log`
- 独立 HTTPS 检查：`storage/refactor-https-tests.log`
- 真实 CLI 检查：`storage/codex-version.txt`
- 新包正常实验：`storage/protein-cpu-acceptance.json`
- 新包篡改实验：`storage/protein-tamper-acceptance.json`
- 安装包检查：`storage/package-check/build.log` 与 `check.json`

正常实验目录：`storage/runs/20260908T085646Z-eb8a85f7`。

篡改实验目录：`storage/runs/20260908T085049Z-824a1b84`。

目录中的 `result.json` 给出实际状态；各轮请求、反馈、日志、私有 Context 和固定提交可直接检查。实验目录包含隐藏资料，不能挂载给被测 Agent。

## 实现边界

真实模型供应商路由、真实 Agent 的科学表现尚未测试。框架执行任务包当前评分，不声明其科学准入通过。联网限制覆盖配置的已知目标，不解密 HTTPS 或识别未知数据镜像。正常实验只评分一次，确定性测试使用独立状态副本；没有断点续跑或旧格式兼容。

现有独立 Matrix 计算库继续保留，其内部分析合同不参与实验启动。旧生成的 setuptools 构建副本已清除，避免重新打包时带回已删除模块。

## 后续本地验收补记（2026-10-12）

原 README 中的本地运行记录迁到此处保留：蛋白任务在 D 集群用 Codex 跑出有效原始分 0.07367270；PR8 与 PR18 的本地候选包在 Docker 用 Codex 分别得到 0.69811693 和 0.66666667。后两者来自任务审查工作区，不自动计入正式注册任务。模型均为 `gpt-6-luna`。

这些结果证明具体版本的执行链路完成，不证明参考方法已复现或完整任务审查通过。原始操作方证据未随 Git 分发；这些数值不是新安装的验收阈值。已独立分发的蛋白任务的主分是最终候选的隐藏稳定性变化平均值，越低越好；旧综合评分已经退役，任务语义以任务自身说明和当前评分程序为准。

## 公开 README 快速启动检查（2026-10-15）

该历史版本的 `configs/quickstart-cpu.yaml` 当时使用蛋白任务（该任务现已移出框架），使用公开的 `python:3.11-slim-bookworm` 和脚本 Agent；不依赖操作方基础镜像、模型密钥、Codex 包或 GPU。实际 Docker 运行 `20261015T032015Z-0214a958` 返回 `scored`，全流程 30.124 秒，无回收错误；证据保留在 `storage/readme-quickstart.log` 和该运行目录。

当前公开自检已改为 `examples/integer_echo`；上述 30.124 秒是旧版本记录。

本次使用现有开发机控制器验证示例执行链路，未在全新宿主机上重新安装依赖，没有调用真实模型；不把这次脚本自检当作科学 baseline 或模型供应商验收。


## 正式任务拆分后的公开自检（2026-10-19）

当前 `quickstart-cpu.yaml` 只使用 `examples/integer_echo` 与公开脚本，不再需要蛋白任务。真实 Docker 运行 `20261019T031450Z-a394d606` 返回 `scored`，全流程 6.829 秒，无回收错误；中途验证和最终交卷均完成。证据为本地 `storage/public-echo-quickstart.log` 及该运行目录。不调用模型，不验证科学能力，也不代表全新宿主机安装或真实 GPU 验收。
