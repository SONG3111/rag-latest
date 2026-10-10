package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/** conversation_summaries 的 SQL（每工作区一行的滚动摘要书签）。 */
@Mapper
public interface SummaryMapper {

    record SummaryRowData(String summary, int coveredCount) {}

    @Select("SELECT summary, covered_count FROM conversation_summaries WHERE workspace_id = #{workspaceId}")
    SummaryRowData find(@Param("workspaceId") String workspaceId);

    /**
     * upsert 用 Postgres 的 {@code ON CONFLICT} 而不是先 UPDATE 再看影响行数：
     * 后者在并发回合下会出现两个事务都判定"不存在"、双双 INSERT 撞主键。
     */
    @Insert("""
            INSERT INTO conversation_summaries (workspace_id, summary, covered_count, updated_at)
            VALUES (#{workspaceId}, #{summary}, #{coveredCount}, #{now})
            ON CONFLICT (workspace_id) DO UPDATE
            SET summary = EXCLUDED.summary,
                covered_count = EXCLUDED.covered_count,
                updated_at = EXCLUDED.updated_at
            """)
    int upsert(@Param("workspaceId") String workspaceId,
               @Param("summary") String summary,
               @Param("coveredCount") int coveredCount,
               @Param("now") OffsetDateTime now);
}
