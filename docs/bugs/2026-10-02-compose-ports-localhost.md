# BUG-005：compose 未把 5672/6379 映射到宿主机，本地裸跑后端连不上中间件

- 日期：2026-10-02（docker compose + 本地裸跑组合冒烟时发现）
- 模块：docker-compose.yml / README 本地启动指引
- 状态：已修复（rabbitmq/redis 各加 `127.0.0.1` 回环端口映射）

## 现象

`docker compose up -d rabbitmq redis` 后两个容器 healthy，README 指引的本地裸跑
后端（`mvnw spring-boot:run`）也起来了，但 `/health` 聚合显示：

```json
{"ai_service": "up", "redis": "down", "queue": "down"}
```

上传/任何触发缓存的操作不可用（缓存静默穿透、索引任务投递失败会把文件标 failed）。

## 根因

初版 compose 只给 rabbitmq 映射了管理界面 `15672`，AMQP `5672` 与 Redis `6379`
都没有 `ports`——容器编排内服务经服务名互通没问题，但**本地裸跑的 Java 进程
默认连 `127.0.0.1:5672/6379`**，这两个端口在宿主机上根本没监听。
"README 教人起容器、后端默认连回环"两个半边各自正确，组合起来断了。

## 修复

```yaml
rabbitmq:
  ports:
    - "127.0.0.1:5672:5672"    # AMQP：本地裸跑后端用
    - "127.0.0.1:15672:15672"  # 管理界面
redis:
  ports:
    - "127.0.0.1:6379:6379"
```

只绑回环：本地开发可达，不对局域网暴露。修复后无需重启 Java——
Lettuce 与 AMQP 连接工厂惰性重连，`/health` 的 redis/queue 自动转 up。

## 预防 / 教训

- 写"本地开发用容器起中间件"的编排时，必须**站在宿主机进程视角**检查它要连的
  每个端口；容器间互通与健康检查都绿，不代表宿主机回环可达。
- `/health` 聚合端点在这次冒烟里直接把断点指出来了——多维健康检查的价值
  在于让"哪个半边断了"一眼可见。
