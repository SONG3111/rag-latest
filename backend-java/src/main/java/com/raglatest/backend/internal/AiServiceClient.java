package com.raglatest.backend.internal;

import com.raglatest.backend.api.ApiException;
import com.raglatest.backend.config.RagProperties;
import com.raglatest.backend.config.ResilienceConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import io.github.resilience4j.reactor.circuitbreaker.operator.CircuitBreakerOperator;
import java.time.Duration;
import org.springframework.core.ParameterizedTypeReference;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.resilience.annotation.ConcurrencyLimit;
import org.springframework.resilience.annotation.Retryable;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientRequestException;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import org.springframework.web.util.UriBuilder;
import reactor.netty.http.client.HttpClient;
import tools.jackson.databind.JsonNode;

/**
 * ai-service（FastAPI，内网 8001）的唯一客户端，同时承载 Java→ai-service 服务边界的韧性策略。
 *
 * <p>所有请求走相对路径，baseUrl 来自配置；noProxy() 与 Python 版 trust_env=False
 * 同因（本机代理环境变量会劫持内网调用）。上游 4xx/5xx 转成同状态码的
 * ApiException，detail 透传，保证前端看到的错误文本不丢语义。</p>
 *
 * <p>韧性分层（策略矩阵，勿随意扩张）：</p>
 * <ul>
 *   <li>幂等读（{@link #listTools()} / {@link #preview}）——{@code @Retryable} 只重试
 *       {@link TransientAiServiceException}（瞬时故障），配合读路径熔断；</li>
 *   <li>{@link #getHealth()}——不重试（健康检查要快），仅熔断 + 并发限制；</li>
 *   <li>清理类（{@link #dropCollection} / {@link #deleteFileVectors}）——仅熔断记账，
 *       失败由调用方降级为告警；</li>
 *   <li>{@link #streamChat}——<b>绝不重试</b>（响应式重试会重新订阅、重跑整轮）；
 *       用 Reactor 操作符按终止信号记账，并对已打开的熔断做前置快速失败。</li>
 * </ul>
 * 读路径与流式回合用两个独立熔断实例（{@link #READS_CIRCUIT} / {@link #CHAT_CIRCUIT}），互不牵连。
 */
@Component
public class AiServiceClient {

    /** 熔断实例名：短阻塞读路径。 */
    public static final String READS_CIRCUIT = "aiServiceReads";
    /** 熔断实例名：流式聊天回合。 */
    public static final String CHAT_CIRCUIT = "aiServiceChat";

    private final WebClient webClient;
    private final Duration requestTimeout;
    /** 重活（索引/嵌入）的墙钟预算：本地嵌入可能耗时数十秒，沿用整轮对话预算。 */
    private final Duration longOperationTimeout;
    private final CircuitBreaker readsCircuitBreaker;
    private final CircuitBreaker chatCircuitBreaker;

    public AiServiceClient(RagProperties properties, CircuitBreakerRegistry circuitBreakers) {
        this.requestTimeout = properties.aiService().requestTimeout();
        this.longOperationTimeout = properties.aiService().chatTurnTimeout();
        HttpClient httpClient = HttpClient.create().noProxy();
        this.webClient = WebClient.builder()
                .baseUrl(properties.aiService().baseUrl())
                .clientConnector(new ReactorClientHttpConnector(httpClient))
                .build();
        this.readsCircuitBreaker = circuitBreakers.circuitBreaker(READS_CIRCUIT);
        // 聊天实例用专用配置：整轮墙钟记成功/慢成功，不得按“慢调用”开闸（见 ResilienceConfig）。
        this.chatCircuitBreaker =
                circuitBreakers.circuitBreaker(CHAT_CIRCUIT, ResilienceConfig.CHAT_CIRCUIT_CONFIG);
    }

    /** 流式回合熔断的当前状态；供 ChatController 进入编排前做前置快速失败。 */
    public CircuitBreaker.State chatCircuitState() {
        return chatCircuitBreaker.getState();
    }

    // ------------------------------------------------------------------ //
    // 幂等读：重试（瞬时故障）+ 熔断 + 并发限制
    // ------------------------------------------------------------------ //
    /** 健康检查要快速返回，失败即降级，因此不重试。 */
    @ConcurrencyLimit(limit = 8, policy = ConcurrencyLimit.ThrottlePolicy.REJECT)
    public JsonNode getHealth() {
        return readsCircuitBreaker.decorateSupplier(() -> get("/health")).get();
    }

