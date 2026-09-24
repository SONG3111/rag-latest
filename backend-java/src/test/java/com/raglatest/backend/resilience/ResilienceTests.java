package com.raglatest.backend.resilience;

import static com.github.tomakehurst.wiremock.client.WireMock.aResponse;
import static com.github.tomakehurst.wiremock.client.WireMock.get;
import static com.github.tomakehurst.wiremock.client.WireMock.getRequestedFor;
import static com.github.tomakehurst.wiremock.client.WireMock.okJson;
import static com.github.tomakehurst.wiremock.client.WireMock.post;
import static com.github.tomakehurst.wiremock.client.WireMock.postRequestedFor;
import static com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo;
import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.github.tomakehurst.wiremock.stubbing.Scenario;
import com.jayway.jsonpath.JsonPath;
import com.raglatest.backend.internal.AiServiceClient;
import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import java.nio.file.Path;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.web.server.context.WebServerApplicationContext;
import org.springframework.http.HttpEntity;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.ClientHttpResponse;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.web.client.ResponseErrorHandler;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.DefaultUriBuilderFactory;
import reactor.core.Disposable;
import tools.jackson.databind.ObjectMapper;

/**
 * Java→ai-service 服务边界的韧性回归（WireMock 桩掉 ai-service，无真实模型调用）：
 * 瞬时故障重试、4xx 不重试、熔断开闸快速失败、半开恢复、流式回合熔断前置、
 * 舱壁限流、客户端取消不误伤熔断、状态端点。
 *
 * <p>用例内把阈值调小（minimumNumberOfCalls=3 / openWait=1s / halfOpen=1 / 舱壁=1），
 * 使状态机可在毫秒级观测；@BeforeEach 复位熔断器，杜绝用例间状态污染。</p>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ResilienceTests {

    static WireMockServer aiService;

    /** 类加载即创建：@DynamicPropertySource 先于 JUnit 的静态 @TempDir 注入执行。 */
    static final Path dataDir;

    static {
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-resilience-tests");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> deleteTree(dataDir)));
    }

    /** 临时目录清理（WorkspaceController 的删除辅助是包级私有，跨包不可见）。 */
    private static void deleteTree(Path dir) {
        if (!java.nio.file.Files.exists(dir)) {
            return;
        }
        try (var paths = java.nio.file.Files.walk(dir)) {
            paths.sorted(java.util.Comparator.reverseOrder()).forEach(path -> {
                try {
                    java.nio.file.Files.delete(path);
                } catch (java.io.IOException ignored) {
                    // 尽力删
                }
            });
        } catch (java.io.IOException ignored) {
            // 尽力删
        }
    }

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) {
        if (aiService == null) {
            aiService = new WireMockServer(WireMockConfiguration.wireMockConfig().dynamicPort());
            aiService.start();
        }
        registry.add("rag.data-dir", () -> dataDir.toAbsolutePath().toString());
        registry.add("rag.ai-service.base-url", () -> "http://127.0.0.1:" + aiService.port());
        registry.add("rag.resilience.circuit-breaker.minimum-number-of-calls", () -> "3");
        registry.add("rag.resilience.circuit-breaker.sliding-window-size", () -> "10");
        registry.add("rag.resilience.circuit-breaker.wait-duration-in-open-state", () -> "1s");
        registry.add("rag.resilience.circuit-breaker.permitted-number-of-calls-in-half-open-state",
                () -> "1");
        // 把慢调用时长阈值调小，便于在毫秒级观测“慢成功”语义（聊天的 C1 回归靠它）。
        registry.add("rag.resilience.circuit-breaker.slow-call-duration-threshold", () -> "200ms");
        registry.add("rag.resilience.bulkhead.chat-max-concurrent-calls", () -> "1");
    }

    @AfterAll
    static void stopStub() {
        if (aiService != null) {
            aiService.stop();
        }
    }

    @Autowired
    CircuitBreakerRegistry circuitBreakers;
    @Autowired
    Bulkhead chatTurnBulkhead;
    @Autowired
    AiServiceClient aiServiceClient;
    @Autowired
    ObjectMapper mapper;
    @Autowired
    WebServerApplicationContext serverContext;

    RestTemplate rest;

    @BeforeAll
    void setUpRestClient() {
        RestTemplate template = new RestTemplate();
        template.setUriTemplateHandler(new DefaultUriBuilderFactory(
                "http://127.0.0.1:" + serverContext.getWebServer().getPort()));
        template.setErrorHandler(new ResponseErrorHandler() {
            @Override
            public boolean hasError(ClientHttpResponse response) {
                return false;
            }
        });
        rest = template;
    }

    @BeforeEach
    void resetState() {
        aiService.resetAll();
        circuitBreakers.circuitBreaker(AiServiceClient.READS_CIRCUIT).reset();
        circuitBreakers.circuitBreaker(AiServiceClient.CHAT_CIRCUIT).reset();
        // 舱壁复位：任何许可泄漏都应在此处暴露（否则后续用例会莫名 503）。
        assertThat(chatTurnBulkhead.getMetrics().getAvailableConcurrentCalls())
                .isEqualTo(chatTurnBulkhead.getMetrics().getMaxAllowedConcurrentCalls());
    }

    private CircuitBreaker reads() {
        return circuitBreakers.circuitBreaker(AiServiceClient.READS_CIRCUIT);
    }

    private CircuitBreaker chat() {
        return circuitBreakers.circuitBreaker(AiServiceClient.CHAT_CIRCUIT);
    }

    private String createWorkspace(String name) {
        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces", json("{\"name\": \"" + name + "\"}"), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(201);
        return JsonPath.read(response.getBody(), "$.id");
    }

    private static HttpEntity<String> json(String body) {
        org.springframework.http.HttpHeaders headers = new org.springframework.http.HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return new HttpEntity<>(body, headers);
    }

    // ------------------------------------------------------------------ //
    // 重试
    // ------------------------------------------------------------------ //
    @Test
    void retrySucceedsAfterTransientFailuresOnListTools() {
        String scenario = "tools-retry";
        aiService.stubFor(get(urlEqualTo("/v1/tools")).inScenario(scenario)
                .whenScenarioStateIs(Scenario.STARTED)
                .willReturn(aResponse().withStatus(503)).willSetStateTo("two"));
        aiService.stubFor(get(urlEqualTo("/v1/tools")).inScenario(scenario)
                .whenScenarioStateIs("two")
                .willReturn(aResponse().withStatus(503)).willSetStateTo("three"));
        aiService.stubFor(get(urlEqualTo("/v1/tools")).inScenario(scenario)
                .whenScenarioStateIs("three")
                .willReturn(okJson("[]")));

        ResponseEntity<String> response = rest.getForEntity(
                "/api/workspaces/{ws}/tools", String.class, "ws-retry");
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        // 1 次初始 + 2 次重试：恰好 3 次触达下游。
        aiService.verify(3, getRequestedFor(urlEqualTo("/v1/tools")));
    }

    @Test
    void retryDoesNotRetryOnClientError() {
        aiService.stubFor(get(urlEqualTo("/v1/tools"))
                .willReturn(aResponse().withStatus(400)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"detail\":\"bad tools\"}")));

        ResponseEntity<String> response = rest.getForEntity(
                "/api/workspaces/{ws}/tools", String.class, "ws-400");
        assertThat(response.getStatusCode().value()).isEqualTo(400);
        assertThat(response.getBody()).contains("bad tools");
        // 4xx 不重试：仅 1 次触达下游。
        aiService.verify(1, getRequestedFor(urlEqualTo("/v1/tools")));
    }

    // ------------------------------------------------------------------ //
    // 熔断
    // ------------------------------------------------------------------ //
    @Test
    void circuitOpensAfterFailuresAndFailsFast() {
        aiService.stubFor(get(urlEqualTo("/v1/tools"))
                .willReturn(aResponse().withStatus(500)));

        for (int i = 0; i < 3; i++) {
            ResponseEntity<String> failed = rest.getForEntity(
                    "/api/workspaces/{ws}/tools", String.class, "ws-open");
            assertThat(failed.getStatusCode().value()).isEqualTo(500);
        }
        assertThat(reads().getState()).isEqualTo(CircuitBreaker.State.OPEN);

        ResponseEntity<String> rejected = rest.getForEntity(
                "/api/workspaces/{ws}/tools", String.class, "ws-open");
        assertThat(rejected.getStatusCode().value()).isEqualTo(503);
        // 熔断打开后快速失败，不再触达下游（仍是 3 次）。
        aiService.verify(3, getRequestedFor(urlEqualTo("/v1/tools")));
    }

    @Test
    void circuitHalfOpensAndRecovers() throws InterruptedException {
        reads().transitionToOpenState();
        Thread.sleep(1100); // 超过 wait-duration-in-open-state(1s)
        aiService.stubFor(get(urlEqualTo("/v1/tools")).willReturn(okJson("[]")));

        ResponseEntity<String> response = rest.getForEntity(
                "/api/workspaces/{ws}/tools", String.class, "ws-recover");
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(reads().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
    }

    @Test
    void chatRejectedFastWhenCircuitOpen() {
        String ws = createWorkspace("熔断开闸");
        chat().transitionToOpenState();
        try {
            ResponseEntity<String> response = rest.postForEntity(
                    "/api/workspaces/{ws}/chat/stream",
                    json("{\"message\": \"在吗\"}"), String.class, ws);
            assertThat(response.getStatusCode().value()).isEqualTo(503);
        } finally {
            chat().transitionToClosedState();
        }
        // 熔断前置：不建 SSE、不触达下游。
        aiService.verify(0, postRequestedFor(urlEqualTo("/v1/chat/stream")));
    }

    // ------------------------------------------------------------------ //
    // 舱壁 + 取消语义
    // ------------------------------------------------------------------ //
    @Test
    void concurrentChatTurnsAreLimited() {
        String ws = createWorkspace("舱壁");
        // 占满唯一许可（chat-max-concurrent-calls=1），模拟进行中的回合。
        assertThat(chatTurnBulkhead.tryAcquirePermission()).isTrue();
        try {
            ResponseEntity<String> response = rest.postForEntity(
                    "/api/workspaces/{ws}/chat/stream",
                    json("{\"message\": \"在吗\"}"), String.class, ws);
            assertThat(response.getStatusCode().value()).isEqualTo(503);
        } finally {
            chatTurnBulkhead.releasePermission();
        }
        aiService.verify(0, postRequestedFor(urlEqualTo("/v1/chat/stream")));
    }

    @Test
    void clientCancelDoesNotCountAsChatFailure() throws InterruptedException {
        aiService.stubFor(post(urlEqualTo("/v1/chat/stream"))
                .willReturn(aResponse().withStatus(200)
                        .withHeader("Content-Type", "text/event-stream")
                        .withFixedDelay(3000)
                        .withBody("event: token\ndata: {\"type\":\"token\",\"text\":\"hi\"}\n\n")));

        Disposable subscription =
                aiServiceClient.streamChat(mapper.createObjectNode()).subscribe();
        Thread.sleep(300);
        subscription.dispose(); // 模拟客户端断开（取消流）
        Thread.sleep(300);

        // 取消不应被计为下游失败（否则用户频繁“停止”会误开熔断）。
        assertThat(chat().getMetrics().getNumberOfFailedCalls()).isZero();
        assertThat(chat().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
    }

    @Test
    void retryExhaustedStillReturns503WithUpstreamDetail() {
        // 重试耗尽后对外仍是 503 + 上游 detail（本次改动唯一影响错误形状的关键路径）。
        aiService.stubFor(get(urlEqualTo("/v1/tools"))
                .willReturn(aResponse().withStatus(503)
                        .withHeader("Content-Type", "application/json")
                        .withBody("{\"detail\":\"upstream busy\"}")));

        ResponseEntity<String> response = rest.getForEntity(
                "/api/workspaces/{ws}/tools", String.class, "ws-exhausted");
        assertThat(response.getStatusCode().value()).isEqualTo(503);
        assertThat(response.getBody()).contains("upstream busy");
        aiService.verify(3, getRequestedFor(urlEqualTo("/v1/tools")));
    }

    @Test
    void slowChatTurnsDoNotOpenChatBreaker() {
        String ws = createWorkspace("慢回合不误伤");
        // 整轮墙钟(≈400ms) > 测试调小的 slow-call 阈值(200ms)，属“慢成功”；
        // 聊天实例显式关闭慢调用判定，不得因正常长回合开闸（C1 回归）。
        aiService.stubFor(post(urlEqualTo("/v1/chat/stream"))
                .willReturn(aResponse().withStatus(200)
                        .withHeader("Content-Type", "text/event-stream")
                        .withFixedDelay(400)
                        .withBody("""
                                event: persist
                                data: {"type":"persist","status":"ok","content":"好","citations":[],"proposals":[],"trace_nodes":[],"summary":null}
                                """)));

        for (int i = 0; i < 3; i++) {
            ResponseEntity<String> response = rest.postForEntity(
                    "/api/workspaces/{ws}/chat/stream",
                    json("{\"message\": \"在吗\"}"), String.class, ws);
            assertThat(response.getStatusCode().value()).isEqualTo(200);
        }
        assertThat(chat().getMetrics().getNumberOfSuccessfulCalls()).isGreaterThanOrEqualTo(3);
        assertThat(chat().getState()).isEqualTo(CircuitBreaker.State.CLOSED);
    }

    // ------------------------------------------------------------------ //
    // 状态端点
    // ------------------------------------------------------------------ //
    @Test
    void resilienceStateEndpointReflectsState() {
        reads().transitionToOpenState();

        ResponseEntity<String> response = rest.getForEntity("/api/system/resilience", String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        var body = JsonPath.parse(response.getBody());
        assertThat(body.read("$.circuit_breakers.aiServiceReads.state", String.class))
                .isEqualTo("OPEN");
        assertThat(body.read("$.circuit_breakers.aiServiceChat.state", String.class))
                .isEqualTo("CLOSED");
        assertThat(body.read("$.bulkhead.chat_turn.max", Integer.class)).isEqualTo(1);
    }
}
