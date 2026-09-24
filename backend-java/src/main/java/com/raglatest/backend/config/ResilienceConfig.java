package com.raglatest.backend.config;

import com.raglatest.backend.api.ApiException;
import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.bulkhead.BulkheadConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.resilience.annotation.EnableResilientMethods;

/**
 * Java→ai-service 服务边界的韧性装配。
 *
 * <p>选型刻意分两层：</p>
 * <ul>
 *   <li><b>重试 / 并发限制</b>走 Spring Framework 7 原生能力
 *       （{@code @Retryable} / {@code @ConcurrencyLimit}，由 {@code @EnableResilientMethods}
 *       注册），零额外依赖——这是框架已内建的部分；</li>
 *   <li><b>熔断 / 舱壁</b>框架未内建，用 Resilience4j 核心模块补位（无 starter、
 *       无 Jackson 依赖，规避 Boot 4 的 Jackson 2/3 摩擦）。</li>
 * </ul>
 *
 * <p>熔断分两个命名实例：{@code aiServiceReads}（短阻塞读）与 {@code aiServiceChat}
 * （流式回合），互不牵连。实例由 {@link io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry}
 * 按名派生（见 {@code AiServiceClient}），不单独声明同名 Bean，避免同类型多 Bean 的注入歧义。</p>
 */
@Configuration
@EnableResilientMethods
public class ResilienceConfig {

    /**
     * 聊天回合专用熔断配置名：与读路径共用一份参数，但**显式关闭慢调用判定**。
     * 流式调用按整轮墙钟记账（本项目常态 5~20s），若沿用读路径的慢调用阈值，
     * 正常长回答会被记为“慢成功”，窗口内占比超阈值即无故障开闸 → 周期性 503。
     */
    public static final String CHAT_CIRCUIT_CONFIG = "chat";

    /** 熔断注册表：读路径为基础配置，聊天实例覆写慢调用策略。 */
    @Bean
    public CircuitBreakerRegistry circuitBreakerRegistry(RagProperties properties) {
        RagProperties.Resilience.CircuitBreaker cb = properties.resilience().circuitBreaker();
        CircuitBreakerConfig readsConfig = CircuitBreakerConfig.custom()
                .failureRateThreshold(cb.failureRateThreshold())
                .slidingWindowType(CircuitBreakerConfig.SlidingWindowType.COUNT_BASED)
                .slidingWindowSize(cb.slidingWindowSize())
                .minimumNumberOfCalls(cb.minimumNumberOfCalls())
                .waitDurationInOpenState(cb.waitDurationInOpenState())
                .permittedNumberOfCallsInHalfOpenState(cb.permittedNumberOfCallsInHalfOpenState())
                .slowCallDurationThreshold(cb.slowCallDurationThreshold())
                .slowCallRateThreshold(cb.slowCallRateThreshold())
                // 只把服务端故障（5xx）与连接类异常计为失败：4xx 是确定性语义错误
                // （无效引用 / 未找到），不应让读路径被用户输入触发开闸。
                .recordException(t -> !(t instanceof ApiException api)
                        || api.status().is5xxServerError())
                .build();
        CircuitBreakerRegistry registry = CircuitBreakerRegistry.of(readsConfig);
        // slowCallDurationThreshold 抬到远超单轮墙钟（1h）→ 聊天回合永不被判“慢调用”；
        // 同时把慢调用率阈值设为 100，双保险（只以真实失败开闸）。
        registry.addConfiguration(CHAT_CIRCUIT_CONFIG, CircuitBreakerConfig.from(readsConfig)
                .slowCallDurationThreshold(java.time.Duration.ofHours(1))
                .slowCallRateThreshold(100f)
                .build());
        return registry;
    }

    /**
     * 聊天回合舱壁（信号量型，无额外线程）：
     * {@code SseEmitter} 立即返回使 {@code @ConcurrencyLimit} 无法覆盖整段流，
     * 故由 {@code ChatController} 在流生命周期内手动持有/释放许可。
     */
    @Bean
    public Bulkhead chatTurnBulkhead(RagProperties properties) {
        RagProperties.Resilience.Bulkhead b = properties.resilience().bulkhead();
        return Bulkhead.of("chatTurn", BulkheadConfig.custom()
                .maxConcurrentCalls(b.chatMaxConcurrentCalls())
                .maxWaitDuration(b.chatMaxWaitDuration())
                .build());
    }
}
