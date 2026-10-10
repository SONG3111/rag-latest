package com.raglatest.backend.health;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.config.RagProperties;
import com.raglatest.backend.config.ResilienceConfig;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.identity.UserRepository;
import com.raglatest.backend.security.InternalToken;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import static org.mockito.Mockito.when;

/**
 * /health 聚合行为：ai-service 可达时透传 mcp_started/tools 等字段。
 * 契约与原 Python 版保持一致，前端与 docker healthcheck 依赖它。
 */
@WebMvcTest(HealthController.class)
// InternalToken 是 AiServiceClient 的构造依赖（内部调用的凭据头）。
@Import({AiServiceClient.class, ResilienceConfig.class, InternalToken.class})
// 切片只测 /health 的聚合语义，不测鉴权（鉴权由 ApiAuthorizationTests 专门覆盖）。
@AutoConfigureMockMvc(addFilters = false)
@EnableConfigurationProperties(RagProperties.class)
class HealthControllerUpTests {

    static WireMockServer aiService = new WireMockServer(
            WireMockConfiguration.wireMockConfig().dynamicPort());

    /** 切片上下文没有 Redis/RabbitMQ 连接工厂：基础设施探针以 mock 提供。 */
    @MockitoBean
    InfraHealth infra;

    /**
     * AccountFreshnessFilter 是 Filter 类型的 @Component，会被 @WebMvcTest 的切片
     * 一起扫进来（@WebMvcTest 包含 Filter 组件），它依赖 UserRepository —— 切片
     * 里没有数据层，故以 mock 提供；本类不测鉴权。
     */
    @MockitoBean
    UserRepository users;

    @DynamicPropertySource
    static void aiServiceUrl(DynamicPropertyRegistry registry) {
        registry.add("rag.ai-service.base-url", () -> "http://127.0.0.1:" + aiService.port());
    }

    @BeforeAll
    static void startStub() {
        aiService.start();
        aiService.stubFor(
                com.github.tomakehurst.wiremock.client.WireMock
                        .get(com.github.tomakehurst.wiremock.client.WireMock.urlEqualTo("/health"))
                        .willReturn(com.github.tomakehurst.wiremock.client.WireMock.okJson(
                                """
                                {"status": "ok", "mcp_started": true,
                                 "mcp_error": null, "tools": 22}
                                """)));
    }

    @AfterAll
    static void stopStub() {
        aiService.stop();
    }

    @Autowired
    MockMvc mockMvc;

    @Test
    void passesThroughAiServiceHealth() throws Exception {
        when(infra.redisUp()).thenReturn(true);
        when(infra.queueUp()).thenReturn(true);
        mockMvc.perform(get("/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ok"))
                .andExpect(jsonPath("$.ai_service").value("up"))
                .andExpect(jsonPath("$.mcp_started").value(true))
                .andExpect(jsonPath("$.tools").value(22))
                .andExpect(jsonPath("$.redis").value("up"))
                .andExpect(jsonPath("$.queue").value("up"));
    }
}
