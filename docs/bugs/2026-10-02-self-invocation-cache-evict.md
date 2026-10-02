# BUG-007：self-invocation 绕过 @CacheEvict——index() 内部调 applyIndexResult 失效缓存

- 日期：2026-10-02（索引吞吐优化重构时引入、被既有测试当场抓住）
- 模块：backend-java（`service/IndexingService`）
- 状态：已修复（两个方法各挂一份 @CacheEvict）

## 现象

把 `@CacheEvict(retrievalCorpus)` 从 `index()` 下沉到新抽的 `applyIndexResult()`（供批量
消费者共用）后，`CacheEvictTests.corpusCacheHitsUntilReindexEvictsIt` 失败：单文件
`index()` 路径重建索引后，corpus 缓存里仍是旧语料（期望 0 chunk 实际 1）。

## 根因

Spring AOP 的经典陷阱：`index()` 内部 `this.applyIndexResult(...)` 是 self-invocation，
**不经过代理**，`applyIndexResult` 上的 `@CacheEvict` 对这条调用路径完全不生效。批量
消费者（外部 bean 经代理调 `applyIndexResult`）不受影响，只有单文件路径漏掉。

## 修复

`index()` 与 `applyIndexResult()` 各挂一份相同的 `@CacheEvict`（同 key 同 condition）。
双触发无害（evict 幂等），并在方法 javadoc 里写明"为什么必须是两份"。

## 预防 / 教训

- 把注解逻辑下沉到"共用方法"时，先问一句：**原调用路径会不会变成 self-invocation**？
  会被内部调用的公共方法，其缓存注解必须在对外入口也保留一份。
- 既有行为测试（CacheEvictTests 钉住"索引后缓存必须失效"）正是为这种重构回归准备的，
  本次它在第一时间拦住了。
