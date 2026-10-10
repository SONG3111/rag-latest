package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/** run_traces 的 SQL：run 级聚合列表与节点链详情。 */
@Mapper
public interface TraceMapper {

    record RunRow(
            String runId,
            OffsetDateTime startedAt,
            int nodeCount,
            long totalMs,
            int errorCount) {}

    record NodeRow(
            String node,
            long durationMs,
            String input,
            String output,
            String error) {}

    @Select("""
            SELECT run_id,
                   MIN(created_at) AS started_at,
                   COUNT(*) AS node_count,
                   SUM(duration_ms) AS total_ms,
                   SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS error_count
            FROM run_traces WHERE workspace_id = #{workspaceId}
            GROUP BY run_id
            ORDER BY started_at DESC
            LIMIT #{limit}
            """)
    List<RunRow> listRuns(@Param("workspaceId") String workspaceId, @Param("limit") int limit);

    /**
     * 按插入顺序返回节点链：{@code seq} 是自增列，专门替代 SQLite 时代的隐式 rowid
     * ——同一毫秒内插入的多个节点靠它保持顺序，而 created_at + id 做不到。
     */
    @Select("""
            SELECT node, duration_ms, input, output, error
            FROM run_traces WHERE workspace_id = #{workspaceId} AND run_id = #{runId}
            ORDER BY seq ASC
            """)
    List<NodeRow> runDetail(@Param("workspaceId") String workspaceId, @Param("runId") String runId);

    @Insert("""
            INSERT INTO run_traces
                (id, workspace_id, run_id, node, duration_ms, input, output, error, created_at)
            VALUES (#{id}, #{workspaceId}, #{runId}, #{node}, #{durationMs},
                    #{input}, #{output}, #{error}, #{now})
            """)
    int insertNode(@Param("id") String id,
                   @Param("workspaceId") String workspaceId,
                   @Param("runId") String runId,
                   @Param("node") String node,
                   @Param("durationMs") long durationMs,
                   @Param("input") String input,
                   @Param("output") String output,
                   @Param("error") String error,
                   @Param("now") OffsetDateTime now);
}
