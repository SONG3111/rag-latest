package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.TraceNodeRead;
import com.raglatest.backend.api.dto.Dtos.TraceRunRead;
import com.raglatest.backend.store.mapper.TraceMapper;
import com.raglatest.backend.store.mapper.TraceMapper.NodeRow;
import com.raglatest.backend.store.mapper.TraceMapper.RunRow;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
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

    private final TraceMapper mapper;
    private final ObjectMapper json;

    public TraceRepository(TraceMapper mapper, ObjectMapper json) {
        this.mapper = mapper;
        this.json = json;
    }

    public record TraceNodeInput(String node, long durationMs, JsonNode input,
                                 JsonNode output, String error) {}

    public List<TraceRunRead> listRuns(String workspaceId, int limit) {
        return mapper.listRuns(workspaceId, limit).stream()
                .map(TraceRepository::toRunRead)
                .toList();
    }

    public Optional<List<TraceNodeRead>> runDetail(String workspaceId, String runId) {
        List<TraceNodeRead> nodes = mapper.runDetail(workspaceId, runId).stream()
                .map(this::toNodeRead)
                .toList();
        return nodes.isEmpty() ? Optional.empty() : Optional.of(nodes);
    }

    /** 聊天链路落库用；也供测试直接铺 trace 数据。 */
    public void insertNode(String workspaceId, String runId, TraceNodeInput node) {
        mapper.insertNode(
                WorkspaceRepository.newId(), workspaceId, runId, node.node(), node.durationMs(),
                json.writeValueAsString(node.input()),
                json.writeValueAsString(node.output()),
                node.error(),
                OffsetDateTime.now());
    }

    private static TraceRunRead toRunRead(RunRow row) {
        return new TraceRunRead(
                row.runId(),
                WorkspaceRepository.toInstant(row.startedAt()),
                row.nodeCount(),
                row.totalMs(),
                row.errorCount() > 0);
    }

    private TraceNodeRead toNodeRead(NodeRow row) {
        return new TraceNodeRead(
                row.node(),
                row.durationMs(),
                parseJson(row.input()),
                parseJson(row.output()),
                row.error());
    }

    private JsonNode parseJson(String raw) {
        if (raw == null || raw.isBlank()) {
            return null;
        }
        return json.readTree(raw);
    }
}
