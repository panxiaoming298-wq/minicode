# MiniCode 工作区约定

## Docker Desktop 中间件分组

- 本工作区需要的向量数据库、关系型数据库、缓存、消息队列、对象存储及其他存储中间件，统一纳入 Docker Desktop 的 `minicode` Compose 应用组。
- 新增中间件时，优先在 `MiniCode-Python-repo/docker-compose.yml` 中增加服务、网络和具名卷；不要为单个中间件创建独立 Compose 项目或使用独立的 `docker run` 常驻容器。
- Compose 项目名保持为 `minicode`。启动、停止和检查中间件时，应通过该 Compose 文件操作，避免改变项目名导致 Docker Desktop 出现多个 MiniCode 应用组。
- 除非当前功能确实需要，不预装向量数据库或其他中间件；新增后必须配置健康检查和具名卷，且不得在未获明确要求时删除已有运行时卷。
