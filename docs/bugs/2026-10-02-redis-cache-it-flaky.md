# BUG-004：RedisCacheIT 断言与 @Retryable 合法重试冲突导致的 flaky

- 日期：2026-10-02
- 模块：backend-java（`it/RedisCacheIT.toolsCacheSurvivesRedisRoundTrip`）
- 状态：已修复（断言语义改为"第二次逻辑调用不再穿透"）

## 现象

`mvnw verify`（全量）时 `RedisCacheIT` 偶发失败、单独重跑同一用例通过：

```
VerificationException: Expected exactly 1 requests matching
{ "url": "/v1/tools", "method": "GET" } but received 2
```

失败时用例耗时 0.29s 左右——恰好容纳一次 ~200ms 的重试退避。

## 根因

`AiServiceClient.listTools` 上同时挂着 `@Retryable`（瞬时故障重试，200ms 起指数退避）
与新的 `@Cacheable`。WireMock 在本地偶发连接重置时，第一次**逻辑调用**会合法地产生
2 次 HTTP（1 次失败 + 1 次重试成功）。测试断言"恰好 1 次请求"实际上把重试也计入了，
把一个合法的韧性行为判为失败——是断言语义错了，不是缓存没生效。

## 修复

断言改为与重试正交的命中语义：

1. 第一次逻辑调用后请求数允许 1~3 次（重试合法），并断言 Redis 里 key 已写入；
2. 第二次逻辑调用后**请求数不再增长**（这才是"缓存命中"的本质）。

## 预防 / 教训

- 给带重试/熔断的调用写计数断言时，先想清楚计数口径是"逻辑调用"还是"物理请求"；
  二者差一个重试系数（本项目读路径 1+2）。
- "先精确断言失败、放松后通过、且单跑稳定"是 flaky 断言的典型指纹，优先怀疑
  测试语义而不是产品代码。
