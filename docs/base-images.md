# 基础镜像说明

本页记录操作方基础镜像的配方与历史构建结果，归档不随 Git 分发。首次安装不必获取这些归档，按 [README 的 CPU 自检](../README.md) 使用公开的 Python 镜像即可；下列操作方镜像只在配置明确选用时才需要自行构建或加载。

版本：0.1.0
日期：2026-09-22

本文说明 Science Innovation Exam 目前提供的两张**基础镜像**。基础镜像是实验开始前就放在计算节点上的操作系统环境。每次实验不会重新下载这张大镜像，只会在它上面再加一层很小的框架程序。

面向仓库读者。构建配方在 `containers/base-images/`。下面这张表是 2026-09-22 这次构建的结果，机器可读副本在 `containers/base-images/manifest.json`。归档文件本身不进入 Git。

## 本次构建记录

| | CPU 基础镜像 | GPU 基础镜像 |
|---|---|---|
| 标签 | `sie-cpu-base:0.1.0` | `sie-gpu-base:0.1.0-cu128` |
| 镜像 ID | `sha256:6cbe52eeb9b5cb216cd8b6d7cfc9c685a92135989666a3a6d6ef0b110564af02` | `sha256:ff8982a7133bc029c7deef0d2c4ad954ed8b1b1a76a8afdfc57302c2da514a0d` |
| 归档 | `data/images/sie-cpu-base-0.1.0-linux-amd64.tar.gz` | `data/images/sie-gpu-base-0.1.0-cu128-linux-amd64.tar.gz` |
| 归档大小 | 343,354,688 字节（约 328 MB） | 9,868,304,952 字节（约 9.2 GB） |
| 归档 SHA-256 | `e9e4c2a5e84012c066baf276a307f65085d571414b5e9d5f97a27c6680d5c334` | `865ce1fd1bd6aceb4c3c1a1735d85ef2a4d76ef4f8b19f05910eb45f7f2d796f` |

两张镜像在构建结束时都通过了断网导入检查：`/opt/sie/selftest.py`。GPU 镜像另外确认 PyTorch 仍是 `2.8.0+cu128`，NumPy 仍是 `2.3.2`。

## 现在有哪些镜像

| 名称 | 标签 | 给谁用 | 里面有什么 |
|---|---|---|---|
| CPU 基础镜像 | `sie-cpu-base:0.1.0` | 不需要显卡的题目，例如蛋白稳定性筛选 | Python 3.11、常用命令行工具、一套钉死版本的数据分析库 |
| GPU 基础镜像 | `sie-gpu-base:0.1.0-cu128` | 需要显卡训练的题目 | 与 CPU 基础镜像相同的数据分析库，外加 CUDA 12.8 和 PyTorch 2.8.0 |

两张镜像遵守同一份 Python 包清单 `containers/base-images/requirements-baseline.txt`。GPU 镜像在软件能力上覆盖 CPU 镜像：会用表格和画图的程序，在两张镜像里看到的库版本相同。

GPU 镜像**不是**在 CPU 镜像上再装显卡驱动做出来的。显卡版 PyTorch 官方镜像和精简 Python 镜像是两条不同的系统，硬叠在一起既难固定版本，也不会更小。所以 GPU 镜像直接从已经固定的 PyTorch 镜像做起，再装同一份工具和同一份 Python 包。

## 每次实验实际用的镜像

实验配置里的 `base_image` 选择上面两张之一。运行器随后现场构建一张只属于这一次实验的镜像，名字类似 `sie-cpu:local-<本次后缀>`。这一层只加入评分程序接口和 `sie-action` 命令，大约几百 KB。同一台机器上，基础镜像已经在本地时，这一步是几秒，不是重新加载几 GB。

题目如果还需要别的软件，写在该题自己的 `environment/Containerfile` 里，并在 `instruction.json.environment` 里向 Agent 写明名字和版本。基础镜像里的库是所有使用该镜像的题目共用的，不逐题重复声明。

历史蛋白验收使用操作方 CPU 镜像；该任务已独立分发。公开 CPU 自检及 Codex 示例采用 `python:3.11-slim-bookworm`，不需要这些归档。

## 两张镜像都有的内容

### 系统工具

Python 3.11（命令是 `python3`）、`pip`、`ca-certificates`、`git`、`git-lfs`、`curl`、`wget`、`tar`、`gzip`、`unzip`、`xz-utils`、`zip`、`build-essential`、`pkg-config`、`procps`、`jq`、`rsync`。

