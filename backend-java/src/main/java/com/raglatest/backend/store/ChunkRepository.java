package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.CorpusChunk;
import com.raglatest.backend.api.dto.Dtos.CorpusResponse;
import com.raglatest.backend.store.mapper.ChunkMapper;
import com.raglatest.backend.store.mapper.ChunkMapper.ChunkInsert;
import com.raglatest.backend.store.mapper.ChunkMapper.ChunkRow;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * /internal/workspaces/{id}/retrieval-corpus 的数据源：children + parents 全量
 * 语料（corpus 规模小，无需分页）。语义移植自 python retrieval/pipeline.py 的
 * _load_children/_load_parents 与 file map 查询——无状态聊天部署里 ai-service
 * 经 httpx 回读这里，保证业务库只被本进程打开。
 *
 * <p>SQL 在 {@link ChunkMapper}；本类负责 JSON 列解析（meta / token_counts 在
 * mapper 层是 String，因为 MyBatis-Plus 自带的 TypeHandler 绑 Jackson 2，
 * 而本项目用 Jackson 3）与分批写入的编排。</p>
 */
@Repository
public class ChunkRepository {

    /**
     * 单条 INSERT 的行数上限：chunks 有 12 列，Postgres 单语句参数上限 65535，
     * 1000 行 = 12000 个参数，留足余量。大表格文件能产出上万个子块，因此分批
     * 不是"以防万一"而是必然路径。
     */
    private static final int INSERT_BATCH_ROWS = 1000;

    private final ChunkMapper mapper;
    private final ObjectMapper json;

    public ChunkRepository(ChunkMapper mapper, ObjectMapper json) {
        this.mapper = mapper;
        this.json = json;
    }

    public CorpusResponse loadCorpus(String workspaceId) {
        Map<String, String> files = new LinkedHashMap<>();
        mapper.listFilePaths(workspaceId).forEach(row -> files.put(row.id(), row.relPath()));

        List<CorpusChunk> chunks = mapper.loadChunks(workspaceId).stream()
                .map(this::toCorpusChunk)
                .toList();
        return new CorpusResponse(files, chunks);
    }

    private CorpusChunk toCorpusChunk(ChunkRow row) {
        return new CorpusChunk(
                row.id(), row.fileId(), row.parentId(), row.level(), row.text(), row.location(),
                parseJson(row.meta()), parseJson(row.tokenCounts()),
                row.tokenLength(), row.ordinal());
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return json.readTree(raw);
    }

    /**
     * 用一次「删旧 + 插新」替换某文件的全部分块（reindex 语义）。分块 id 与 parent_id 由
     * ai-service 生成（与 Qdrant payload.chunk_id 一致），此处原样落库，不再重新生成。
     * 返回插入的行数。
     *
     * <p>事务包裹 + 分批多值 INSERT：@Transactional 保证删旧与插新的原子性（删旧后插到
     * 一半崩溃会永久丢该文件分块）；分批是为了绕开 Postgres 的单语句参数上限。</p>
     */
    @Transactional
    public int replaceFileChunks(String workspaceId, String fileId, JsonNode chunks) {
        mapper.deleteByFile(fileId);
        OffsetDateTime now = OffsetDateTime.now();
        List<ChunkInsert> rows = new ArrayList<>();
        for (JsonNode chunk : chunks) {
            JsonNode parent = chunk.get("parent_id");
            rows.add(new ChunkInsert(
                    chunk.path("id").asString(),
                    workspaceId,
                    fileId,
                    parent == null || parent.isNull() ? null : parent.asString(),
                    chunk.path("level").asString("child"),
                    chunk.path("ordinal").asInt(0),
                    chunk.path("text").asString(""),
                    chunk.path("location").asString(""),
                    jsonText(chunk.get("meta")),
                    jsonText(chunk.get("token_counts")),
                    chunk.path("token_length").asInt(0),
                    now));
        }
        if (!rows.isEmpty()) {
            for (int start = 0; start < rows.size(); start += INSERT_BATCH_ROWS) {
                int end = Math.min(start + INSERT_BATCH_ROWS, rows.size());
                mapper.insertChunks(rows.subList(start, end));
            }
        }
        return rows.size();
    }

    private String jsonText(JsonNode node) {
        return node == null || node.isNull() ? "{}" : node.toString();
    }
}
