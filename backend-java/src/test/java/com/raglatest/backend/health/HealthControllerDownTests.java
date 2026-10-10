package com.raglatest.backend.health;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.raglatest.backend.config.RagProperties;
import com.raglatest.backend.config.ResilienceConfig;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.identity.UserRepository;
import com.raglatest.backend.security.InternalToken;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.boot.webmvc.test.autoconfigure.WebMvcTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

/** ai-service 不可达时仍返回 200 + 降级字段（与原版 MCP 降级语义一致）。 */
@WebMvcTest(HealthController.class)
@Import({AiServiceClient.class, ResilienceConfig.class, InternalToken.class})
@AutoConfigureMockMvc(addFilters = false)
@EnableConfigurationProperties(RagProperties.class)
@TestPropertySource(properties = "rag.ai-service.base-url=http://127.0.0.1:1")
class HealthControllerDownTests {

    /** 切片上下文没有 Redis/RabbitMQ 连接工厂：基础设施探针以 mock 提供（默认 false）。 */
    @MockitoBean
    InfraHealth infra;

    /** 同上：切片会扫到 Filter 组件，其依赖以 mock 提供。 */
    @MockitoBean
    UserRepository users;

    @Autowired
    MockMvc mockMvc;

    @Test
    void degradesGracefullyWhenAiServiceUnreachable() throws Exception {
        mockMvc.perform(get("/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("ok"))
                .andExpect(jsonPath("$.ai_service").value("down"))
                .andExpect(jsonPath("$.mcp_started").value(false))
                .andExpect(jsonPath("$.tools").value(0))
                .andExpect(jsonPath("$.mcp_error").isNotEmpty())
                .andExpect(jsonPath("$.redis").value("down"))
                .andExpect(jsonPath("$.queue").value("down"));
    }
}
