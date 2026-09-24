package com.raglatest.backend.api;

import com.raglatest.backend.internal.AiServiceClient;
import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 只读运维端点：暴露 Java→ai-service 边界的韧性状态
 * （各熔断实例的状态 / 失败率 / 计数，聊天舱壁水位）。
 *
 * <p>刻意不改动既有 {@code /health}——后者承载前端降级语义与既有测试，
 * 这里只做独立的可观测补充。</p>
 */
@RestController
@RequestMapping("/api/system")
public class SystemController {

    private static final JsonNodeFactory JSON = JsonNodeFactory.instance;

    private final CircuitBreakerRegistry circuitBreakers;
    private final Bulkhead chatTurnBulkhead;

    public SystemController(CircuitBreakerRegistry circuitBreakers, Bulkhead chatTurnBulkhead) {
        this.circuitBreakers = circuitBreakers;
        this.chatTurnBulkhead = chatTurnBulkhead;
    }

    @GetMapping("/resilience")
    public ObjectNode resilience() {
        ObjectNode body = JSON.objectNode();

        ObjectNode breakers = body.putObject("circuit_breakers");
        breakers.set(AiServiceClient.READS_CIRCUIT,
                describe(circuitBreakers.circuitBreaker(AiServiceClient.READS_CIRCUIT)));
        breakers.set(AiServiceClient.CHAT_CIRCUIT,
                describe(circuitBreakers.circuitBreaker(AiServiceClient.CHAT_CIRCUIT)));

        ObjectNode bulkhead = body.putObject("bulkhead").putObject("chat_turn");
        Bulkhead.Metrics metrics = chatTurnBulkhead.getMetrics();
        bulkhead.put("available", metrics.getAvailableConcurrentCalls());
        bulkhead.put("max", metrics.getMaxAllowedConcurrentCalls());
        return body;
    }

    private static ObjectNode describe(CircuitBreaker breaker) {
        ObjectNode node = JSON.objectNode();
        node.put("state", breaker.getState().name());
        CircuitBreaker.Metrics metrics = breaker.getMetrics();
        node.put("failure_rate", metrics.getFailureRate());
        node.put("buffered_calls", metrics.getNumberOfBufferedCalls());
        node.put("failed_calls", metrics.getNumberOfFailedCalls());
        node.put("successful_calls", metrics.getNumberOfSuccessfulCalls());
        node.put("not_permitted_calls", metrics.getNumberOfNotPermittedCalls());
        return node;
    }
}
