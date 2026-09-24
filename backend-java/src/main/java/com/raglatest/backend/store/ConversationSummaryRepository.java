package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.SummaryRow;
import java.sql.Timestamp;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

/**
 * 会话滚动摘要（conversation_summaries，每工作区一行）。get/upsert 语义移植自
 * python services/memory.py 的 load_summary 与 compact_memory 的写回半边；
 * 压缩决策在 ai-service 的纯函数 compute_compaction 里做，结果经 persist 帧带回。
 */
@Repository
public class ConversationSummaryRepository {

    private final JdbcClient jdbc;

    public ConversationSummaryRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    public Optional<SummaryRow> find(String workspaceId) {
        return jdbc.sql("SELECT summary, covered_count FROM conversation_summaries WHERE workspace_id = :ws")
                .param("ws", workspaceId)
                .query((rs, i) -> new SummaryRow(rs.getString("summary"), rs.getInt("covered_count")))
                .optional();
    }

    public void upsert(String workspaceId, String summary, int coveredCount) {
        int updated = jdbc.sql("""
                        UPDATE conversation_summaries
                        SET summary = :summary, covered_count = :covered, updated_at = :updated
                        WHERE workspace_id = :ws
                        """)
                .param("summary", summary)
                .param("covered", coveredCount)
                .param("updated", Timestamp.from(java.time.Instant.now()))
                .param("ws", workspaceId)
                .update();
        if (updated == 0) {
            jdbc.sql("""
                            INSERT INTO conversation_summaries (workspace_id, summary, covered_count, updated_at)
                            VALUES (:ws, :summary, :covered, :updated)
                            """)
                    .param("ws", workspaceId)
                    .param("summary", summary)
                    .param("covered", coveredCount)
                    .param("updated", Timestamp.from(java.time.Instant.now()))
                    .update();
        }
    }
}