    /**
     * MCP 工具清单（含审批注解），GET /api/.../tools 的数据源。
     *
     * <p>重试在熔断内层：一次逻辑调用最多产生 1+maxRetries 次熔断记账，因此
     * {@code minimum-number-of-calls} 的口径是“尝试数”而非“逻辑调用数”（默认 5 约合
     * 2 次逻辑失败即开闸，属有意的快速保护）。{@code timeout} 给整轮（含重试与退避）
     * 设总预算，避免 3 × 30s 的最坏阻塞。</p>
     */
    @Retryable(maxRetries = 2, delay = 200, multiplier = 2.0, jitter = 100, maxDelay = 2000,
            timeout = 45000, includes = TransientAiServiceException.class)
    @ConcurrencyLimit(limit = 8, policy = ConcurrencyLimit.ThrottlePolicy.REJECT)
    public JsonNode listTools() {
        return readsCircuitBreaker.decorateSupplier(() -> get("/v1/tools")).get();
    }

    /** 引用出处预览：按 location 解析成读窗口后经 ai-service 的 MCP 读工具取原文。 */
    @Retryable(maxRetries = 2, delay = 200, multiplier = 2.0, jitter = 100, maxDelay = 2000,
            timeout = 45000, includes = TransientAiServiceException.class)
    @ConcurrencyLimit(limit = 8, policy = ConcurrencyLimit.ThrottlePolicy.REJECT)
    public JsonNode preview(String workspaceId, String file, String location) {
        return readsCircuitBreaker.decorateSupplier(() -> get(uri -> uri
                .path("/v1/workspaces/{id}/preview")
                .queryParam("file", file)
                .queryParam("location", location)
                .build(workspaceId))).get();
    }

    // ------------------------------------------------------------------ //
    // 索引：分块 + 嵌入 + 向量 upsert（不写库，分块行由本侧落库）
    // ------------------------------------------------------------------ //
    /**
     * 请求 ai-service 解析文件、嵌入子块并写入稠密索引，返回分块行（含 id）交本侧持久化。
     * 不走熔断：索引是显式触发的重活，失败应直接反馈给调用方而非被快速拒绝。
     */
    public JsonNode indexFile(String workspaceId, String fileId, String relPath) {
        java.util.Map<String, String> body = new java.util.LinkedHashMap<>();
        body.put("workspace_id", workspaceId);
        body.put("file_id", fileId);
        body.put("rel_path", relPath);
        return post("/v1/index", body);
    }

    /** 按名执行一个 MCP 工具并返回其 {"ok": ...} 信封（审批通过后由业务层调用写工具）。 */
    public JsonNode callTool(String tool, JsonNode arguments) {
        java.util.Map<String, Object> body = new java.util.LinkedHashMap<>();
        body.put("tool", tool);
        body.put("arguments", arguments);
        return post("/v1/tools/call", body);
    }

    private JsonNode post(String path, Object body) {
        try {
            return webClient.post()
                    .uri(path)
                    .contentType(MediaType.APPLICATION_JSON)
                    .bodyValue(body)
                    .retrieve()
                    .bodyToMono(JsonNode.class)
                    .block(longOperationTimeout);
        } catch (WebClientResponseException ex) {
            throw translateUpstreamStatus(ex);
        } catch (WebClientRequestException | IllegalStateException ex) {
            if (isTransient(ex)) {
                throw new TransientAiServiceException("ai-service unreachable: " + ex.getMessage());
            }
            throw ex;
        }
    }

    // ------------------------------------------------------------------ //
    // 清理类：仅熔断记账（失败由调用方降级为告警，不重试）
    // ------------------------------------------------------------------ //
    /** 删除工作区时清掉它的稠密索引集合；失败由调用方降级为告警。 */
    public void dropCollection(String workspaceId) {
        readsCircuitBreaker.decorateRunnable(() -> webClient.delete()
                .uri("/v1/collections/{id}", workspaceId)
                .retrieve()
                .toBodilessEntity()
                .block(requestTimeout)).run();
    }

    /** 删除单文件的向量（文件删除时），按 file_id 过滤。 */
    public void deleteFileVectors(String workspaceId, String fileId) {
        readsCircuitBreaker.decorateRunnable(() -> webClient.method(HttpMethod.DELETE)
                .uri(uri -> uri.path("/v1/collections/{id}/vectors")
                        .queryParam("file_id", fileId)
                        .build(workspaceId))
                .retrieve()
                .toBodilessEntity()
                .block(requestTimeout)).run();
    }

