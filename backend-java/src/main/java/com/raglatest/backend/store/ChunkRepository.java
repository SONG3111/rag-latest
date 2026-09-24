package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.CorpusChunk;
import com.raglatest.backend.api.dto.Dtos.CorpusResponse;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * /internal/workspaces/{id}/retrieval-corpus 的数据源：children + parents 全量
 * 语料（corpus 规模小，无需分页）。语义移植自 python retrieval/pipeline.py 的
 * _load_children/_load_parents 与 file map 查询——无状态聊天部署里 ai-service
 * 经 httpx 回读这里，保证 app.db 只被本进程打开。
 */
@Repository
public class ChunkRepository {

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public ChunkRepository(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public CorpusResponse loadCorpus(String workspaceId) {
        Map<String, String> files = new LinkedHashMap<>();
        // 注意：Spring 7 JdbcClient 的 query(...) 是惰性的，必须 .list() 才执行。
        jdbc.sql("SELECT id, rel_path FROM document_files WHERE workspace_id = :ws")
                .param("ws", workspaceId)
                .query((rs, i) -> new String[] {
                        rs.getString("id"), rs.getString("rel_path")})
                .list()
                .forEach(row -> files.put(row[0], row[1]));

        List<CorpusChunk> chunks = jdbc.sql("""
                        SELECT id, file_id, parent_id, level, text, location,
                               meta, token_counts, token_length, ordinal
                        FROM chunks WHERE workspace_id = :ws
                        ORDER BY ordinal ASC, created_at ASC, id ASC
                        """)
                .param("ws", workspaceId)
                .query((rs, i) -> new CorpusChunk(
                        rs.getString("id"),
                        rs.getString("file_id"),
                        rs.getString("parent_id"),
                        rs.getString("level"),
                        rs.getString("text"),
                        rs.getString("location"),
                        parseJson(rs.getString("meta")),
                        parseJson(rs.getString("token_counts")),
                        rs.getInt("token_length"),
                        rs.getInt("ordinal")))
                .list();
        return new CorpusResponse(files, chunks);
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return mapper.readTree(raw);
    }
}
