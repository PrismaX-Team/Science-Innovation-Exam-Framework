这里存放操作方基础镜像的 `docker save` 归档和旁边的 `.sha256` 文件。二进制不进入 Git。

运行器不会自动读取本目录。新节点先核对校验和，再 `docker load`。实验配置里的 `base_image` 使用加载后的标签。每次实验仍会在这张基础镜像上现场叠加一层很小的框架程序。

当前两个归档：

- `sie-cpu-base-0.1.0-linux-amd64.tar.gz` → `sie-cpu-base:0.1.0`
- `sie-gpu-base-0.1.0-cu128-linux-amd64.tar.gz` → `sie-gpu-base:0.1.0-cu128`

镜像里有什么、为什么 GPU 镜像不从 CPU 镜像继承，见 [`docs/base-images.md`](../../docs/base-images.md)。构建记录见 [`containers/base-images/manifest.json`](../../containers/base-images/manifest.json)。
