# Science Innovation Exam 任务包合同

更新日期：2026-10-19

本仓库分发副本：2026-10-19，与权威合同同步，支持文件和目录提交。

本文是正式五部分任务包的权威合同。项目目标与科学准入背景见[共建者背景](../contributor/build-sie-task-package/project-background.md)，安装与当前实现状态见[README](../README.md)。网站、Question Explorer 和实验框架不得自行定义与本文冲突的任务结构。机器可执行的字段约束以 `src/science_innovation_exam/contracts/schemas/` 为准；Schema 与本文冲突时先改本文再改 Schema。

共建者用的 Skill 与空模板在 `contributor/`。展示分与跨题 Arena 的完整算法见 [Matrix 算法](matrix.md)。

## 1. 任务包是什么

正式任务包不是论文、数据、构造脚本和审查记录的全集，而是从中抽出的**最小运行合同**：框架只靠它隔离 Agent、冻结提交、离线评分。一份获准提案对应一个正式包。包内 Agent 可见文本为英文。

Baseline（公开起点方法的可执行工件）不是任务包的一部分。构造信息、baseline 和正式任务包分开保存。`meta.json.task_sources` 说明构题原型和实际用到的数据；`meta.json.baseline` 只保存基线名称、地址和两个原始锚点分数。

## 2. 顶层结构

正式任务包只包含五部分：

```text
task_package/
├── instruction.json
├── input/
├── environment/
├── verifier/
│   ├── validation/run/main.py
│   └── test/
│       ├── run/main.py
│       └── data/                 # 可选；仅供 test 使用的私有规则和隐藏标签
└── meta.json
```

顶层不增加 `README`、`TASK.md`、构造历史、baseline、测试、来源证据全集或完整性清单。Verifier 内部除两个固定入口外由任务自行组织；最终评分的私有数据归 `test`，通常放在 `verifier/test/data/`，不另造第三个接口名。网站题面可以从 `instruction.json` 和 `meta.json` 生成。

## 3. `instruction.json`

只描述 Agent 要完成的任务及当前实验运行条件。

```json
{
  "instructions": "根据任务输入产生更好的科学结果，并按指定格式提交。这里同时说明任务目标和必要背景，不再拆分 objective。",
  "input": [
    {
      "path": "data.json",
      "format": "json",
      "description": "列出需要处理的任务实例。"
    }
  ],
  "submission": [
    {
      "path": "submission.json",
      "format": "json",
      "description": "最终提交文件的内容和格式要求。",
      "has_template": true
    }
  ],
  "environment": [
    {
      "name": "Python",
      "version": "3.12.5",
      "description": "初始环境已经安装的运行时。"
    }
  ],
  "requirement": [
    "禁止通过互联网查询本题的原始隐藏标签；允许调研解决该科学问题的方法。"
  ],
  "limitation": {
    "cpu": {"cores": 2},
    "memory": {"gb": 4},
    "gpu": {"count": 0},
    "runtime": {"seconds": 7200}
  }
}
```

字段规则：

- `instructions` 合并任务目标、问题说明和操作要求，不再单设 `objective`。使用英文。提案为中文时译成英文题面；不要写“应当 / 不必调用 validation”这类引导。
- `input` 是输入说明数组，可以为 `[]`。`input/` 下每个实际文件必须被某条 `input` 路径或某条提交模板路径覆盖。
- `submission` 是提交产物数组，至少一项，不再包一层 `artifacts`。只列框架要冻结并交给 Verifier 的最终产物。每条 `path` 可以是普通文件或目录；目录递归包含其全部内容，包括空目录。使用不带末尾斜杠的规范相对路径，如 `model`。禁止重复或父子重叠的声明。列出的全部默认必需，没有 `required` 字段。
- 每个提交产物默认有模板；`has_template` 省略时等于 `true`。文件或目录模板放在 `input/` 下同一相对路径，并复制到 Agent 可写的 `/submission/<path>`；目录模板递归复制且保留空目录。不提供模板时写 `"has_template": false`，由 Agent 自行创建最终产物。
- `environment` 向 Agent 说明已经安装的软件。任务不添加依赖、使用运营方默认镜像时写 `[]`，不要写“使用框架默认镜像”。机器可执行的环境在 `environment/`。
- `requirement` 是字符串数组，可以为 `[]`。每项只写一条任务专用硬要求，不重复框架已经保证的隔离规则。
- `limitation` 只有 `cpu`、`memory`、`gpu`、`runtime`。运营方尚未确认时写 `{}`。构造阶段的资源估计属于 `build/`，不能未经确认写成运行上限。运行器拒绝任何超过 `limitation` 的实验配置，所以它必须不超过运营方实际能提供的单容器资源。当前运营方正式评测环境（D 集群沙箱）的默认资源配置是 2 CPU 核、4 GB 内存、0 GPU；CPU 任务的 `limitation` 以此为上限，需要更多资源的任务先向运营方申请再写入。
- 没有 `storage`。运行器不执行磁盘配额。

