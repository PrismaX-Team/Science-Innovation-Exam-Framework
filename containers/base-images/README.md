# 基础镜像配方

对外说明见 [`docs/base-images.md`](../../docs/base-images.md)。这里只保留构建入口。

```bash
bash containers/base-images/build-base-images.sh
```

脚本假定本机 Docker 已经有：

- `python:3.11-slim-bookworm`
- `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel`，本地镜像 ID 必须是 `sha256:6b99022ca960b0bd57f96f2283f2e8584101b2c72f2723bab6af64d6478e04df`

成功后生成 `manifest.json`，并把两个 `.tar.gz` 和 `.sha256` 写到 `data/images/`。归档不进入 Git。
