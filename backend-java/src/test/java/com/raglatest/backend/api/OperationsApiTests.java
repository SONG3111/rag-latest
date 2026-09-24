package com.raglatest.backend.api;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.service.FileStorage;
import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.OperationRepository;
import com.raglatest.backend.store.OperationRepository.OperationRow;
import com.raglatest.backend.store.WorkspaceRepository;
import java.nio.file.Files;
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
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.web.client.ResponseErrorHandler;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.DefaultUriBuilderFactory;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 审批闭环（M3）：apply 备份并真正执行写工具、刷新同文件后续提案的乐观锁；reject 不碰文件；
 * revert 用备份还原。WireMock 桩掉 ai-service 的 /v1/tools/call，全程无真实模型/文档写。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OperationsApiTests {

    static WireMockServer aiService;

    static final Path dataDir;

    static {
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-ops-tests");
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
    OperationRepository operations;
    @Autowired
    DocumentFileRepository files;
    @Autowired
    FileStorage storage;
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
            public boolean hasError(org.springframework.http.client.ClientHttpResponse response) {
                return false;
            }
        });
        rest = template;
    }

    @BeforeEach
    void resetStubs() {
        aiService.resetAll();
    }

    private String createWorkspace(String name) {
        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces", json("{\"name\": \"" + name + "\"}"), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(201);
        return com.jayway.jsonpath.JsonPath.read(response.getBody(), "$.id");
    }

    private Path writeFile(String workspaceId, String relPath, String content) throws Exception {
        Path dir = storage.workspaceDir(workspaceId);
        Files.createDirectories(dir);
        Path file = dir.resolve(relPath);
        Files.write(file, content.getBytes());
        return file;
    }

    /** 直接落一条 proposed 提案（模拟聊天链路拦截 proposal 帧后 Java 建的行）。 */
    private String seedOperation(String workspaceId, String relPath, String digest) {
        ObjectNode args = mapper.createObjectNode();
        args.put("path", relPath);
        args.put("sheet_name", "S");
        args.putArray("updates").addObject().put("cell", "A2").put("value", 2);
        if (digest != null) {
            args.put("expected_digest", digest);
        }
        ArrayNode diff = mapper.createArrayNode();
        diff.addObject().put("cell", "A2").put("before", 1).put("after", 2);
        return operations.insertProposed(workspaceId, "update_cells", relPath, args, diff, "改 A2");
    }

    /** 直接落一条任意工具的 proposed 提案（rebase/自动驳回场景的种子）。 */
    private String seedOperation(String workspaceId, String relPath, String tool,
                                 String argsJson, String summary) {
        return operations.insertProposed(workspaceId, tool, relPath,
                (ObjectNode) mapper.readTree(argsJson), null, summary);
    }

    private void stubToolOk() {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/tools/call"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "{\"ok\":true,\"data\":{\"digest\":\"digest-new\","
                        + "\"changes\":[{\"cell\":\"A2\",\"before\":1,\"after\":2}]}}")));
    }

    /** 只命中指定工具名的 /v1/tools/call 桩（同测试里可与 read_range 桉共存）。 */
    private void stubToolOk(String tool) {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/tools/call"))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .containing("\"tool\":\"" + tool + "\""))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "{\"ok\":true,\"data\":{\"digest\":\"digest-new\",\"changes\":[]}}")));
    }

    /** read_range 回读当前值：diff 重建用（返回单格 [[before]]）。 */
    private void stubReadRange(int before) {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/tools/call"))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .containing("\"tool\":\"read_range\""))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "{\"ok\":true,\"data\":{\"values\":[[" + before + "]]}}")));
    }

    private void stubIndexOk() {
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/index"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "{\"status\":\"indexed\",\"chunk_count\":0,\"vector_count\":0,"
                        + "\"error\":null,\"checksum\":\"x\",\"chunks\":[]}")));
    }

    /** 后台 reindex 是异步的：轮询 WireMock 直到 /v1/index 达到期望次数（最多 10s）。 */
    private void awaitIndexRequests(int expectedCount) throws InterruptedException {
        long deadline = System.currentTimeMillis() + 10_000;
        while (System.currentTimeMillis() < deadline) {
            if (aiService.findAll(com.github.tomakehurst.wiremock.client.WireMock
                    .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                            .urlEqualTo("/v1/index"))).size() >= expectedCount) {
                return;
            }
            Thread.sleep(50);
        }
    }

    private static HttpEntity<String> json(String body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return new HttpEntity<>(body, headers);
    }

    private static Object jsonPath(ResponseEntity<String> response, String path) {
        return com.jayway.jsonpath.JsonPath.read(response.getBody(), path);
    }

    @Test
    void applyBacksUpExecutesToolAndMarksApplied() throws Exception {
        String ws = createWorkspace("审批");
        writeFile(ws, "a.xlsx", "ORIGINAL");
        String opId = seedOperation(ws, "a.xlsx", "digest-old");
        stubToolOk();

        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, opId);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(jsonPath(response, "$.status")).isEqualTo("applied");
        assertThat(jsonPath(response, "$.backup_path").toString()).isNotBlank();

        // 工具收到的是沙箱相对路径 <workspace_id>/a.xlsx
        aiService.verify(com.github.tomakehurst.wiremock.client.WireMock
                .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/tools/call"))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .containing(ws + "/a.xlsx")));
    }

    @Test
    void applyRefreshesChainedDigestOfSameFile() throws Exception {
        String ws = createWorkspace("链式");
        writeFile(ws, "a.xlsx", "ORIGINAL");
        String op1 = seedOperation(ws, "a.xlsx", "digest-old");
        String op2 = seedOperation(ws, "a.xlsx", "digest-old");
        stubToolOk();

        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, op1);

        OperationRow other = operations.find(ws, op2).orElseThrow();
        assertThat(other.arguments().path("expected_digest").asString()).isEqualTo("digest-new");
    }

    @Test
    void rejectLeavesFileUntouched() throws Exception {
        String ws = createWorkspace("拒绝");
        Path file = writeFile(ws, "a.xlsx", "ORIGINAL");
        String opId = seedOperation(ws, "a.xlsx", "digest-old");

        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/reject", null, String.class, ws, opId);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(jsonPath(response, "$.status")).isEqualTo("rejected");
        assertThat(Files.readAllBytes(file)).isEqualTo("ORIGINAL".getBytes());
        aiService.verify(0, com.github.tomakehurst.wiremock.client.WireMock
                .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/tools/call")));
    }

    @Test
    void revertRestoresBackup() throws Exception {
        String ws = createWorkspace("还原");
        Path file = writeFile(ws, "a.xlsx", "ORIGINAL");
        String opId = seedOperation(ws, "a.xlsx", "digest-old");
        stubToolOk();
        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, opId);

        Files.write(file, "MODIFIED".getBytes()); // 模拟真实的写入
        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/revert", null, String.class, ws, opId);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(Files.readAllBytes(file)).isEqualTo("ORIGINAL".getBytes());
    }

    // ---------------- 坐标重排（对标 test_proposal_rebase.py 的核心场景） ---------------- //

    @Test
    void applyDeleteRebasesPendingFollowUps() throws Exception {
        String ws = createWorkspace("重排");
        writeFile(ws, "行.xlsx", "X");
        String deleteOp = seedOperation(ws, "行.xlsx", "delete_rows",
                "{\"path\":\"行.xlsx\",\"sheet_name\":\"S\",\"start_row\":2,\"count\":1}",
                "删第2行");
        String editOp = seedOperation(ws, "行.xlsx", "update_cells",
                "{\"path\":\"行.xlsx\",\"sheet_name\":\"S\"," 
                        + "\"updates\":[{\"cell\":\"B5\",\"value\":41}]}",
                "改 B5");
        stubToolOk("delete_rows");
        stubReadRange(40);

        ResponseEntity<String> response = rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, deleteOp);
        assertThat(response.getStatusCode().value()).isEqualTo(200);

        // 编辑提案的坐标跟行上移（5 → 4），diff 重读的是现在 B4 里的值，摘要同步改写
        OperationRow edit = operations.find(ws, editOp).orElseThrow();
        assertThat(edit.arguments().path("updates").get(0).path("cell").asString())
                .isEqualTo("B4");
        assertThat(edit.diff().get(0).path("cell").asString()).isEqualTo("B4");
        assertThat(edit.diff().get(0).path("before").asInt()).isEqualTo(40);
        assertThat(edit.summary()).contains("B4");

        // read_range 收到的是重排后的坐标与沙箱相对路径
        aiService.verify(com.github.tomakehurst.wiremock.client.WireMock
                .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/tools/call"))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .containing("\"start_cell\":\"B4\""))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .containing(ws + "/行.xlsx")));
    }

    @Test
    void applyRejectsProposalWhoseTargetRowWasDeleted() throws Exception {
        String ws = createWorkspace("目标已删");
        writeFile(ws, "行.xlsx", "X");
        String deleteOp = seedOperation(ws, "行.xlsx", "delete_rows",
                "{\"path\":\"行.xlsx\",\"sheet_name\":\"S\",\"start_row\":2,\"count\":1}",
                "删第2行");
        String staleEdit = seedOperation(ws, "行.xlsx", "update_cells",
                "{\"path\":\"行.xlsx\",\"sheet_name\":\"S\"," 
                        + "\"updates\":[{\"cell\":\"B2\",\"value\":11}]}",
                "改 B2");
        stubToolOk("delete_rows");

        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, deleteOp);

        // 往 B2 写 11 会命中另一行：提案被关闭而不是静默改写别的数据
        OperationRow stale = operations.find(ws, staleEdit).orElseThrow();
        assertThat(stale.status()).isEqualTo("rejected");
        assertThat(stale.error()).contains("自动驳回");
    }

    @Test
    void rebaseRespectsOtherSheetsOnTheSameFile() throws Exception {
        String ws = createWorkspace("多表隔离");
        writeFile(ws, "多表.xlsx", "X");
        String deleteOp = seedOperation(ws, "多表.xlsx", "delete_rows",
                "{\"path\":\"多表.xlsx\",\"sheet_name\":\"S\",\"start_row\":2,\"count\":1}",
                "删第2行");
        String otherSheet = seedOperation(ws, "多表.xlsx", "update_cells",
                "{\"path\":\"多表.xlsx\",\"sheet_name\":\"T\"," 
                        + "\"updates\":[{\"cell\":\"B9\",\"value\":456}]}",
                "改 B9");
        stubToolOk("delete_rows");

        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, deleteOp);

        OperationRow untouched = operations.find(ws, otherSheet).orElseThrow();
        assertThat(untouched.arguments().path("updates").get(0).path("cell").asString())
                .isEqualTo("B9");
        assertThat(untouched.status()).isEqualTo("proposed");
    }

    // ---------------- 写后后台重建索引 ---------------- //

    @Test
    void applyAndRevertTriggerBackgroundReindex() throws Exception {
        String ws = createWorkspace("写后重建");
        writeFile(ws, "a.xlsx", "ORIGINAL");
        FileRead file = files.insertPending(ws, "a.xlsx", "xlsx", 8L, "sum");
        String opId = seedOperation(ws, "a.xlsx", "digest-old");
        stubToolOk();
        stubIndexOk();

        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/apply", null, String.class, ws, opId);
        awaitIndexRequests(1);

        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/revert", null, String.class, ws, opId);
        awaitIndexRequests(2);

        aiService.verify(2, com.github.tomakehurst.wiremock.client.WireMock
                .postRequestedFor(com.github.tomakehurst.wiremock.client.WireMock
                        .urlEqualTo("/v1/index"))
                .withRequestBody(com.github.tomakehurst.wiremock.client.WireMock
                        .containing(file.id())));
    }

    // ---------------- 列表契约（对齐 python OperationRead） ---------------- //

    @Test
    void listOperationsSupportsStatusFilterAndResponseContract() throws Exception {
        String ws = createWorkspace("列表契约");
        writeFile(ws, "a.xlsx", "X");
        String kept = seedOperation(ws, "a.xlsx", "digest-old");
        String rejected = seedOperation(ws, "a.xlsx", "digest-old");
        rest.postForEntity(
                "/api/workspaces/{ws}/operations/{op}/reject", null, String.class, ws, rejected);

        // Python 版查询参数 status_filter 与本侧 status 都认
        ResponseEntity<String> proposedOnly = rest.getForEntity(
                "/api/workspaces/{ws}/operations?status_filter=proposed", String.class, ws);
        assertThat(proposedOnly.getStatusCode().value()).isEqualTo(200);
        assertThat((int) jsonPath(proposedOnly, "$.length()")).isEqualTo(1);
        assertThat(jsonPath(proposedOnly, "$[0].id")).isEqualTo(kept);

        ResponseEntity<String> viaAlias = rest.getForEntity(
                "/api/workspaces/{ws}/operations?status=proposed", String.class, ws);
        assertThat((int) jsonPath(viaAlias, "$.length()")).isEqualTo(1);

        ResponseEntity<String> history = rest.getForEntity(
                "/api/workspaces/{ws}/operations", String.class, ws);
        assertThat((int) jsonPath(history, "$.length()")).isEqualTo(2);

        // 契约：created_at 必在，arguments 不外泄（full 参数只在 SSE proposal 帧里）
        JsonNode body = mapper.readTree(history.getBody());
        assertThat(body.get(0).hasNonNull("created_at")).isTrue();
        assertThat(body.get(0).has("arguments")).isFalse();
        assertThat(body.get(0).has("resolved_at")).isTrue();
        assertThat(body.get(0).has("diff")).isTrue();
    }
}