未知但非执行关键的信息用有类型的空值：数组 `[]`，尚未分类的 `metric.type` 写 `null`。任务说明、`id`、至少一个提交产物、`environment/environment.json`、两个 Verifier 入口和评分所需输入不得为空。

`instruction.json` 不含 Schema 版本、评分转换、网络策略、任务版本、构造修订或校验和。具体 Agent、模型、种子和重复次数属于独立实验配置。

## 4. `input/`

只存放注入给 Agent 的数据：科学输入、任务实例和可选提交模板。不分 `public` / `private`，不放来源元数据、构造脚本、baseline、测试或校验和。

校验和不进入任务包。框架只读挂载 `input/`。构造侧的路径–SHA-256 清单保存在 Question Maker `build/`，由操作方持有。

Verifier 需要的隐藏评分材料由 Verifier 自己组织，不建立 `input/private`。

## 5. `environment/`

必须包含 `environment.json`，不能只放说明，也不能为空。

- `{"schema_version":"1.0","type":"default"}`：使用运营方为本次实验锁定的默认镜像。此时目录内不能有其他文件。
- `{"schema_version":"1.0","type":"containerfile","containerfile":"Containerfile"}`：默认镜像缺少任务依赖时，框架先用该文件构建任务镜像。Containerfile 必须以 `ARG SIE_BASE_IMAGE` / `FROM ${SIE_BASE_IMAGE}` 起步。依赖文件可与 Containerfile 放在同一目录。

`instruction.json.environment` 告诉 Agent 已经有什么；`environment/` 负责实际安装。两者不能互相替代。只有软件名称和版本都已冻结时才用 `containerfile`，并让两边列出同一份已安装软件。只点了名字、版本未钉时保持 `type: default` 和 `[]`，把名单记在 `build/`，不要放一个空的 Containerfile。

## 6. `verifier/`

两个固定入口：

```text
verifier/validation/run/main.py
verifier/test/run/main.py
```

两个 `main.py` 都必须存在，因为框架只认 `validation` 和 `test` 两个动作。

**validation** 只做一件事：题目需要 Agent 在研究中途核验时，回答这次核验。不需要中途核验的题仍须保留该文件，稳定返回本题没有中途核验即可。是否使用、请求长什么样、预算多少，一律跟已获准的科学设计走；合同不建议、不补充、也不削弱中途核验。题面只陈述任务，不写“应当 / 不必调用 validation”这类引导。设计里没有中途核验时，程序可返回 `this task has no mid-run check`。设计里有、请求格式未冻时，题面仍写该项核验，程序返回 `mid-run check request schema is not frozen`，不得留下模板那句「本题没有中途核验」，也不得编造请求格式。

框架以 `--input`、`--request`、`--response` 调用。请求和响应都是有字节上限的 JSON。科学字段、预算和耗尽规则由任务代码定义。框架不设通用科学调用次数。业务拒绝仍应退出码 0 并写出 JSON；进程崩溃、超时或非法 JSON 会结束实验并回滚 Context。validation 与 test 是两个独立程序。正式原始分只认 test；validation 的回复不能替代或改写最终分。

