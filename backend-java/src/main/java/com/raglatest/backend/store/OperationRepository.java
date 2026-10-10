package com.raglatest.backend.store;

import com.raglatest.backend.store.mapper.OperationMapper;
import com.raglatest.backend.store.mapper.OperationMapper.OperationRowData;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * operations 表：审批闭环的持久化（提案、列表、状态迁移、参数改写）。
 *
 * <p>多用户改造在这里加了两个只写不读的审计列：{@code proposed_by_user_id}（谁触发
 * 了这次写入）与 {@code resolved_by_user_id}（谁批准的）。对外的 OperationRead
 * 契约刻意不变（前端零改动），取证直接查库；等下一轮做"审计面板"时再露出来。</p>
 */
@Repository
public class OperationRepository {

    private final OperationMapper mapper;
    private final ObjectMapper json;

    public OperationRepository(OperationMapper mapper, ObjectMapper json) {
        this.mapper = mapper;
        this.json = json;
    }

    /** 建一条待确认提案行，返回生成的 id（替换进 proposal 帧后转发）。 */
    public String insertProposed(String workspaceId, String userId, String toolName, String relPath,
                                 JsonNode arguments, JsonNode diff, String summary) {
        String id = WorkspaceRepository.newId();
        mapper.insertProposed(id, workspaceId, userId, toolName, relPath,
                jsonText(arguments), jsonText(diff, "[]"), summary, OffsetDateTime.now());
        return id;
    }

    /** 一行操作（含备份路径与错误，供列表/审批端点使用；created/resolved 供对外契约）。 */
    public record OperationRow(String id, String toolName, String relPath, JsonNode arguments,
                               JsonNode diff, String summary, String status, String backupPath,
                               JsonNode result, String error,
                               Instant createdAt, Instant resolvedAt) {}

    public List<OperationRow> listByWorkspace(String workspaceId) {
        return mapper.listByWorkspace(workspaceId).stream().map(this::toRow).toList();
    }

    /** 仅待确认（proposed）的行，按创建时间正序——审批与 rebase 只用这批。 */
    public List<OperationRow> listPending(String workspaceId) {
        return mapper.listPending(workspaceId).stream().map(this::toRow).toList();
    }

    public Optional<OperationRow> find(String workspaceId, String operationId) {
        return Optional.ofNullable(mapper.find(workspaceId, operationId)).map(this::toRow);
    }

    public void markApplied(String operationId, String userId, JsonNode result, JsonNode diff,
                            String backupPath) {
        mapper.markApplied(operationId, userId, jsonText(result), jsonText(diff), backupPath,
                OffsetDateTime.now());
    }

    public void markFailed(String operationId, String error) {
        mapper.markFailed(operationId, error);
    }

    public void markRejected(String operationId, String userId, String error) {
        mapper.markRejected(operationId, userId, error, OffsetDateTime.now());
    }

    /** rebase / 摘要刷新：改写一条待确认提案的参数（链式 digest 或坐标位移）。 */
    public void updateArguments(String operationId, JsonNode arguments, String summary, JsonNode diff) {
        mapper.updateArguments(operationId, jsonText(arguments), summary, jsonText(diff));
    }

    private OperationRow toRow(OperationRowData row) {
        return new OperationRow(
                row.id(),
                row.toolName(),
                row.relPath(),
                parseJson(row.arguments()),
                parseJson(row.diff()),
                row.summary(),
                row.status(),
                row.backupPath(),
                parseJson(row.result()),
                row.error(),
                WorkspaceRepository.toInstant(row.createdAt()),
                WorkspaceRepository.toInstant(row.resolvedAt()));
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
        return json.readTree(raw);
    }
}
