package com.raglatest.backend.it;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.store.WorkspaceRepository;
import java.nio.file.Path;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.springframework.amqp.rabbit.core.RabbitAdmin;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.web.server.context.WebServerApplicationContext;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.util.MultiValueMap;
import org.springframework.web.client.ResponseErrorHandler;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.DefaultUriBuilderFactory;
import org.springframework.core.io.ByteArrayResource;
import org.testcontainers.rabbitmq.RabbitMQContainer;
import org.testcontainers.utility.DockerImageName;

/**
 * 真实 RabbitMQ 端到端（Testcontainers，命名 *IT 由 failsafe 跑，不进 mvnw test）：
 * 上传入队 → 声明拓扑 → 消费者攒批调 /v1/index-batch（WireMock 桩）→ 落库标 indexed；
 * 毒消息经 default-requeue-rejected=false 路由进 DLQ 停车场。
 * 迁移自 Spring AMQP 官方文档的 DLQ 拓扑验证方式 + Testcontainers rabbitmq 模块示例。
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ReindexFlowIT {

    static final RabbitMQContainer rabbitmq = new RabbitMQContainer(
            DockerImageName.parse("rabbitmq:4-alpine"));

    static WireMockServer aiService;

    static final Path dataDir;

    static {
        rabbitmq.start();
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-reindex-it");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            rabbitmq.stop();
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
        // 推翻 test application.yml 的 mock 默认：真声明拓扑、真启动消费者。
        registry.add("spring.rabbitmq.host", rabbitmq::getHost);
        registry.add("spring.rabbitmq.port", () -> String.valueOf(rabbitmq.getMappedPort(5672)));
        registry.add("spring.rabbitmq.dynamic", () -> "true");
        registry.add("spring.rabbitmq.listener.simple.auto-startup", () -> "true");
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
    RabbitAdmin amqpAdmin;
    @Autowired
    WebServerApplicationContext serverContext;
    @Autowired
    org.springframework.amqp.rabbit.core.RabbitTemplate rabbitTemplate;

    RestTemplate rest;

    @org.junit.jupiter.api.BeforeAll
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
        // 批量端点：上传用例只传 1 个文件（攒批后单文件成批），响应为单元素数组
        aiService.stubFor(com.github.tomakehurst.wiremock.client.WireMock
                .post(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/v1/index-batch"))
                .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                        "[{\"status\":\"indexed\",\"chunk_count\":2,\"vector_count\":2,"
                        + "\"error\":null,\"checksum\":\"x\",\"chunks\":["
                        + "{\"id\":\"p1\",\"parent_id\":null,\"level\":\"parent\",\"ordinal\":0,"
                        + "\"text\":\"表头\",\"location\":\"销售!第1行\",\"meta\":{},"
                        + "\"token_counts\":{},\"token_length\":2},"
                        + "{\"id\":\"c1\",\"parent_id\":\"p1\",\"level\":\"child\",\"ordinal\":1,"
                        + "\"text\":\"A型,1000\",\"location\":\"销售!第2行\",\"meta\":{},"
                        + "\"token_counts\":{},\"token_length\":4}]}]")));
    }

    @Test
    void uploadFlowsThroughQueueToIndexedFile() throws Exception {
        var workspace = workspaces.create("队列端到端", null);
        String ws = workspace.id();

        MultiValueMap<String, Object> body = new LinkedMultiValueMap<>();
        body.add("files", new ByteArrayResource("not-really-xlsx".getBytes()) {
            @Override
            public String getFilename() {
                return "销售表.xlsx";
            }
        });
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.MULTIPART_FORM_DATA);
        ResponseEntity<String> upload = rest.postForEntity(
                "/api/workspaces/{ws}/files", new HttpEntity<>(body, headers), String.class, ws);
        assertThat(upload.getStatusCode().value()).isEqualTo(201);
        String initialStatus = com.jayway.jsonpath.JsonPath.read(upload.getBody(), "$[0].status");
        assertThat(initialStatus).isEqualTo("indexing");

        // 真消费者异步处理：轮询文件列表直到 indexed（冷启动 + 消费，留足 60s）。
        long deadline = System.currentTimeMillis() + 60_000;
        String status = "indexing";
        while (System.currentTimeMillis() < deadline) {
            ResponseEntity<String> list = rest.getForEntity(
                    "/api/workspaces/{ws}/files", String.class, ws);
            status = com.jayway.jsonpath.JsonPath.read(list.getBody(), "$[0].status");
            if ("indexed".equals(status)) {
                break;
            }
            Thread.sleep(200);
        }
        assertThat(status).isEqualTo("indexed");
    }

    @Test
    void poisonMessageDeadLettersInsteadOfRequeueLoop() throws Exception {
        amqpAdmin.purgeQueue("rag.indexing.dlq", false);
        // 毒消息（无法反序列化）：消费者抛出 → 不 requeue → DLX → DLQ 停车场
        rabbitTemplate.convertAndSend("rag.indexing", "file.reindex", "not-json{");

        long deadline = System.currentTimeMillis() + 30_000;
        long dlqDepth = 0;
        while (System.currentTimeMillis() < deadline) {
            var info = amqpAdmin.getQueueInfo("rag.indexing.dlq");
            dlqDepth = info != null ? info.getMessageCount() : 0;
            if (dlqDepth >= 1) {
                break;
            }
            Thread.sleep(200);
        }
        assertThat(dlqDepth).isGreaterThanOrEqualTo(1);
        // 主队列不积压：毒消息没有被退回重投
        var main = amqpAdmin.getQueueInfo("rag.indexing.queue");
        assertThat(main == null || main.getMessageCount() == 0).isTrue();
    }
}