**test** 是提交冻结后的一次逻辑最终评分。框架以 `--input`、`--submission`、`--result` 调用。框架可对同一冻结输入重复执行以检查确定性。Agent 没有第二次提交机会。`--result` 必须符合 `verifier_result.schema.json`：`task_id` 等于 `meta.json.id`，`valid` 与 `status` 一致，无效时 `error` 为字符串、有效时为 `null`。

停止 Agent 后，框架只将声明的文件和完整目录树复制到独立评分快照，再以只读方式挂载。目录内的全部内容（包括缓存、备份和空目录）都会收取；声明范围外的内容忽略并保留在原输出中。声明路径缺失、路径中或目录内含符号链接和特殊文件时拒收。文件或目录的具体结构与内容要求由任务 Verifier 判断。

两个入口共享 `VerifyContext`：本次实验私有的 JSON 键值空间，Agent 不可见。两边都用 `VerifyContext.current()` 读写。`context.log(value)` 写一行诊断 JSON 到进程日志，不回传给 Agent，也不自动计分。

框架不规定 Verifier 的其他目录名。需要隐藏标签时由任务自己放在 `verifier/` 内，通常是 `verifier/test/data/`。没有隐藏材料时不必建 `data/`。

Verifier 必须在 Agent 开始前固定，本地、离线、确定性运行，并对缺失文件、非法输出、坏数字和自身失败作确定处理。框架不得把未声明的评分材料挂进 Agent 环境。Agent 只发送 `validation` 或 `test`，没有第三个 `finish` 动作。

## 7. 不进入任务包的框架提示词

所有题目都相同的说明，以及只随机房变化的事实，不写进任务包。运行时按这个顺序拼成一份提示词，写入该场的 `agent-state/prompt.txt`：

1. 通用层。框架自带的固定文本（`prompts/general.json`）。它告诉被测程序：读 `/instruction.json` 和只读的 `/input`；整个沙箱都是工作区，框架不指定草稿纸放在哪；`submission[].path` 是相对路径，`has_template` 省略或为真时模板在 `/input/<path>`，最终文件或目录一律写到 `/submission/<path>`；`input`、`environment`、`requirement`、`limitation` 分别是公开材料说明、已经装好的软件、本题硬性要求、CPU 与内存和时间上限，具体数字以题面为准。中途核对是 `sie-action validation --request 文件`：该文件里的 JSON 作为 `payload` 交给核对程序，框架不读取 `/submission`。交卷是 `sie-action test`，没有正文；框架只收取 `submission` 列出的路径，分数不发回。通用层不抄题面，也不抄某一场的核数、内存和秒数。
2. 环境层。由容器服务提供。现在 Docker 和 D 集群对程序没有差别，这一层是空的。以后的 GPU 机房才在这里写机器事实，例如有一块什么显卡。
3. 程序层。实验配置里的 `agent.prompt` 可选，默认空。只写这个程序自己的用法，不写科学要求，也不重复通用层。

实验配置没有 `system_prompt` 字段，也不能在通用层和机房层里二选一。科学要求只写在 `instruction.json`。集群细节不回流到任务合同。

## 8. `meta.json`

简洁索引，不保存构造历史。必需字段：`id`、`title`、`domain`、`task_sources`、`metric`。没有 `source_id`。

```json
{
  "id": "task_id",
  "title": "任务标题",
  "domain": "biology",
  "task_sources": [
    {
      "name": "构题原型或所用数据",
      "url": "https://stable-source.example/item"
    }
  ],
  "metric": {
    "description": "准确率百分比，越高越好。",
    "type": "linear_maximize"
  },
  "baseline": {
    "existing_baseline": true,
    "name": "公开基线方法",
    "source": "https://stable-project.example/baseline/",
    "score": 99.0,
    "bottom_line_score": 10.0
  }
}
```

没有可复现方法、只有专家估计时：

```json
{
  "baseline": {
    "existing_baseline": false,
    "name": "",
    "source": "",
    "score": 99.0,
    "bottom_line_score": 10.0
  }
}
```

