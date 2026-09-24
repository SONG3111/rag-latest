package com.raglatest.backend.health;

import com.raglatest.backend.internal.AiServiceClient;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 对外健康检查：保持原 Python 版 /health 的响应形状
 * （status/mcp_started/mcp_error/tools），另加 ai_service 字段标注 AI 服务可达性。
 * 即使 ai-service 不可达也返回 200 + 降级字段，让 UI 能解释哪里坏了（与原版 MCP 降级语义一致）。
 */
@RestController
public class HealthController {

    private static final Logger log = LoggerFactory.getLogger(HealthController.class);
    private static final JsonNodeFactory JSON = JsonNodeFactory.instance;

    private final AiServiceClient aiService;

    public HealthController(AiServiceClient aiService) {
        this.aiService = aiService;
    }

    @GetMapping("/health")
    public ObjectNode health() {
        ObjectNode body = JSON.objectNode();
        body.put("status", "ok");
        try {
            JsonNode upstream = aiService.getHealth();
            body.put("ai_service", "up");
            body.set("mcp_started", upstream.path("mcp_started"));
            body.set("mcp_error", upstream.path("mcp_error"));
            body.set("tools", upstream.path("tools"));
        } catch (Exception ex) {
            log.warn("ai-service health check failed: {}", ex.getMessage());
            body.put("ai_service", "down");
            body.put("mcp_started", false);
            body.put("mcp_error", "ai-service unreachable: " + ex.getMessage());
            body.put("tools", 0);
        }
        return body;
    }
}
