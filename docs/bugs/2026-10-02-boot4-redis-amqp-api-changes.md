# BUG-001：Spring Boot 4.1 下 Redis / AMQP 的 API 变化导致编译失败

- 日期：2026-10-02
- 模块：backend-java（引入 RabbitMQ + Redis 时踩到）
- 状态：已修复（`CacheConfig` / `InfraHealth` / `AmqpConfig`）

## 现象

引入 `spring-boot-starter-amqp` / `spring-boot-starter-data-redis`（Boot 4.1.1，
Spring AMQP 4.1.1、Spring Data Redis 4.1.1）后，按 Boot 3.x 经验写的代码出现四处编译失败：

1. `org.springframework.boot.autoconfigure.amqp.RabbitTemplateCustomizer` 包不存在；
2. `RedisCacheConfiguration.SerializationPair` 找不到符号；
3. `RedisServerCommands.ping()` 方法不存在；
4. `RabbitAdmin` 队列探测在新 API 下的正确姿势不明确。

## 根因

Boot 4 模块化 + Framework 7 / Spring Data 2025.1 一并改了多处 API（这些都不是
deprecation，是直接搬家/删除）：

| 旧（Boot 3.x） | 新（Boot 4.1 实测） |
|---|---|
| `o.s.boot.autoconfigure.amqp.RabbitTemplateCustomizer` | `o.s.boot.amqp.autoconfigure.RabbitTemplateCustomizer` |
| `RedisCacheConfiguration.SerializationPair.fromSerializer` | `o.s.data.redis.serializer.RedisSerializationContext.SerializationPair.fromSerializer` |
| `RedisServerCommands.ping()` | **已删除**，用同为 O(1) 的 `serverCommands().time()` 探活 |
| `getQueueProperties(String)` 返回 `Properties` | 新增 `getQueueInfo(String)` 返回 `QueueInformation`（带 `getMessageCount()`） |

## 修复

见 `config/CacheConfig.java`、`health/InfraHealth.java`、`queue/AmqpConfig.java`。
定位方法：本地仓库 jar 上 `javap -classpath <jar> <FQCN>` 逐一确认签名，不靠猜。

## 预防 / 教训

- 本仓库已有先例：resilience4j starter 因 Jackson 2/3 摩擦被刻意规避
  （`backend-java/pom.xml` 注释）。Boot 4.x 生态的第三方集成凡涉及 Jackson 2、
  autoconfigure 老包名的，动手前先 `dependency:tree` + `javap` 验证，别信 Boot 3 记忆。
- spring-amqp 的 `Jackson2JsonMessageConverter` 绑定 Jackson 2：消息体统一走
  String + Jackson 3 `ObjectMapper` 手动序列化（`ReindexProducer`），绕开摩擦。