字段：

- `id`、`title`、`domain`：任务身份、展示名称、领域。`id` 匹配 `^[a-z0-9][a-z0-9._-]{0,127}$`。
- `task_sources`：构题原型，以及真正进入 `input/` 或 Verifier 的数据集、仓库或构造依据。每项只有 `name` 和稳定 `url`。只用来论证重要性的论文留在 Proposal 或 `build/`。
- `metric.description`：用话说清原始指标和优劣方向。
- `metric.type`：V1 展示变换类型。允许 `linear_maximize`、`linear_minimize`、`log_ratio_maximize`、`log_ratio_minimize`、`upper_residual_log`、`lower_residual_log`、`power_maximize`、`power_minimize`、`monotone_piecewise_maximize`、`monotone_piecewise_minimize`、`custom`、`raw_only`、`null`。`null` 表示尚未分类；`raw_only` 表示已审查、不做 0–1000 换算。V2 总榜只读 Verifier 原始分。
- `linear_*` 与 `log_ratio_*` 可以没有 `transform`。此时必须有 `baseline`：框架把 `bottom_line_score` 映射为展示分 0，`score` 映射为 1000，无效展示分 -100，精度与舍入用框架默认值。
- `upper_residual_log`、`lower_residual_log`、`power_*`、`monotone_piecewise_*` 和 `custom` 必须自带完整 `transform`（方向、参数、锚点、无效分、精度、舍入）。`null` 和 `raw_only` 不得带 `transform`。
- `custom` 只允许 `verifier/matrix/transform.py` 中的纯函数 `to_utility(raw_value, parameters)`，输入输出都是有限 `Decimal`，越大越好。
- `baseline` 可选。出现时必须恰好有 `existing_baseline`、`name`、`source`、`score`、`bottom_line_score`。两个分数都是原始分。`score` 映射 1000，`bottom_line_score` 映射 0。锚点不封顶。有方向的类型上，`score` 必须严格优于 `bottom_line_score`（最小化则更小，最大化则更大）。
- `existing_baseline: true`：存在可复现公开方法。`name` 与 `source` 非空，`score` 是正式 Verifier 实测值。可执行 baseline 仍单独存放。
- `existing_baseline: false`：专家估计。`name` 与 `source` 必须是空字符串。正式 release 的展示坐标仍须由冻结校准批次确认。
- 既没有实测方法也没有专家估计时，省略整个 `baseline`，不要写占位对象。空模板里的数字只是形状，不能留在成品包中。

`meta.json` 不另设 `extension`、`schema_version`、`task_version`、完整性或 Verifier 入口字段。

## 9. Baseline 与正式注册位置

Baseline 使用 Agent 可见的 `input/` 和已声明环境，产出与 Agent 相同的提交文件。它属于实验框架，不进入五部分包。非空 baseline bundle 至少为：

```text
baseline_bundle/
├── baseline.json
├── run/main.py
└── ...
```

`run/main.py` 由框架以 `--input <root> --submission <root>` 调用。`baseline.json` 只能有 `id`、`task_id`、`title`、`source_id`。两个 ID 使用任务 slug 规则；`task_id` 必须等于正式任务 `meta.json.id`。`source_id` 是这条 baseline 工件自己的构造身份，不再要求等于已删除的任务 `meta.source_id`。

正式注册位置：

```text
science_innovation_exam/
├── task_packages/<task_id>/
└── baselines/<task_id>/<baseline_id>/
```

包被放进 `task_packages/` 只说明工件已保存。科学准入、合同、隔离、确定性 Verifier 和长期可持续性全部通过后才能正式提升。

## 10. 本包与三层分数

原始分由本包的 `test` 写出，是权威科学结果。展示分是单题阅读坐标：有 `transform` 时按包内变换计算，`linear_*` / `log_ratio_*` 省略 `transform` 时由框架用 `baseline` 两个锚点计算。Arena 对比分不读展示分，也不写进任务包。完整协议见 [Matrix 算法](matrix.md)。
