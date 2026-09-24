package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.TraceNodeRead;
import com.raglatest.backend.api.dto.Dtos.TraceRunRead;
import java.sql.Timestamp;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * run 聚合语义移植自 python services/tracing.py：一个 run = 一次用户提问，
 * list 是「最近 limit 个 run」的摘要（起点/节点数/总耗时/是否出错），
 * detail 按插入顺序返回节点链。
 */
@Repository
public class TraceRepository {

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public TraceRepository(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public record TraceNodeInput(String node, long durationMs, JsonNode input,
                                 JsonNode output, String error) {}

    public List<TraceRunRead> listRuns(String workspaceId, int limit) {
        return jdbc.sql("""
                        SELECT run_id,
                               MIN(created_at) AS started_at,
                               COUNT(*) AS node_count,
                               SUM(duration_ms) AS total_ms,
                               SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_count
                        FROM run_traces WHERE workspace_id = :ws
                        GROUP BY run_id
                        ORDER BY started_at DESC
                        LIMIT :limit
                        """)
                .param("ws", workspaceId)
                .param("limit", limit)
                .query((rs, i) -> new TraceRunRead(
                        rs.getString("run_id"),
                        WorkspaceRepository.toInstant(rs.getTimestamp("started_at")),
                        rs.getInt("node_count"),
                        rs.getLong("total_ms"),
                        rs.getInt("error_count") > 0))
                .list();
    }

    public Optional<List<TraceNodeRead>> runDetail(String workspaceId, String runId) {
        List<TraceNodeRead> nodes = jdbc.sql("""
                        SELECT node, duration_ms, input, output, error
                        FROM run_traces WHERE workspace_id = :ws AND run_id = :run
                        ORDER BY rowid ASC
                        """)
                .param("ws", workspaceId)
                .param("run", runId)
                .query((rs, i) -> new TraceNodeRead(
                        rs.getString("node"),
                        rs.getLong("duration_ms"),
                        parseJson(rs.getString("input")),
                        parseJson(rs.getString("output")),
                        rs.getString("error")))
                .list();
        return nodes.isEmpty() ? Optional.empty() : Optional.of(nodes);
    }

    /** M2 聊天链路落库用；先供测试直接铺 trace 数据。 */
    public void insertNode(String workspaceId, String runId, TraceNodeInput node) {
        Timestamp now = Timestamp.from(java.time.Instant.now());
        jdbc.sql("""
                INSERT INTO run_traces (id, workspace_id, run_id, node, duration_ms, input, output, error, created_at)
                VALUES (:id, :ws, :run, :node, :durationMs, :input, :output, :error, :created)
                """)
                .param("id", WorkspaceRepository.newId())
                .param("ws", workspaceId)
                .param("run", runId)
                .param("node", node.node())
                .param("durationMs", node.durationMs())
                .param("input", mapper.writeValueAsString(node.input()))
                .param("output", mapper.writeValueAsString(node.output()))
                .param("error", node.error())
                .param("created", now)
                .update();
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return mapper.readTree(raw);
    }
}
