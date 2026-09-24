package com.raglatest.backend.store;

import java.sql.Timestamp;
import java.util.List;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * operations 表的 M2 最小面：聊天链路拦截 proposal 帧时建 proposed 行
 * （message_id 与旧版 create_operation 一样保持 null）。apply/reject/revert
 * 全生命周期在 M3 补齐。
 */
@Repository
public class OperationRepository {

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public OperationRepository(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

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
                .param("args", mapper.writeValueAsString(arguments))
                .param("diff", mapper.writeValueAsString(diff))
                .param("summary", summary)
                .param("created", Timestamp.from(java.time.Instant.now()))
                .update();
        return id;
    }

    /** 只读面（M2 测试与 M3 列表端点的底座）：按时间正序返回工作区的操作行。 */
    public record OperationRow(String id, String toolName, String relPath, JsonNode arguments,
                               JsonNode diff, String summary, String status, JsonNode result) {}

    public List<OperationRow> listByWorkspace(String workspaceId) {
        return jdbc.sql("""
                        SELECT id, tool_name, rel_path, arguments, diff, summary, status, result
                        FROM operations WHERE workspace_id = :ws ORDER BY created_at ASC
                        """)
                .param("ws", workspaceId)
                .query((rs, i) -> new OperationRow(
                        rs.getString("id"),
                        rs.getString("tool_name"),
                        rs.getString("rel_path"),
                        parseJson(rs.getString("arguments")),
                        parseJson(rs.getString("diff")),
                        rs.getString("summary"),
                        rs.getString("status"),
                        parseJson(rs.getString("result"))))
                .list();
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return mapper.readTree(raw);
    }
}
