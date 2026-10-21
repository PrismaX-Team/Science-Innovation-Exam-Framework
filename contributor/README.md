# 共建者工具：Skill 与任务包模板

`contract_revision`: 2026-09-17

五部分字段与目录以[仓库内中文任务包合同](../docs/task-contract.zh-CN.md) 为准。本目录是共建者工作手册和空模板。

获准 Proposal 之后，用这两样东西把材料收成五部分正式任务包。

- **Skill**（给 AI 的工作手册）：[`build-sie-task-package/`](build-sie-task-package/)
- **模板**（空的合法五部分包）：[`build-sie-task-package/template/`](build-sie-task-package/template/)
- **额外软件示例**：[`build-sie-task-package/examples/environment-containerfile/`](build-sie-task-package/examples/environment-containerfile/)（题目需要新包时，用这份替换模板里的 `environment/`）

Skill 被 `@` 加载后会带上项目背景和任务合同，按现有材料能填的先填，缺会改变科学、分数、安全或合同的事实时再问你。模板给人对一眼看见目录长什么样；工作区里没有模板时，AI 也能按 Skill 里的合同搭出同一套骨架。

## 推荐工作区

把 Skill 目录拷进 Cursor / Codex 的项目 skills，把模板拷成 `task_package/`，再放入已获准提案和材料：

一份获准提案对应一个任务包。工作区里有几份提案，就建几套目录，不要合成一个包。

一份提案：

```text
workspace/
  .cursor/skills/build-sie-task-package/   # 本目录的副本
  proposal/                                # 已获准表单全文
  materials/                               # 论文、数据、代码、许可
  task_package/                            # 模板副本，只放五部分
  build/                                   # 缺口清单和测试，不进正式包
```

多份提案：每份一套 `<task_id>/{proposal,materials,task_package,build}`。第一轮只**复制**提案进各树的 `proposal/`，不移动你放在工作区根上的原件。

然后对 AI 说：按 `build-sie-task-package` 帮我准备正式任务包。正式包内文本为英文。

## 本地结构检查

模板和正在填写的包都应能通过同一条检查：

```bash
python3 build-sie-task-package/scripts/check_package.py task_package
```

若已安装实验框架，这条命令会走和 `sie check` 相同的加载器。未安装时用 Skill 内附带的 schema 做同一套结构规则。

绿灯只表示**目录能被加载**。科学是否准入、实验能否跑通、隐藏分现在算不算得出，仍要分开看。

## 版本

合同改了，本目录、附带 schema 和 `contract_revision` 必须同一提交一起改。请使用仓库里的当前副本，不要长期使用私下下载的旧 Skill。