CPU 镜像的 Python 来自 `python:3.11-slim-bookworm`，小版本是 3.11.15。GPU 镜像的 Python 来自 PyTorch 自带的 conda，小版本是 3.11.13。两边都是 3.11，补丁号不同，因为不能把 GPU 镜像里的 Python 换成另一套而不破坏 PyTorch。

### 数据分析库

这些版本在两张镜像里相同。NumPy 用 2.3.2，因为 GPU 底里的 PyTorch 2.8 已经带了这个版本；如果改成别的 NumPy，PyTorch 会坏。

| 包 | 版本 | 作用 |
|---|---|---|
| numpy | 2.3.2 | 数组计算 |
| pandas | 2.2.3 | 表格，例如 CSV |
| scipy | 1.17.1 | 数值计算 |
| scikit-learn | 1.7.2 | 划分数据、常用指标、简单模型 |
| matplotlib | 3.10.8 | 画图 |
| pillow | 12.3.0 | 读写普通图片 |
| h5py | 3.16.0 | 读写 HDF5（分层的科学数据文件，气象题的提交就用这种格式） |
| PyYAML | 6.0.2 | 读写 YAML 配置 |
| tqdm | 4.70.1 | 进度显示 |
| joblib | 1.6.0 | scikit-learn 使用的并行工具 |

完整的 `pip freeze` 写在镜像内的 `/opt/sie/python-packages.lock`。系统包列表在 `/opt/sie/system-packages.lock`。构建结束时会运行 `/opt/sie/selftest.py`，版本不对就失败。

## 只有 GPU 基础镜像多出来的部分

| 项目 | 版本 |
|---|---|
| 父镜像 | `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel` |
| 父镜像本地 ID | `sha256:6b99022ca960b0bd57f96f2283f2e8584101b2c72f2723bab6af64d6478e04df` |
| CUDA | 12.8 |
| cuDNN | 9 |
| PyTorch | 2.8.0+cu128 |

cuDNN 是显卡上跑神经网络时用的加速库。这张父镜像是带编译器的 `devel` 版本，题目可以在里面编译需要 CUDA 的 Python 扩展。

## 故意没有放进基础镜像的东西

下面这些体积大，而且只在一部分题目里需要。它们属于那道题自己的环境，不属于两张基础镜像：

- `transformers`、`tokenizers`、`datasets`、`accelerate`、`peft`、`trl`
- `deepspeed`、`bitsandbytes`
- `flash-attn`、`vllm`
- OpenAI 官方 Python 客户端、`boto3`

RSI-Bench 的 `optimizer-runtime`（压缩包约 9.5 GB）是一套语言模型微调环境。本项目的 GPU 基础镜像只用了它的 PyTorch/CUDA 父镜像，没有把那套微调包打进来。

## 节点上怎么得到镜像

镜像归档放在 `science_innovation_exam/data/images/`，不提交进 Git：

| 文件 | 对应标签 |
|---|---|
| `sie-cpu-base-0.1.0-linux-amd64.tar.gz` | `sie-cpu-base:0.1.0` |
| `sie-gpu-base-0.1.0-cu128-linux-amd64.tar.gz` | `sie-gpu-base:0.1.0-cu128` |

每个归档旁边有同名 `.sha256`。校验和就是上一节表格里的两行。加载前先核对，再执行：

```bash
sha256sum -c sie-cpu-base-0.1.0-linux-amd64.tar.gz.sha256
docker load --input sie-cpu-base-0.1.0-linux-amd64.tar.gz
```

GPU 归档同理。一台新的计算节点只需要加载一次，并放在该节点自己的 Docker 数据盘上（计算节点上用本机 NVMe，不要把 Docker 数据放在内存盘里）。之后每场实验都复用这张镜像。

Docker 的安装、磁盘位置和启动方式由宿主机管理员配置，本仓库不提供站点专用节点引导脚本。需要构建代理时显式设置 `SIE_BUILD_PROXY`；未设置时沿用 shell 的 HTTPS 代理，否则直连。

在已经有父镜像的机器上重新构建：

```bash
bash containers/base-images/build-base-images.sh
```

构建需要联网，用来安装系统包和 Python 包。实验运行时的镜像构建步骤默认不联网；所以这些库必须事先在基础镜像里，或显式配置任务构建联网，不能假定每场实验都能下载。

## 和「缓存」的关系

Docker 的层缓存只存在于这一台机器的 Docker 里。新开一台计算节点时，缓存是空的。如果每道题都在实验开始时下载 pandas、scikit-learn、matplotlib，每台新节点都要再付一次下载时间，而且当前实验构建默认不允许联网，下载会直接失败。

把这套库打进基础镜像之后，新节点的代价是启动时加载一次归档。同一节点上的后续实验只叠加那一层很小的框架程序。
