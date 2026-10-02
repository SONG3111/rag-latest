# BUG-002：test classpath 的 application.yml 整体遮蔽（而非合并）main 配置

- 日期：2026-10-02
- 模块：backend-java（为默认测试套件加 mock 配置时踩到）
- 状态：已修复（`src/test/resources/application.yml`）

## 现象

新建 `backend-java/src/test/resources/application.yml`，只写了三行测试覆盖
（`spring.cache.type=simple`、`spring.rabbitmq.dynamic=false`、listener 不启动），
期望它与 main 的 `application.yml` 合并。结果全部 `@SpringBootTest` 上下文加载失败：

```
Failed to instantiate [com.zaxxer.hikari.HikariDataSource]:
Factory method 'dataSource' threw exception with message:
Failed to determine a suitable driver class
```

`spring.datasource.url/driver-class-name`（定义在 main yml）全部丢失。

## 根因

Spring Boot 的 application.yml 是**按文件名加载**的：classpath 上出现多个同名
`application.yml` 时，只加载优先级最高的那一个（test-classes 排在 classes 之前），
**同名文件之间不做逐 key 合并**。profile 变体（`application-test.yml` + `@ActiveProfiles`）
才是"叠加"机制。

## 修复

test 的 `application.yml` 改为 **main 的全文拷贝 + 三处测试覆盖**（rabbitmq
dynamic/listener、cache simple），并在文件头注释里写明"整体遮蔽、须与 main 保持同步"
的结构性约定。

## 预防 / 教训

- 想给测试加少量覆盖时，优先考虑：类级 `@TestPropertySource` / `@DynamicPropertySource`
  （逐 key 覆盖、无遮蔽风险）；只有覆盖面广、且接受"维护一份拷贝"时才用
  test resources 的同名 yml。
- 排查这类问题最快的路径：看失败的 `Caused by` 最底层——"driver class"丢失直接指向
  datasource 配置消失，再往上是哪个 property source 顶掉了它。
