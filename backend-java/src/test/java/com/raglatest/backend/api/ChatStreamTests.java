package com.raglatest.backend.api;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.client.ResponseDefinitionBuilder;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.store.ConversationSummaryRepository;
import com.raglatest.backend.internal.AiServiceClient;
import io.github.resilience4j.circuitbreaker.CircuitBreakerRegistry;
import com.raglatest.backend.store.MessageRepository;
import com.raglatest.backend.store.OperationRepository;
import com.raglatest.backend.store.OperationRepository.OperationRow;
import com.raglatest.backend.store.TraceRepository;
import com.raglatest.backend.store.WorkspaceRepository;
import com.raglatest.backend.api.dto.Dtos.MessageRead;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.web.server.context.WebServerApplicationContext;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.ClientHttpResponse;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.web.client.ResponseErrorHandler;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.DefaultUriBuilderFactory;

/**
 * M2 聊天链路（对标 python test_api_chat_stream.py 的关键用例）：
 * WireMock 回放 ai-service 的 SSE 帧序列（proposal 拦截落库、persist 权威落库、
 * done 合成、超时/断流的兜底持久化），全程无模型调用。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ChatStreamTests {

    static WireMockServer aiService;

    /** 类加载即创建：@DynamicPropertySource 先于 JUnit 的静态 @TempDir 注入执行。 */
    static final Path dataDir;

    static {
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-chat-tests");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(
                () -> WorkspaceController.deleteDirectoryTree(dataDir)));
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
    WorkspaceRepository workspaces;
    @Autowired
    MessageRepository messages;
    @Autowired
    ConversationSummaryRepository summaries;
    @Autowired
    OperationRepository operations;
    @Autowired
    TraceRepository traces;
    @Autowired
    CircuitBreakerRegistry circuitBreakers;
    @Autowired
    javax.sql.DataSource dataSource;
    @Autowired
    WebServerApplicationContext serverContext;

    RestTemplate rest;

    @BeforeAll
    void setUpRestClient() {
        // Boot 4.1 的 resttestclient 自动配置类内省异常；自建客户端 + 永不判错。
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
    void resetStubs() {
        aiService.resetAll();
        // 熔断实例状态在同一 Spring 上下文内共享：复位以免上游失败用例影响后续用例。
        circuitBreakers.circuitBreaker(AiServiceClient.READS_CIRCUIT).reset();
        circuitBreakers.circuitBreaker(AiServiceClient.CHAT_CIRCUIT).reset();
    }

    // ------------------------------------------------------------------ //
    // helpers
    // ------------------------------------------------------------------ //
    private String createWorkspace(String name) {
        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces", json("{\"name\": \"" + name + "\"}"), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(201);
        return com.jayway.jsonpath.JsonPath.read(response.getBody(), "$.id");
    }

    /** 读取整个 SSE 响应体（WireMock 一次性回放，整读即得全部帧）。 */
    private String streamTurn(String workspaceId, String message) {
        return rest.execute("/api/workspaces/{ws}/chat/stream",
                HttpMethod.POST, request -> {
                    request.getHeaders().setContentType(MediaType.APPLICATION_JSON);
                    request.getBody().write(("{\"message\": \"" + message + "\"}")
                            .getBytes(java.nio.charset.StandardCharsets.UTF_8));
                }, response -> new String(
                        response.getBody().readAllBytes(), java.nio.charset.StandardCharsets.UTF_8),
                workspaceId);
    }

    /** raw SSE body → (event, payload) 列表（与 python 侧测试同款解析）。 */
    private static List<Object[]> parseFrames(String body) {
        java.util.ArrayList<Object[]> frames = new java.util.ArrayList<>();
        for (String block : body.split("\r?\n\r?\n")) {
            String event = null;
            StringBuilder data = new StringBuilder();
            for (String line : block.split("\r?\n")) {
                if (line.startsWith("event:")) {
                    event = line.substring(6).trim();
                } else if (line.startsWith("data:")) {
                    data.append(line.substring(5).trim());
                }
            }
            if (event != null && !data.isEmpty()) {
                frames.add(new Object[]{event, com.jayway.jsonpath.JsonPath.parse(data.toString())});
            }
        }
        return frames;
    }

    private static ResponseDefinitionBuilder sse(String body) {
        return com.github.tomakehurst.wiremock.client.WireMock.ok()
                .withHeader("Content-Type", "text/event-stream")
                .withBody(body);
    }

    private static HttpEntity<String> json(String body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return new HttpEntity<>(body, headers);
    }

    private static final String PROPOSAL_FRAME = """
            {"type":"proposal","operation_id":null,"tool":"update_cells","summary":"修改 销售表.xlsx 工作表「销售」中的单元格：B2","path":"销售表.xlsx","diff":[{"cell":"B2","before":1000,"after":1500}],"arguments":{"path":"销售表.xlsx","sheet_name":"销售","updates":[{"cell":"B2","value":1500}]}}
            """;

    private static final String PERSIST_FRAME = """
            {"type":"persist","status":"ok","content":"已生成修改提案，等待你确认。","citations":[{"file":"销售表.xlsx","location":"销售!第2行"}],"proposals":[{"operation_id":null,"tool":"update_cells","summary":"修改 销售表.xlsx 工作表「销售」中的单元格：B2","path":"销售表.xlsx","diff":[{"cell":"B2","before":1000,"after":1500}],"arguments":{"sheet_name":"销售"}}],"trace_nodes":[{"node":"agent","input":{},"output":{"text_chars":10},"error":null,"duration_ms":5}],"summary":{"text":"旧摘要+新内容","covered_count":8}}
            """;

    // ------------------------------------------------------------------ //
    // 用例
    // ------------------------------------------------------------------ //
    @Test
    void proposalFramesGetIdsAndPersistCreatesRowsAndDone() {
        String ws = createWorkspace("提案轮");
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"))
                .willReturn(sse("""
                        event: token
                        data: {"type":"token","text":"正在修改"}

                        event: proposal
                        data: %s

                        event: persist
                        data: %s
                        """.formatted(PROPOSAL_FRAME.trim(), PERSIST_FRAME.trim()))));

        String body = streamTurn(ws, "把 B2 改成 1500");
        List<Object[]> frames = parseFrames(body);

        // done 由 Java 合成：带 message_id/run_id，且是最后一帧。
        assertThat(frames.get(frames.size() - 1)[0]).isEqualTo("done");
        com.jayway.jsonpath.DocumentContext done =
                (com.jayway.jsonpath.DocumentContext) frames.get(frames.size() - 1)[1];
        String assistantId = done.read("$.message_id", String.class);
        assertThat(assistantId).isNotBlank();
        assertThat(done.read("$.run_id", String.class)).isNotBlank();
        assertThat(done.read("$.content", String.class)).contains("修改提案");

        // proposal 帧被拦截回填：前端拿到的 operation_id 非空，与 operations 行一致。
        com.jayway.jsonpath.DocumentContext proposal =
                (com.jayway.jsonpath.DocumentContext) frames.stream()
                        .filter(f -> f[0].equals("proposal")).findFirst().orElseThrow()[1];
        String forwardedOperationId = proposal.read("$.operation_id", String.class);
        assertThat(forwardedOperationId).isNotBlank();

        List<OperationRow> rows = operations.listByWorkspace(ws);
        assertThat(rows).hasSize(1);
        assertThat(rows.get(0).id()).isEqualTo(forwardedOperationId);
        assertThat(rows.get(0).status()).isEqualTo("proposed");
        assertThat(rows.get(0).toolName()).isEqualTo("update_cells");
        assertThat(rows.get(0).arguments().path("updates").get(0).path("cell").asString())
                .isEqualTo("B2");
        assertThat(rows.get(0).arguments().has("expected_digest")).isFalse();

        // assistant 行：tool_calls 回填真实 operation id（旧版消息契约无 arguments）。
        List<MessageRead> stored = messages.recent(ws, 10);
        assertThat(stored).hasSize(2);
        MessageRead assistant = stored.get(1);
        assertThat(assistant.role()).isEqualTo("assistant");
        assertThat(assistant.id()).isEqualTo(assistantId);
        var toolCall = assistant.toolCalls().get(0);
        assertThat(toolCall.path("operation_id").asString()).isEqualTo(forwardedOperationId);
        assertThat(toolCall.has("arguments")).isFalse();
        assertThat(assistant.citations().get(0).path("file").asString()).isEqualTo("销售表.xlsx");

        // trace 节点与摘要书签随 persist 落库。
        assertThat(traces.runDetail(ws, done.read("$.run_id", String.class)).orElseThrow())
                .hasSize(1);
        assertThat(summaries.find(ws).orElseThrow().coveredCount()).isEqualTo(8);

        // 发给 ai-service 的请求：全量消息 + 摘要书签 + tracked 文件清单。
        aiService.verify(com.github.tomakehurst.wiremock.client.WireMock
                .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .matchingJsonPath("$.message",
                                com.github.tomakehurst.wiremock.client.WireMock
                                        .equalTo("把 B2 改成 1500"))));
    }

    @Test
    void persistWithoutProposalsCreatesAssistantRowAndDone() {
        String ws = createWorkspace("纯文本轮");
        messages.insert(ws, "user", "上一轮问题", null, null);
        messages.insert(ws, "assistant", "上一轮回答", null, null);

        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"))
                .willReturn(sse("""
                        event: token
                        data: {"type":"token","text":"表格里 A型 的销售额是 1000。"}

                        event: followups
                        data: {"type":"followups","items":["A型卖了多少？"]}

                        event: persist
                        data: {"type":"persist","status":"ok","content":"表格里 A型 的销售额是 1000。","citations":[],"proposals":[],"trace_nodes":[],"summary":null}
                        """)));

        String body = streamTurn(ws, "A型 卖了多少？");
        List<Object[]> frames = parseFrames(body);
        List<String> events = frames.stream().map(f -> (String) f[0]).toList();
        assertThat(events).containsExactly("token", "followups", "done");

        // 无新摘要时不写 conversation_summaries。
        assertThat(summaries.find(ws)).isEmpty();
        List<MessageRead> stored = messages.recent(ws, 10);
        assertThat(stored).hasSize(4); // 两条历史 + user + assistant
        assertThat(stored.get(3).role()).isEqualTo("assistant");
        assertThat(stored.get(3).content()).isEqualTo("表格里 A型 的销售额是 1000。");
    }

    @Test
    void summaryBookmarkTravelsWithTheRequest() {
        String ws = createWorkspace("摘要书签");
        summaries.upsert(ws, "旧摘要", 2);
        for (int i = 0; i < 3; i++) {
            messages.insert(ws, i % 2 == 0 ? "user" : "assistant", "第" + i + "条", null, null);
        }

        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"))
                .willReturn(sse("""
                        event: persist
                        data: {"type":"persist","status":"ok","content":"好","citations":[],"proposals":[],"trace_nodes":[],"summary":null}
                        """)));

        streamTurn(ws, "继续");
        var wireMock = com.github.tomakehurst.wiremock.client.WireMock
                .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"));
        aiService.verify(wireMock
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .matchingJsonPath("$.summary",
                                com.github.tomakehurst.wiremock.client.WireMock
                                        .equalTo("旧摘要"))));
        aiService.verify(wireMock
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .matchingJsonPath("$.covered_count",
                                com.github.tomakehurst.wiremock.client.WireMock
                                        .equalTo("2"))));
        aiService.verify(wireMock
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .matchingJsonPath("$.total_messages",
                                com.github.tomakehurst.wiremock.client.WireMock
                                        .equalTo("3"))));
    }

    @Test
    void timeoutPersistStoresPartialAnswerWithNote() {
        String ws = createWorkspace("超时轮");
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"))
                .willReturn(sse("""
                        event: token
                        data: {"type":"token","text":"写到一半"}

                        event: notice
                        data: {"type":"notice","message":"本轮生成超时，已中断并保留已生成的部分。"}

                        event: persist
                        data: {"type":"persist","status":"timeout","content":"写到一半\\n\\n（本轮生成超时已中断，以上为已生成的部分。）","citations":[],"proposals":[],"trace_nodes":[],"summary":null}
                        """)));

        String body = streamTurn(ws, "慢慢写");
        List<Object[]> frames = parseFrames(body);
        List<String> events = frames.stream().map(f -> (String) f[0]).toList();
        assertThat(events).containsExactly("token", "notice", "done");

        com.jayway.jsonpath.DocumentContext done =
                (com.jayway.jsonpath.DocumentContext) frames.get(2)[1];
        assertThat(done.read("$.content", String.class)).contains("超时已中断");

        List<MessageRead> stored = messages.recent(ws, 10);
        assertThat(stored.get(1).content()).contains("超时已中断");
    }

    @Test
    void upstreamFailurePersistsThePartialAnswerWithStopNote() {
        String ws = createWorkspace("断流轮");
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/chat/stream"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.ok()
                        .withHeader("Content-Type", "text/event-stream")
                        .withFault(com.github.tomakehurst.wiremock.http.Fault
                                .CONNECTION_RESET_BY_PEER)));

        String body = streamTurn(ws, "随便说点什么");
        List<Object[]> frames = parseFrames(body);
        // 上游断流：Java 兜底合成 done（部分答案为空 → 占位文案 + 停止注记）。
        assertThat(frames.get(frames.size() - 1)[0]).isEqualTo("done");
        com.jayway.jsonpath.DocumentContext done =
                (com.jayway.jsonpath.DocumentContext) frames.get(frames.size() - 1)[1];
        assertThat(done.read("$.content", String.class))
                .contains("已停止生成")
                .contains("模型这次没有返回内容");

        List<MessageRead> stored = messages.recent(ws, 10);
        assertThat(stored).hasSize(2);
        assertThat(stored.get(1).role()).isEqualTo("assistant");
    }

    // ------------------------------------------------------------------ //
    // guards
    // ------------------------------------------------------------------ //
    @Test
    void chatStreamGuards() {
        // 未知工作区：404，不触达 ai-service。
        ResponseEntity<String> unknown = rest.postForEntity(
                "/api/workspaces/none-such/chat/stream",
                json("{\"message\": \"在吗\"}"), String.class);
        assertThat(unknown.getStatusCode().value()).isEqualTo(404);

        // 空消息：400，同样不进编排。
        String ws = createWorkspace("守卫");
        ResponseEntity<String> blank = rest.postForEntity(
                "/api/workspaces/{ws}/chat/stream",
                json("{\"message\": \"   \"}"), String.class, ws);
        assertThat(blank.getStatusCode().value()).isEqualTo(400);
        assertThat(Files.exists(dataDir.resolve("workspaces").resolve(ws)));
    }

    // ------------------------------------------------------------------ //
    // /internal/retrieval-corpus
    // ------------------------------------------------------------------ //
    @Test
    void retrievalCorpusExposesChunksAndFileMap() {
        String ws = createWorkspace("语料回读");
        // M1 只落盘不建索引：直接铺 file 行 + parent/child 两行 chunk。
        org.springframework.jdbc.core.JdbcTemplate template =
                new org.springframework.jdbc.core.JdbcTemplate(dataSource);
        String fileId = WorkspaceRepository.newId();
        String chunkChild = WorkspaceRepository.newId();
        String chunkParent = WorkspaceRepository.newId();
        java.sql.Timestamp now = java.sql.Timestamp.from(java.time.Instant.now());
        template.update("""
                INSERT INTO document_files
                    (id, workspace_id, rel_path, kind, size_bytes, checksum, status, chunk_count, created_at)
                VALUES (?, ?, ?, 'excel', 0, '', 'ready', 2, ?)
                """, fileId, ws, "销售表.xlsx", now);
        String chunkInsert = """
                INSERT INTO chunks
                    (id, workspace_id, file_id, parent_id, level, ordinal, text, location, meta, token_counts, token_length, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """;
        template.update(chunkInsert, chunkParent, ws, fileId, null, "parent", 0,
                "表头：产品,销售额", "销售!第1-2行", "{}", "{}", 0, now);
        template.update(chunkInsert, chunkChild, ws, fileId, chunkParent, "child", 1,
                "A型,1000", "销售!第2行", "{\"kind\":\"excel\"}", "{\"total\":4}", 4, now);

        ResponseEntity<String> response = rest.getForEntity(
                "/internal/workspaces/{ws}/retrieval-corpus", String.class, ws);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        com.jayway.jsonpath.DocumentContext body =
                com.jayway.jsonpath.JsonPath.parse(response.getBody());
        assertThat((String) body.read("$.files." + fileId)).isEqualTo("销售表.xlsx");
        assertThat(body.read("$.chunks.length()", Integer.class)).isEqualTo(2);
        assertThat(body.read("$.chunks[0].level", String.class)).isEqualTo("parent");
        assertThat(body.read("$.chunks[1].parent_id", String.class)).isEqualTo(chunkParent);
        assertThat((String) body.read("$.chunks[1].meta.kind")).isEqualTo("excel");
        assertThat(body.read("$.chunks[1].token_length", Integer.class)).isEqualTo(4);

        // 未知工作区：404。
        assertThat(rest.getForEntity("/internal/workspaces/nope/retrieval-corpus",
                String.class).getStatusCode().value()).isEqualTo(404);
    }
}
