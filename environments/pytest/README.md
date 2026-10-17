# 受控 pytest 执行镜像

此配方只为独立验证器提供 Python 与 pytest，不包含目标仓库源码、凭证或宿主配置。

默认构建（需要下载基础镜像与依赖）：

```powershell
docker build -t patchtrail-pytest:0.1 environments/pytest
```

若已经有受信任的 Python 镜像，可显式使用它作为构建基底，例如：

```powershell
docker build --build-arg BASE_IMAGE=algo-coach-runner:0.1 -t patchtrail-pytest:0.1 environments/pytest
```

后一种命令复用现有本地镜像，只在新镜像层安装 pytest；不会改动原镜像或宿主 Python。已有基底仅为本机验证便利，不作为可分发项目的默认依赖。构建属于独立的环境准备步骤，验证命令不会自动执行它。

pytest 直接依赖固定为 8.4.2，传递依赖由构建时解析，因此这份配方不是完整依赖锁。每次正式验证记录实际镜像内容 ID，避免把相同标签误认为相同环境；后续再补充传递依赖锁与平台摘要。
