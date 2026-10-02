# BUG-003：spring.rabbitmq.dynamic=false 时不注册 AmqpAdmin，上下文装配失败

- 日期：2026-10-02
- 模块：backend-java（`health/InfraHealth` 队列探针）
- 状态：已修复（`ObjectProvider<AmqpAdmin>` 可选注入）

## 现象

`InfraHealth` 构造器注入 `RabbitAdmin` 用于队列探活。默认测试套件的
`spring.rabbitmq.dynamic=false`（不向 broker 声明拓扑）之下，所有
`@SpringBootTest` 上下文启动失败：

```
No qualifying bean of type 'org.springframework.amqp.rabbit.core.RabbitAdmin' available
```

## 根因

Boot 的 AMQP 自动配置里 `spring.rabbitmq.dynamic` 直接控制是否注册 `AmqpAdmin`
bean（dynamic 的语义就是"运行时自动声明拓扑的管理员"）。关掉它等于这个 bean
不存在——任何强依赖注入点都会炸。

## 修复

`InfraHealth` 改为 `ObjectProvider<AmqpAdmin>` 可选注入：bean 缺席（测试配置 /
显式禁用声明）时队列探测恒为 down（`queueDepth()` 返回 -1），不影响上下文装配，
也不改变 `/health` 恒 200 + 降级字段的契约。

## 预防 / 教训

- 给"可选中间件"写探针/适配层时，默认用 `ObjectProvider` 或 `@Nullable` 注入，
  把"bean 不存在"当作一种正常状态建模——测试配置经常关掉自动装配。
- 相关取舍：`RedisConnectionFactory` 保持强注入（缓存与队列不同，Redis 连接工厂
  惰性建连、bean 恒存在），只有 AmqpAdmin 有这个开关语义。