    // ------------------------------------------------------------------ //
    // 流式聊天：绝不重试，仅熔断（按终止信号记账）
    // ------------------------------------------------------------------ //
    /**
     * M2 无状态聊天：把 Java 预组装的请求发给 ai-service 的 POST /v1/chat/stream，
     * 返回 SSE 帧流（event + 原始 data 文本）由 ChatController 逐帧拦截/转发。
     * 不在此设超时——整轮墙钟预算由调用方的 Flux.timeout 控制（与
     * chat-turn-timeout 对齐后留余量给 persist 帧）。
     *
     * <p>熔断经 {@link CircuitBreakerOperator} 装饰：连接/HTTP 失败在终止信号时计为
     * 失败并可能打开熔断；打开后新回合在订阅即被拒（快速失败）。</p>
     */
    public reactor.core.publisher.Flux<ServerSentEvent<String>> streamChat(
            tools.jackson.databind.JsonNode request) {
        return webClient.post()
                .uri("/v1/chat/stream")
                .contentType(MediaType.APPLICATION_JSON)
                .accept(MediaType.TEXT_EVENT_STREAM)
                .bodyValue(request)
                .retrieve()
                .bodyToFlux(new ParameterizedTypeReference<ServerSentEvent<String>>() {})
                .transformDeferred(CircuitBreakerOperator.of(chatCircuitBreaker));
    }

    // ------------------------------------------------------------------ //
    // 传输 + 错误分类
    // ------------------------------------------------------------------ //
    private JsonNode get(String path) {
        return exchange(webClient.get().uri(path).accept(MediaType.APPLICATION_JSON));
    }

    private JsonNode get(java.util.function.Function<UriBuilder, java.net.URI> uri) {
        return exchange(webClient.get().uri(uri).accept(MediaType.APPLICATION_JSON));
    }

    private JsonNode exchange(WebClient.RequestHeadersSpec<?> spec) {
        try {
            return spec.retrieve().bodyToMono(JsonNode.class).block(requestTimeout);
        } catch (WebClientResponseException ex) {
            throw translateUpstreamStatus(ex);
        } catch (WebClientRequestException | IllegalStateException ex) {
            // 连接失败 / 连接或读取超时：可重试的瞬时故障（block 读超时表现为
            // IllegalStateException 且 cause 为 TimeoutException）。
            if (isTransient(ex)) {
                throw new TransientAiServiceException("ai-service unreachable: " + ex.getMessage());
            }
            throw ex;
        }
    }

    /** 上游状态码 → 异常：502/503/504 归为可重试瞬时故障，其余保持同状态码语义。 */
    private static ApiException translateUpstreamStatus(WebClientResponseException ex) {
        int status = ex.getStatusCode().value();
        String detail = readDetail(ex);
        if (status == 502 || status == 503 || status == 504) {
            return new TransientAiServiceException(detail);
        }
        HttpStatus resolved = HttpStatus.resolve(status);
        return new ApiException(resolved != null ? resolved : HttpStatus.BAD_GATEWAY, detail);
    }

    /**
     * 判定是否为可重试的瞬时故障。
     *
     * <p>连接建立/重置类与服务端超时可重试（IOException / TimeoutException，
     * 含 ConnectException、Netty 的 ConnectTimeoutException、PrematureCloseException
     * 与 block 读超时的 TimeoutException）；DNS 解析失败与 TLS 握手失败重试无意义，
     * 显式排除，不占用读路径的重试预算。</p>
     *
     * <p>包级可见：供分类回归单测直接调用。</p>
     */
    static boolean isTransient(Throwable ex) {
        if (causedBy(ex, java.net.UnknownHostException.class)
                || causedBy(ex, javax.net.ssl.SSLException.class)) {
            return false;
        }
        return causedBy(ex, java.io.IOException.class)
                || causedBy(ex, java.util.concurrent.TimeoutException.class);
    }

    /** 沿 cause 链查是否出现某类型（自环即止，避免异常链异常时死循环）。 */
    private static boolean causedBy(Throwable ex, Class<? extends Throwable> type) {
        for (Throwable current = ex; current != null; current = current.getCause()) {
            if (type.isInstance(current)) {
                return true;
            }
            if (current.getCause() == current) {
                break;
            }
        }
        return false;
    }

    private static String readDetail(WebClientResponseException ex) {
        try {
            JsonNode body = ex.getResponseBodyAs(JsonNode.class);
            if (body != null && body.hasNonNull("detail")) {
                return body.get("detail").asString();
            }
        } catch (Exception ignored) {
            // 非 JSON 错误体，退回状态文本
        }
        return "ai-service error: " + ex.getStatusCode();
    }
}
