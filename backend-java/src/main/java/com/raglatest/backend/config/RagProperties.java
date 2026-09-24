package com.raglatest.backend.config;

import java.time.Duration;
import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.boot.context.properties.bind.DefaultValue;

/**
 * 业务侧配置。环境变量经 application.yml 注入，与原 Python 版 Settings 中
 * 「业务/存储」段一一对应；AI 模型相关配置仍由 ai-service 自己读取 .env。
 */
@ConfigurationProperties(prefix = "rag")
public record RagProperties(
        /** 运行时数据根目录（app.db、workspaces/、backups/ 都在它下面）。 */
        @DefaultValue("./data") String dataDir,
        @DefaultValue AiService aiService,
        @DefaultValue Resilience resilience,
        /** 与原版 CORS_ORIGINS 语义一致；开发期同源代理下通常用不到。 */
        @DefaultValue({"http://localhost:5173", "http://127.0.0.1:5173"}) List<String> corsOrigins) {

    public record AiService(
            @DefaultValue("http://127.0.0.1:8001") String baseUrl,
            /** 普通内部调用（tools/health 等）的超时。 */
            @DefaultValue("30s") Duration requestTimeout,
            /** 一整轮对话（含工具循环）的墙钟预算，须与 ai-service 的 chat_turn_timeout 对齐。 */
            @DefaultValue("300s") Duration chatTurnTimeout) {
    }

    /**
     * 韧性容错参数（Java→ai-service 服务边界）。重试/并发限制用 Spring Framework 7
     * 原生注解（参数写在注解上，不在此重复）；这里只承载框架未内建的熔断与舱壁。
     */
    public record Resilience(
            @DefaultValue CircuitBreaker circuitBreaker,
            @DefaultValue Bulkhead bulkhead) {

        public record CircuitBreaker(
                @DefaultValue("50") float failureRateThreshold,
                @DefaultValue("20") int slidingWindowSize,
                @DefaultValue("5") int minimumNumberOfCalls,
                @DefaultValue("15s") Duration waitDurationInOpenState,
                @DefaultValue("3") int permittedNumberOfCallsInHalfOpenState,
                @DefaultValue("10s") Duration slowCallDurationThreshold,
                @DefaultValue("80") float slowCallRateThreshold) {
        }

        public record Bulkhead(
                /** 同时进行的聊天回合上限（保护单进程 ai-service 与 SQLite 单写者）。 */
                @DefaultValue("2") int chatMaxConcurrentCalls,
                /** 0 = 不排队，超出即快速拒绝。 */
                @DefaultValue("0s") Duration chatMaxWaitDuration) {
        }
    }
}
