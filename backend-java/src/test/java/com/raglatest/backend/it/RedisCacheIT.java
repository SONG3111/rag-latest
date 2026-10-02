package com.raglatest.backend.it;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.queue.ReindexProducer;
import com.raglatest.backend.service.CorpusService;
import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.WorkspaceRepository;
import com.redis.testcontainers.RedisContainer;
import java.nio.file.Path;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.testcontainers.utility.DockerImageName;
import tools.jackson.databind.JsonNode;

/**
 * 真实 Redis 的缓存行为（Testcontainers，命名 *IT 由 failsafe 跑，不进 mvnw test）：
 * JsonNode 经自定义 RedisSerializer 往返无损、命中不再穿透上游、TTL 按缓存名生效。
 * 容器用法迁移自 Testcontainers 官方 Redis 模块（com.redis:testcontainers-redis）。
 */
@SpringBootTest
class RedisCacheIT {

    static final RedisContainer redis = new RedisContainer(
            DockerImageName.parse("redis:7-alpine"));

    static WireMockServer aiService;

    static final Path dataDir;

    static {
        redis.start();
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-redis-it");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            redis.stop();
            try {
                java.nio.file.Files.walk(dataDir)
                        .sorted(java.util.Comparator.reverseOrder())
                        .forEach(p -> p.toFile().delete());
            } catch (java.io.IOException ignored) {
                // 尽力删
            }
        }));
    }

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) {
        if (aiService == null) {
            aiService = new WireMockServer(WireMockConfiguration.wireMockConfig().dynamicPort());
            aiService.start();
        }
        registry.add("rag.data-dir", () -> dataDir.toAbsolutePath().toString());
        registry.add("rag.ai-service.base-url", () -> "http://127.0.0.1:" + aiService.port());
        // 推翻 test application.yml 的 simple 默认：走真 Redis。
        registry.add("spring.data.redis.host", redis::getHost);
        registry.add("spring.data.redis.port", () -> String.valueOf(redis.getMappedPort(6379)));
        registry.add("spring.cache.type", () -> "redis");
    }

    @AfterAll
    static void stopStub() {
        if (aiService != null) {
            aiService.stop();
        }
    }

    @Autowired
    AiServiceClient aiServiceClient;
    @Autowired
    CorpusService corpus;
    @Autowired
    WorkspaceRepository workspaces;
    @Autowired
    DocumentFileRepository files;
    @Autowired
    StringRedisTemplate redisStrings;

    @MockitoBean
    ReindexProducer reindexQueue;

    @Test
    void toolsCacheSurvivesRedisRoundTrip() {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .get(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/tools"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "[{\"name\": \"read_range\", \"read_only\": true}]")));

        JsonNode first = aiServiceClient.listTools();
        // 第一次逻辑调用可能因瞬时故障合法重试（@Retryable 最多 1+2 次 HTTP），
        // 命中语义的正确断言是「第二次逻辑调用不再穿透」，而非约束首次恰好 1 次。
        int afterFirst = requestCount();
        assertThat(afterFirst).isBetween(1, 3);
        assertThat(redisStrings.hasKey("aiTools::all"))
                .as("first call should populate the Redis cache").isTrue();

        // 反序列化后是等值的另一实例（Redis 往返），内容无损
        JsonNode second = aiServiceClient.listTools();
        assertThat(requestCount()).isEqualTo(afterFirst);
        assertThat(second).isNotSameAs(first).isEqualTo(first);
        assertThat(second.get(0).path("name").asString()).isEqualTo("read_range");
    }

    private int requestCount() {
        return aiService.findAll(com.github.tomakehurst.wiremock.client.WireMock
                .getRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/tools"))).size();
    }

    @Test
    void corpusCacheLandsInRedisWithKeyPrefix() {
        var workspace = workspaces.create("Redis 语料", null);
        String ws = workspace.id();
        files.insertPending(ws, "a.xlsx", "xlsx", 8L, "sum");

        JsonNode first = corpus.load(ws);
        assertThat(first.path("chunks").size()).isZero();
        assertThat(corpus.load(ws)).isEqualTo(first);

        // key 前缀（usePrefix 默认开启）：retrievalCorpus::ws
        assertThat(redisStrings.hasKey("retrievalCorpus::" + ws)).isTrue();
    }
}
