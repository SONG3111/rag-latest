package com.raglatest.backend.api.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.time.Instant;
import tools.jackson.databind.JsonNode;

/**
 * 对外 API 的请求/响应记录，字段形状对齐原 Python pydantic schemas.py
 * （全局 SNAKE_CASE 命名策略，见 application.yml）。
 * 动态 JSON 字段（citations/tool_calls）用 JsonNode 透传，不另建模型。
 */
public final class Dtos {

    private Dtos() {}

    public record WorkspaceCreate(
            @NotBlank @Size(max = 200) String name,
            @Size(max = 2000) String description) {}

    public record WorkspaceRead(
            String id, String name, String description,
            Instant createdAt, Instant updatedAt, int fileCount) {}

    public record FileRead(
            String id, String relPath, String kind, long sizeBytes,
            String status, int chunkCount, String error,
            Instant indexedAt, Instant createdAt) {}

    public record MessageRead(
            String id, String role, String content,
            JsonNode citations, JsonNode toolCalls, String feedback,
            Instant createdAt) {}

    /** 点赞/点踩；"none" 清除（与 python MessageFeedback 一致）。 */
    public record MessageFeedback(
            @NotBlank @Pattern(regexp = "up|down|none") String feedback) {}

    /** 上传/重建索引的单文件结果。M1 阶段未接索引：status=pending、计数为 0。 */
    public record IndexingResponse(
            String fileId, String relPath, int chunkCount,
            int vectorCount, String status, String error) {}

    public record TraceRunRead(
            String runId, Instant startedAt, int nodeCount, long totalMs, boolean hasError) {}

    public record TraceNodeRead(
            String node, long durationMs, JsonNode input, JsonNode output, String error) {}

    public record TraceRunDetail(String runId, java.util.List<TraceNodeRead> nodes) {}

    /** 聊天请求，对齐 python schemas.ChatRequest（min_length=1 / max 8000）。 */
    public record ChatRequest(
            @NotBlank @Size(max = 8000) String message) {}

    /** /internal 检索语料的一个 chunk 行（ai-service 的 ChunkRecord 逐字段对应）。 */
    public record CorpusChunk(
            String id, String fileId, String parentId, String level, String text,
            String location, JsonNode meta, JsonNode tokenCounts,
            int tokenLength, int ordinal) {}

    /** /internal 检索语料响应：files 为 {file_id: rel_path}，chunks 为 children+parents 全量。 */
    public record CorpusResponse(
            java.util.Map<String, String> files, java.util.List<CorpusChunk> chunks) {}

    /** 会话滚动摘要（messages 表书签算术的另一半）。 */
    public record SummaryRow(String summary, int coveredCount) {}

    /**
     * 审批操作对外响应，字段形状对齐 python schemas.OperationRead（不含 arguments；
     * full 参数只出现在 SSE proposal 帧里）。diff 缺失时序列化为空数组。
     */
    public record OperationRead(
            String id, String toolName, String relPath, String summary, String status,
            JsonNode diff, JsonNode result, String error, String backupPath,
            Instant createdAt, Instant resolvedAt) {}
}
