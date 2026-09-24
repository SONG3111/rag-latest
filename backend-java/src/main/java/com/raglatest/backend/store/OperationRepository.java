package com.raglatest.backend.store;

import java.sql.Timestamp;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * operations 表：审批闭环的持久化。M2 只建 proposed 行；M3 补全
 * find / listPending / markApplied / markFailed / markRejected / updateArguments。
 */
@Repository
public class OperationRepository {

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public OperationRepository(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    private static final String SELECT = """
            SELECT id, tool_name, rel_path, arguments, diff, summary, status,
                   backup_path, result, error, created_at, resolved_at
            FROM operations
            """;

    /** 建一条待确认提案行，返回生成的 id（替换进 proposal 帧后转发）。 */
    public String insertProposed(String workspaceId, String toolName, String relPath,
                                 JsonNode arguments, JsonNode diff, String summary) {
        String id = WorkspaceRepository.newId();
        jdbc.sql("""
                        INSERT INTO operations
                            (id, workspace_id, message_id, tool_name, rel_path, arguments,
                             diff, summary, status, created_at)
                        VALUES (:id, :ws, NULL, :tool, :relPath, :args, :diff, :summary,
                                'proposed', :created)
                        """)
                .param("id", id)
                .param("ws", workspaceId)
                .param("tool", toolName)
                .param("relPath", relPath)
                .param("args", jsonText(arguments))
                .param("diff", jsonText(diff, "[]"))
                .param("summary", summary)
                .param("created", Timestamp.from(java.time.Instant.now()))
                .update();
        return id;
    }

    /** 一行操作（含备份路径与错误，供列表/审批端点使用；created/resolved 供对外契约）。 */
    public record OperationRow(String id, String toolName, String relPath, JsonNode arguments,
                               JsonNode diff, String summary, String status, String backupPath,
                               JsonNode result, String error,
                               java.time.Instant createdAt, java.time.Instant resolvedAt) {}

    public List<OperationRow> listByWorkspace(String workspaceId) {
        return jdbc.sql(SELECT + " WHERE workspace_id = :ws ORDER BY created_at ASC")
                .param("ws", workspaceId)
                .query(this::mapRow)
                .list();
    }

    /** 仅待确认（proposed）的行，按创建时间正序——审批与 rebase 只用这批。 */
    public List<OperationRow> listPending(String workspaceId) {
        return jdbc.sql(SELECT + " WHERE workspace_id = :ws AND status = 'proposed'"
                        + " ORDER BY created_at ASC")
                .param("ws", workspaceId)
                .query(this::mapRow)
                .list();
    }

    public Optional<OperationRow> find(String workspaceId, String operationId) {
        return jdbc.sql(SELECT + " WHERE workspace_id = :ws AND id = :id")
                .param("ws", workspaceId)
                .param("id", operationId)
                .query(this::mapRow)
                .optional();
    }

    public void markApplied(String operationId, JsonNode result, JsonNode diff, String backupPath) {
        jdbc.sql("""
                        UPDATE operations
                        SET status = 'applied', result = :result, diff = :diff,
                            backup_path = :backup, error = NULL, resolved_at = :at
                        WHERE id = :id
                        """)
                .param("result", jsonText(result))
                .param("diff", jsonText(diff))
                .param("backup", backupPath)
                .param("at", Timestamp.from(java.time.Instant.now()))
                .param("id", operationId)
                .update();
    }

    public void markFailed(String operationId, String error) {
        jdbc.sql("UPDATE operations SET status = 'failed', error = :error WHERE id = :id")
                .param("error", error)
                .param("id", operationId)
                .update();
    }

    public void markRejected(String operationId, String error) {
        jdbc.sql("""
                        UPDATE operations
                        SET status = 'rejected', error = :error, resolved_at = :at
                        WHERE id = :id
                        """)
                .param("error", error)
                .param("at", Timestamp.from(java.time.Instant.now()))
                .param("id", operationId)
                .update();
    }

    /** rebase / 摘要刷新：改写一条待确认提案的参数（链式 digest 或坐标位移）。 */
    public void updateArguments(String operationId, JsonNode arguments, String summary, JsonNode diff) {
        jdbc.sql("""
                        UPDATE operations
                        SET arguments = :args, summary = :summary, diff = :diff
                        WHERE id = :id
                        """)
                .param("args", jsonText(arguments))
                .param("summary", summary)
                .param("diff", jsonText(diff))
                .param("id", operationId)
                .update();
    }

    private OperationRow mapRow(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
        return new OperationRow(
                rs.getString("id"),
                rs.getString("tool_name"),
                rs.getString("rel_path"),
                parseJson(rs.getString("arguments")),
                parseJson(rs.getString("diff")),
                rs.getString("summary"),
                rs.getString("status"),
                rs.getString("backup_path"),
                parseJson(rs.getString("result")),
                rs.getString("error"),
                WorkspaceRepository.toInstant(rs.getTimestamp("created_at")),
                WorkspaceRepository.toInstant(rs.getTimestamp("resolved_at")));
    }

    private String jsonText(JsonNode node) {
        return jsonText(node, "{}");
    }

    private String jsonText(JsonNode node, String fallback) {
        return node == null || node.isNull() ? fallback : node.toString();
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return mapper.readTree(raw);
    }
}
