package com.raglatest.backend.api;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.store.MessageRepository;
import com.raglatest.backend.store.TraceRepository;
import com.raglatest.backend.store.TraceRepository.TraceNodeInput;
import com.raglatest.backend.store.WorkspaceRepository;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.web.server.context.WebServerApplicationContext;
import org.springframework.core.io.ByteArrayResource;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.DefaultUriBuilderFactory;

/**
 * CRUD 全链路（对标 python test_api.py / test_api_files.py 的关键用例）：
 * 真实 SQLite（临时目录）+ WireMock 桩掉 ai-service，全程无模型调用。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class WorkspaceApiTests {

    static WireMockServer aiService;

    /** 类加载即创建：@DynamicPropertySource 先于 JUnit 的静态 @TempDir 注入执行。 */
    static final Path dataDir;

    static {
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-api-tests");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(
                () -> com.raglatest.backend.api.WorkspaceController.deleteDirectoryTree(dataDir)));
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
    TraceRepository traces;
    @Autowired
    WebServerApplicationContext serverContext;

    RestTemplate rest;

    @BeforeAll
    void setUpRestClient() {
        // Boot 4.1 的 resttestclient 自动配置类内省异常；这里直接对随机端口自建客户端，
        // 并关闭 4xx 抛错（对齐 TestRestTemplate 的行为，断言走状态码）。
        RestTemplate template = new RestTemplate();
        template.setUriTemplateHandler(new DefaultUriBuilderFactory(
                "http://127.0.0.1:" + serverContext.getWebServer().getPort()));
        template.setErrorHandler(new org.springframework.web.client.ResponseErrorHandler() {
            @Override
            public boolean hasError(org.springframework.http.client.ClientHttpResponse response) {
                // 永不判错：由各用例断言状态码（对齐 TestRestTemplate 行为）
                return false;
            }
        });
        rest = template;
    }

    private static final byte[] XLSX_BYTES = "not-really-xlsx-but-nonempty".getBytes();

    // ------------------------------------------------------------------ //
    // workspaces
    // ------------------------------------------------------------------ //
    @Test
    void createWorkspaceReturns201AndCreatesDirectory() {
        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces", json("{\"name\": \"测试工作区\", \"description\": \"说明\"}"), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(201);
        assertThat(jsonPath(response, "$.name")).isEqualTo("测试工作区");
        assertThat(jsonPath(response, "$.file_count")).isEqualTo(0);
        String id = jsonPath(response, "$.id").toString();
        assertThat(Files.isDirectory(dataDir.resolve("workspaces").resolve(id))).isTrue();
    }

    @Test
    void unknownWorkspaceIs404() {
        assertThat(rest.getForEntity("/api/workspaces/nope", String.class)
                .getStatusCode().value()).isEqualTo(404);
    }

    @Test
    void workspaceDeleteCleansDirectoryAndDropsCollection() {
        String id = createWorkspace("待删除");
        assertThat(rest.exchange("/api/workspaces/{id}", HttpMethod.DELETE, null, String.class, id)
                .getStatusCode().value()).isEqualTo(204);
        assertThat(rest.getForEntity("/api/workspaces/" + id, String.class)
                .getStatusCode().value()).isEqualTo(404);
        assertThat(Files.exists(dataDir.resolve("workspaces").resolve(id))).isFalse();
        aiService.verify(com.github.tomakehurst.wiremock.client.WireMock
                .deleteRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/collections/" + id)));
    }

    @Test
    void blankWorkspaceNameIsRejected() {
        assertThat(rest.postForEntity("/api/workspaces",
                json("{\"name\": \"\"}"), String.class).getStatusCode().value()).isEqualTo(400);
    }

    // ------------------------------------------------------------------ //
    // files
    // ------------------------------------------------------------------ //
    @Test
    void uploadStoresFileAsPendingAndDownloadRoundTrips() {
        String ws = createWorkspace("文件");
        ResponseEntity<String> upload = upload(ws, "销售表.xlsx", XLSX_BYTES);
        assertThat(upload.getStatusCode().value()).isEqualTo(201);
        assertThat(jsonPath(upload, "$[0].rel_path")).isEqualTo("销售表.xlsx");
        assertThat(jsonPath(upload, "$[0].status")).isEqualTo("pending");
        assertThat(jsonPath(upload, "$[0].chunk_count").toString()).isEqualTo("0");

        ResponseEntity<String> list = rest.getForEntity(
                "/api/workspaces/{ws}/files", String.class, ws);
        assertThat(jsonPath(list, "$[0].kind")).isEqualTo("excel");
        assertThat(jsonPath(list, "$[0].size_bytes").toString())
                .isEqualTo(String.valueOf(XLSX_BYTES.length));
        String fileId = jsonPath(list, "$[0].id").toString();

        byte[] downloaded = rest.getForObject(
                "/api/workspaces/{ws}/files/{id}/download", byte[].class, ws, fileId);
        assertThat(downloaded).isEqualTo(XLSX_BYTES);
        assertThat(Files.exists(dataDir.resolve("workspaces").resolve(ws).resolve("销售表.xlsx"))).isTrue();
    }

    @Test
    void uploadSanitizesPathSeparatorsFromFilename() {
        String ws = createWorkspace("净化");
        ResponseEntity<String> upload = upload(ws, "../../evil<script>.xlsx", XLSX_BYTES);
        assertThat(upload.getStatusCode().value()).isEqualTo(201);
        // 基名保留，非法字符替换为下划线
        String relPath = jsonPath(upload, "$[0].rel_path").toString();
        assertThat(relPath).doesNotContain("..").doesNotContain("/").endsWith(".xlsx");
    }

    @Test
    void uploadRejectsUnsupportedSuffixAndEmptyFile() {
        String ws = createWorkspace("非法");
        assertThat(upload(ws, "malware.exe", XLSX_BYTES).getStatusCode().value()).isEqualTo(400);
        assertThat(upload(ws, "empty.xlsx", new byte[0]).getStatusCode().value()).isEqualTo(400);
        assertThat(rest.getForEntity("/api/workspaces/{ws}/files", String.class, ws)
                .getBody()).isEqualTo("[]");
    }

    @Test
    void deleteFileRemovesRecordDiskAndVectorEntries() {
        String ws = createWorkspace("删除文件");
        ResponseEntity<String> upload = upload(ws, "a.xlsx", XLSX_BYTES);
        String fileId = jsonPath(upload, "$[0].file_id").toString();

        assertThat(rest.exchange("/api/workspaces/{ws}/files/{id}", HttpMethod.DELETE,
                null, String.class, ws, fileId).getStatusCode().value()).isEqualTo(204);
        assertThat(rest.exchange("/api/workspaces/{ws}/files/{id}", HttpMethod.DELETE,
                null, String.class, ws, fileId).getStatusCode().value()).isEqualTo(404);
        assertThat(Files.exists(dataDir.resolve("workspaces").resolve(ws).resolve("a.xlsx"))).isFalse();
        aiService.verify(com.github.tomakehurst.wiremock.client.WireMock
                .deleteRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlPathEqualTo("/v1/collections/" + ws + "/vectors")));
    }

    // ------------------------------------------------------------------ //
    // messages / feedback
    // ------------------------------------------------------------------ //
    @Test
    void feedbackUpsertsAndClears() {
        String ws = createWorkspace("反馈");
        String messageId = messages.insert(ws, "assistant", "答案", null, null);

        ResponseEntity<String> up = rest.postForEntity(
                "/api/workspaces/{ws}/messages/{id}/feedback",
                json("{\"feedback\": \"up\"}"), String.class, ws, messageId);
        assertThat(jsonPath(up, "$.feedback")).isEqualTo("up");

        ResponseEntity<String> cleared = rest.postForEntity(
                "/api/workspaces/{ws}/messages/{id}/feedback",
                json("{\"feedback\": \"none\"}"), String.class, ws, messageId);
        assertThat(jsonPath(cleared, "$.feedback")).isNull();

        assertThat(rest.postForEntity("/api/workspaces/{ws}/messages/{id}/feedback",
                        json("{\"feedback\": \"sideways\"}"), String.class, ws, messageId)
                .getStatusCode().value()).isEqualTo(400);
        assertThat(rest.postForEntity("/api/workspaces/{ws}/messages/{id}/feedback",
                        json("{\"feedback\": \"up\"}"), String.class, ws, "missing")
                .getStatusCode().value()).isEqualTo(404);
    }

    @Test
    void messagesListIsChronologicalAndHonorsLimit() {
        String ws = createWorkspace("消息");
        // created_at 毫秒精度：紧邻插入会同毫秒，排序退化到随机 id。隔开插入，
        // 让时间序成为唯一决定因素（本用例钉的就是时间序语义）。
        messages.insert(ws, "user", "第一条", null, null);
        org.assertj.core.api.Assertions
                .assertThatCode(() -> Thread.sleep(5)).doesNotThrowAnyException();
        messages.insert(ws, "assistant", "第二条", null, null);
        org.assertj.core.api.Assertions
                .assertThatCode(() -> Thread.sleep(5)).doesNotThrowAnyException();
        messages.insert(ws, "user", "第三条", null, null);

        ResponseEntity<String> all = rest.getForEntity(
                "/api/workspaces/{ws}/messages?limit=200", String.class, ws);
        List<String> allContents = com.jayway.jsonpath.JsonPath.read(all.getBody(), "$..content");
        assertThat(allContents).containsExactly("第一条", "第二条", "第三条");

        ResponseEntity<String> tail = rest.getForEntity(
                "/api/workspaces/{ws}/messages?limit=2", String.class, ws);
        List<String> tailContents = com.jayway.jsonpath.JsonPath.read(tail.getBody(), "$..content");
        assertThat(tailContents).containsExactly("第二条", "第三条");
    }

    // ------------------------------------------------------------------ //
    // traces
    // ------------------------------------------------------------------ //
    @Test
    void tracesSummarizeRunsAndDetailKeepsInsertionOrder() {
        String ws = createWorkspace("轨迹");
        String runA = WorkspaceRepository.newId();
        String runB = WorkspaceRepository.newId();
        traces.insertNode(ws, runA, new TraceNodeInput("agent", 10, null, null, null));
        traces.insertNode(ws, runA, new TraceNodeInput("tools", 5, null, null, "boom"));
        traces.insertNode(ws, runB, new TraceNodeInput("agent", 3, null, null, null));

        ResponseEntity<String> runs = rest.getForEntity(
                "/api/workspaces/{ws}/traces", String.class, ws);
        List<?> summaries = com.jayway.jsonpath.JsonPath.read(runs.getBody(),
                "$[?(@.run_id=='" + runA + "')]");
        assertThat(summaries).hasSize(1);
        List<Integer> nodeCounts = com.jayway.jsonpath.JsonPath.read(runs.getBody(),
                "$[?(@.run_id=='" + runA + "')].node_count");
        assertThat(nodeCounts.get(0)).isEqualTo(2);
        List<Boolean> hasErrors = com.jayway.jsonpath.JsonPath.read(runs.getBody(),
                "$[?(@.run_id=='" + runA + "')].has_error");
        assertThat(hasErrors.get(0)).isEqualTo(true);
        List<Boolean> cleanRun = com.jayway.jsonpath.JsonPath.read(runs.getBody(),
                "$[?(@.run_id=='" + runB + "')].has_error");
        assertThat(cleanRun.get(0)).isEqualTo(false);

        ResponseEntity<String> detail = rest.getForEntity(
                "/api/workspaces/{ws}/traces/{run}", String.class, ws, runA);
        List<String> nodesInOrder = com.jayway.jsonpath.JsonPath.read(detail.getBody(), "$.nodes[*].node");
        assertThat(nodesInOrder).containsExactly("agent", "tools");

        assertThat(rest.getForEntity("/api/workspaces/{ws}/traces/unknown", String.class, ws)
                .getStatusCode().value()).isEqualTo(404);
    }

    // ------------------------------------------------------------------ //
    // tools / preview（ai-service 透传）
    // ------------------------------------------------------------------ //
    @Test
    void toolsListIsPassedThroughFromAiService() {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .get(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/tools"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        """
                        [{"name": "read_range", "description": "d", "read_only": true,
                          "destructive": false, "requires_approval": false, "schema": {}}]
                        """)));
        ResponseEntity<String> response = rest.getForEntity(
                "/api/workspaces/whatever/tools", String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        List<Boolean> readOnlyFlags = com.jayway.jsonpath.JsonPath.read(
                response.getBody(), "$[?(@.name=='read_range')].read_only");
        assertThat(readOnlyFlags).containsExactly(true);
    }

    @Test
    void previewProxiesAiServiceAndValidatesWorkspace() {
        String ws = createWorkspace("预览");
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .get(com.github.tomakehurst.wiremock.client.WireMock.urlPathEqualTo(
                        "/v1/workspaces/" + ws + "/preview"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "{\"kind\": \"excel\", \"rows\": [[\"产品\", \"销售额\"]]}")));
        ResponseEntity<String> response = rest.getForEntity(
                "/api/workspaces/{ws}/preview?file=销售表.xlsx&location=销售!第1行",
                String.class, ws);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(jsonPath(response, "$.kind")).isEqualTo("excel");

        // 工作区不存在：不透传到 ai-service，直接 404。
        assertThat(rest.getForEntity(
                "/api/workspaces/none/preview?file=a.xlsx&location=x", String.class)
                .getStatusCode().value()).isEqualTo(404);
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

    private ResponseEntity<String> upload(String workspaceId, String filename, byte[] content) {
        MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
        body.add("files", new ByteArrayResource(content) {
            @Override
            public String getFilename() {
                return filename;
            }
        });
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        return rest.postForEntity("/api/workspaces/{ws}/files",
                new HttpEntity<>(body, headers), String.class, workspaceId);
    }

    private static HttpEntity<String> json(String body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return new HttpEntity<>(body, headers);
    }

    private static Object jsonPath(ResponseEntity<String> response, String path) {
        return com.jayway.jsonpath.JsonPath.read(response.getBody(), path);
    }
}
