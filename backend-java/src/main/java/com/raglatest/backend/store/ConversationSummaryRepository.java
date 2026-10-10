package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.SummaryRow;
import com.raglatest.backend.store.mapper.SummaryMapper;
import java.time.OffsetDateTime;
import java.util.Optional;
import org.springframework.stereotype.Repository;

/**
 * 会话滚动摘要（conversation_summaries，每工作区一行）。get/upsert 语义移植自
 * python services/memory.py 的 load_summary 与 compact_memory 的写回半边；
 * 压缩决策在 ai-service 的纯函数 compute_compaction 里做，结果经 persist 帧带回。
 */
@Repository
public class ConversationSummaryRepository {

    private final SummaryMapper mapper;

    public ConversationSummaryRepository(SummaryMapper mapper) {
        this.mapper = mapper;
    }

    public Optional<SummaryRow> find(String workspaceId) {
        return Optional.ofNullable(mapper.find(workspaceId))
                .map(row -> new SummaryRow(row.summary(), row.coveredCount()));
    }

    public void upsert(String workspaceId, String summary, int coveredCount) {
        mapper.upsert(workspaceId, summary, coveredCount, OffsetDateTime.now());
    }
}
