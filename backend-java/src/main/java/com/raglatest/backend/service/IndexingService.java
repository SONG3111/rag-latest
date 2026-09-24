package com.raglatest.backend.service;

import com.raglatest.backend.api.ApiException;
import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.api.dto.Dtos.IndexingResponse;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.store.ChunkRepository;
import com.raglatest.backend.store.DocumentFileRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;

/**
 * 文件索引编排：把「解析 + 嵌入 + 向量写入」委托给 ai-service 的 {@code /v1/index}，
 * 拿回分块行后在本侧落库（app.db 只被 Java 进程打开）。
 *
 * <p>与旧 Python 版 index_file 的一致纪律：ai-service 的索引调用（可能数秒~数十秒）发生在
 * 任何数据库写入之前，SQLite 写锁只覆盖「删旧行 + 插新行 + 更新文件状态」这一段，不会在
 * 嵌入期间被长时间持有。</p>
 */
@Service
public class IndexingService {

    private static final Logger log = LoggerFactory.getLogger(IndexingService.class);

    private final AiServiceClient aiService;
    private final DocumentFileRepository files;
    private final ChunkRepository chunks;

    public IndexingService(AiServiceClient aiService, DocumentFileRepository files,
                           ChunkRepository chunks) {
        this.aiService = aiService;
        this.files = files;
        this.chunks = chunks;
    }

    /** 索引（或重建索引）单个文件，返回对外统一的 IndexingResponse。 */
    public IndexingResponse index(String workspaceId, String fileId) {
        FileRead record = files.findInWorkspace(workspaceId, fileId)
                .orElseThrow(() -> ApiException.notFound("file not found"));

        JsonNode result;
        try {
            result = aiService.indexFile(workspaceId, fileId, record.relPath());
        } catch (ApiException ex) {
            // 上游不可达/解析报错：登记失败原因，文件保留以便重试。
            files.markFailed(fileId, ex.getMessage());
            return new IndexingResponse(fileId, record.relPath(), 0, 0, "failed", ex.getMessage());
        }

        String status = result.path("status").asString("failed");
        int chunkCount = result.path("chunk_count").asInt(0);
        int vectorCount = result.path("vector_count").asInt(0);
        String error = result.hasNonNull("error") ? result.get("error").asString() : null;
        JsonNode rows = result.path("chunks");

        if ("indexed".equals(status) && rows.isArray()) {
            int inserted = chunks.replaceFileChunks(workspaceId, fileId, rows);
            files.markIndexed(fileId, chunkCount, result.path("checksum").asString(""));
            log.info("indexed {}: {} chunks, {} vectors", record.relPath(), inserted, vectorCount);
        } else {
            files.markFailed(fileId, error != null ? error : "indexing failed");
        }
        return new IndexingResponse(fileId, record.relPath(), chunkCount, vectorCount, status, error);
    }
}
