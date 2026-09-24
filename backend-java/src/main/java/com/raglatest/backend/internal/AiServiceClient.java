package com.raglatest.backend.internal;

import com.raglatest.backend.config.RagProperties;
import java.time.Duration;
import org.springframework.http.MediaType;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import reactor.netty.http.client.HttpClient;
import tools.jackson.databind.JsonNode;

/**
 * ai-service（FastAPI，内网 8001）的唯一客户端。
 *
 * 所有请求走相对路径（如 "/health"），baseUrl 来自 {@link RagProperties.AiService}；
 * 代理类操作（tools/call、chat/stream）超时另配，普通调用用 requestTimeout。
 */
@Component
public class AiServiceClient {

    private final WebClient webClient;
    private final Duration requestTimeout;

    public AiServiceClient(RagProperties properties) {
        this.requestTimeout = properties.aiService().requestTimeout();
        HttpClient httpClient = HttpClient.create().noProxy();
        this.webClient = WebClient.builder()
                .baseUrl(properties.aiService().baseUrl())
                .clientConnector(new ReactorClientHttpConnector(httpClient))
                .build();
    }

    public JsonNode getHealth() {
        return webClient.get()
                .uri("/health")
                .accept(MediaType.APPLICATION_JSON)
                .retrieve()
                .bodyToMono(JsonNode.class)
                .block(requestTimeout);
    }

    /** 供测试注入替换 baseUrl / 超时后的构建方式。 */
    /** 供测试与后续内部端点复用同一个 WebClient。 */
    WebClient client() {
        return webClient;
    }
}
