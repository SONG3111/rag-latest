package com.raglatest.backend.internal;

import com.raglatest.backend.api.ApiException;
import com.raglatest.backend.config.RagProperties;
import java.time.Duration;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.reactive.function.client.WebClientResponseException;
import org.springframework.web.util.UriBuilder;
import reactor.netty.http.client.HttpClient;
import tools.jackson.databind.JsonNode;

/**
 * ai-service（FastAPI，内网 8001）的唯一客户端。
 *
 * 所有请求走相对路径，baseUrl 来自配置；noProxy() 与 Python 版 trust_env=False
 * 同因（本机代理环境变量会劫持内网调用）。上游 4xx/5xx 转成同状态码的
 * ApiException，detail 透传，保证前端看到的错误文本不丢语义。
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
        return get("/health");
    }

    /** MCP 工具清单（含审批注解），GET /api/.../tools 的数据源。 */
    public JsonNode listTools() {
        return get("/v1/tools");
    }

    /** 引用出处预览：按 location 解析成读窗口后经 ai-service 的 MCP 读工具取原文。 */
    public JsonNode preview(String workspaceId, String file, String location) {
        return get(uri -> uri.path("/v1/workspaces/{id}/preview")
                .queryParam("file", file)
                .queryParam("location", location)
                .build(workspaceId));
    }

    /** 删除工作区时清掉它的稠密索引集合；失败由调用方降级为告警。 */
    public void dropCollection(String workspaceId) {
        webClient.delete()
                .uri("/v1/collections/{id}", workspaceId)
                .retrieve()
                .toBodilessEntity()
                .block(requestTimeout);
    }

    /** 删除单文件的向量（文件删除时），按 file_id 过滤。 */
    public void deleteFileVectors(String workspaceId, String fileId) {
        webClient.method(HttpMethod.DELETE)
                .uri(uri -> uri.path("/v1/collections/{id}/vectors")
                        .queryParam("file_id", fileId)
                        .build(workspaceId))
                .retrieve()
                .toBodilessEntity()
                .block(requestTimeout);
    }

    private JsonNode get(String path) {
        return exchange(webClient.get().uri(path).accept(MediaType.APPLICATION_JSON));
    }

    private JsonNode get(java.util.function.Function<UriBuilder, java.net.URI> uri) {
        return exchange(webClient.get().uri(uri).accept(MediaType.APPLICATION_JSON));
    }

    private JsonNode exchange(WebClient.RequestHeadersSpec<?> spec) {
        try {
            return spec.retrieve().bodyToMono(JsonNode.class).block(requestTimeout);
        } catch (WebClientResponseException ex) {
            String detail = readDetail(ex);
            throw new ApiException(
                    org.springframework.http.HttpStatus.resolve(ex.getStatusCode().value()) != null
                            ? org.springframework.http.HttpStatus.resolve(ex.getStatusCode().value())
                            : org.springframework.http.HttpStatus.BAD_GATEWAY,
                    detail);
        }
    }

    private static String readDetail(WebClientResponseException ex) {
        try {
            JsonNode body = ex.getResponseBodyAs(JsonNode.class);
            if (body != null && body.hasNonNull("detail")) {
                return body.get("detail").asText();
            }
        } catch (Exception ignored) {
            // 非 JSON 错误体，退回状态文本
        }
        return "ai-service error: " + ex.getStatusCode();
    }
}
