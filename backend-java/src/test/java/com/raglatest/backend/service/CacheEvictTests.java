package com.raglatest.backend.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.queue.ReindexProducer;
import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.WorkspaceRepository;
import java.nio.file.Path;
import java.sql.Timestamp;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import tools.jackson.databind.JsonNode;

/**
 * 缓存语义（simple cache 驱动，Redis 往返由 RedisCacheIT 覆盖）：
 * 命中不再穿透（corpus 同实例 / tools 只打一次上游），写路径显式失效
 * （重建索引后 corpus 反映新分块）。60s TTL 只是兜底，不在此测。
 */
@SpringBootTest
class CacheEvictTests {

    static WireMockServer aiService;

    static final Path dataDir;

    static {
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-cache-tests");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
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
    }

    @AfterAll
    static void stopStub() {
        if (aiService != null) {
            aiService.stop();
        }
    }

    @Autowired
    CorpusService corpus;
    @Autowired
    IndexingService indexing;
    @Autowired
    AiServiceClient aiServiceClient;
    @Autowired
    WorkspaceRepository workspaces;
    @Autowired
    DocumentFileRepository files;
    @Autowired
    JdbcTemplate jdbc;

    @MockitoBean
    ReindexProducer reindexQueue;

    /** 建工作区 + 登记文件 + 插一行 chunk，返回 workspace id。 */
    private String seedWorkspaceWithChunk() {
        var workspace = workspaces.create("缓存", null);
        String ws = workspace.id();
        var file = files.insertPending(ws, "a.xlsx", "xlsx", 8L, "sum");
        Timestamp now = Timestamp.from(java.time.Instant.now());
        jdbc.update("""
                INSERT INTO chunks
                    (id, workspace_id, file_id, parent_id, level, ordinal, text, location,
                     meta, token_counts, token_length, created_at)
                VALUES ('c1', ?, ?, null, 'child', 0, 'A型,1000', '销售!第2行',
                        '{}', '{}', 4, ?)
                """, ws, file.id(), now);
        return ws;
    }

    @Test
    void corpusCacheHitsUntilReindexEvictsIt() {
        String ws = seedWorkspaceWithChunk();

        JsonNode first = corpus.load(ws);
        assertThat(first.path("chunks").size()).isEqualTo(1);
        // 命中：同一实例（simple cache 存的就是这个引用）
        assertThat(corpus.load(ws)).isSameAs(first);

        // 重建索引（上游返回空分块）：@CacheEvict 必须让下一次读拿到新语料
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/index"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "{\"status\":\"indexed\",\"chunk_count\":0,\"vector_count\":0,"
                        + "\"error\":null,\"checksum\":\"x\",\"chunks\":[]}")));
        String fileId = files.findByRelPath(ws, "a.xlsx").orElseThrow().id();
        indexing.index(ws, fileId);

        assertThat(corpus.load(ws).path("chunks").size()).isZero();
    }

    @Test
    void toolsListIsCachedAcrossCalls() {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .get(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/tools"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson("[]")));

        JsonNode first = aiServiceClient.listTools();
        assertThat(aiServiceClient.listTools()).isSameAs(first);

        aiService.verify(1, com.github.tomakehurst.wiremock.client.WireMock
                .getRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/tools")));
    }
}
