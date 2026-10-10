package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.MessageRead;
import com.raglatest.backend.store.mapper.MessageMapper;
import com.raglatest.backend.store.mapper.MessageMapper.MessageRow;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/** 会话消息：历史窗、书签算术与写入（含"谁说的"这一多用户审计字段）。 */
@Repository
public class MessageRepository {

    private final MessageMapper mapper;
    private final ObjectMapper json;

    public MessageRepository(MessageMapper mapper, ObjectMapper json) {
        this.mapper = mapper;
        this.json = json;
    }

    /**
     * 最新 {@code limit} 条消息按时间正序返回（UI 展示的是对话，长历史不能把
     * 最近几轮挤出响应窗口）。citations/tool_calls 为 JSON 文本列，读出即解析。
     */
    public List<MessageRead> recent(String workspaceId, int limit) {
        List<MessageRead> newestFirst = mapper.recentNewestFirst(workspaceId, limit).stream()
                .map(this::toRead)
                .toList();
        return newestFirst.reversed();
    }

    /** 全量消息数：covered 书签算术用（超过请求封顶时与 messages 列表长度不一致）。 */
    public long countAll(String workspaceId) {
        return mapper.countAll(workspaceId);
    }

    /** 最新一条消息的时间（stale files 的 cutoff；无消息时为 null）。 */
    public Instant latestCreatedAt(String workspaceId) {
        OffsetDateTime latest = mapper.latestCreatedAt(workspaceId);
        return latest == null ? null : latest.toInstant();
    }

    public Optional<MessageRead> findInWorkspace(String workspaceId, String messageId) {
        return Optional.ofNullable(mapper.findInWorkspace(workspaceId, messageId))
                .map(this::toRead);
    }

    /**
     * 落一条消息。{@code userId} 只对 user 角色行非空——assistant 行与系统注入行
     * 没有"谁说的"，落 NULL；外键是 ON DELETE SET NULL，删号不会连坐历史。
     */
    public String insert(String workspaceId, String userId, String role, String content,
                         JsonNode toolCalls, JsonNode citations) {
        String id = WorkspaceRepository.newId();
        mapper.insert(id, workspaceId, userId, role, content,
                toolCalls == null ? null : json.writeValueAsString(toolCalls),
                citations == null ? null : json.writeValueAsString(citations),
                OffsetDateTime.now());
        return id;
    }

    public void updateFeedback(String messageId, String feedback) {
        mapper.updateFeedback(messageId, feedback);
    }

    private MessageRead toRead(MessageRow row) {
        return new MessageRead(
                row.id(),
                row.role(),
                row.content(),
                parseJson(row.citations()),
                parseJson(row.toolCalls()),
                row.feedback(),
                WorkspaceRepository.toInstant(row.createdAt()));
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return json.readTree(raw);
    }
}
