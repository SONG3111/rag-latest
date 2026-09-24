package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.MessageRead;
import java.sql.Timestamp;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

@Repository
public class MessageRepository {

    private final JdbcClient jdbc;
    private final ObjectMapper mapper;

    public MessageRepository(JdbcClient jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /**
     * 最新 {@code limit} 条消息按时间正序返回（UI 展示的是对话，长历史不能把
     * 最近几轮挤出响应窗口）。citations/tool_calls 为 JSON 文本列，读出即解析。
     */
    public List<MessageRead> recent(String workspaceId, int limit) {
        List<MessageRead> newestFirst = jdbc.sql("""
                        SELECT id, role, content, tool_calls, citations, feedback, created_at
                        FROM messages WHERE workspace_id = :ws
                        ORDER BY created_at DESC, id DESC LIMIT :limit
                        """)
                .param("ws", workspaceId)
                .param("limit", limit)
                .query(this::mapRow)
                .list();
        return newestFirst.reversed();
    }

    /** 全量消息数：covered 书签算术用（超过请求封顶时与 messages 列表长度不一致）。 */
    public long countAll(String workspaceId) {
        return jdbc.sql("SELECT COUNT(*) FROM messages WHERE workspace_id = :ws")
                .param("ws", workspaceId)
                .query(Long.class)
                .single();
    }

    /** 最新一条消息的时间（stale files 的 cutoff；无消息时为 null）。 */
    public java.time.Instant latestCreatedAt(String workspaceId) {
        Timestamp latest = jdbc.sql(
                        "SELECT MAX(created_at) FROM messages WHERE workspace_id = :ws")
                .param("ws", workspaceId)
                .query((rs, i) -> rs.getTimestamp(1))
                .optional()
                .orElse(null);
        return latest == null ? null : latest.toInstant();
    }

    public Optional<MessageRead> findInWorkspace(String workspaceId, String messageId) {
        return jdbc.sql("""
                        SELECT id, role, content, tool_calls, citations, feedback, created_at
                        FROM messages WHERE id = :id AND workspace_id = :ws
                        """)
                .param("id", messageId)
                .param("ws", workspaceId)
                .query(this::mapRow)
                .optional();
    }

    public String insert(String workspaceId, String role, String content,
                         JsonNode toolCalls, JsonNode citations) {
        String id = WorkspaceRepository.newId();
        Timestamp now = Timestamp.from(java.time.Instant.now());
        jdbc.sql("""
                INSERT INTO messages (id, workspace_id, role, content, tool_calls, citations, created_at)
                VALUES (:id, :ws, :role, :content, :toolCalls, :citations, :created)
                """)
                .param("id", id)
                .param("ws", workspaceId)
                .param("role", role)
                .param("content", content)
                .param("toolCalls", toolCalls == null ? null : mapper.writeValueAsString(toolCalls))
                .param("citations", citations == null ? null : mapper.writeValueAsString(citations))
                .param("created", now)
                .update();
        return id;
    }

    public void updateFeedback(String messageId, String feedback) {
        jdbc.sql("UPDATE messages SET feedback = :feedback WHERE id = :id")
                .param("feedback", feedback)
                .param("id", messageId)
                .update();
    }

    private MessageRead mapRow(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
        return new MessageRead(
                rs.getString("id"),
                rs.getString("role"),
                rs.getString("content"),
                parseJson(rs.getString("citations")),
                parseJson(rs.getString("tool_calls")),
                rs.getString("feedback"),
                WorkspaceRepository.toInstant(rs.getTimestamp("created_at")));
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return mapper.readTree(raw);
    }
}
