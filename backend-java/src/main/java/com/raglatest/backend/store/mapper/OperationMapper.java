package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/** operations 的 SQL：审批闭环的持久化（提案、列表、四种状态迁移、参数改写）。 */
@Mapper
public interface OperationMapper {

    record OperationRowData(
            String id,
            String toolName,
            String relPath,
            String arguments,
            String diff,
            String summary,
            String status,
            String backupPath,
            String result,
            String error,
            String proposedByUserId,
            String resolvedByUserId,
            OffsetDateTime createdAt,
            OffsetDateTime resolvedAt) {}

    String COLUMNS = """
            SELECT id, tool_name, rel_path, arguments, diff, summary, status,
                   backup_path, result, error, proposed_by_user_id, resolved_by_user_id,
                   created_at, resolved_at
            FROM operations
            """;

    @Insert("""
            INSERT INTO operations
                (id, workspace_id, message_id, proposed_by_user_id, tool_name, rel_path,
                 arguments, diff, summary, status, created_at)
            VALUES (#{id}, #{workspaceId}, NULL, #{userId}, #{toolName}, #{relPath},
                    #{arguments}, #{diff}, #{summary}, 'proposed', #{now})
            """)
    int insertProposed(@Param("id") String id,
                       @Param("workspaceId") String workspaceId,
                       @Param("userId") String userId,
                       @Param("toolName") String toolName,
                       @Param("relPath") String relPath,
                       @Param("arguments") String arguments,
                       @Param("diff") String diff,
                       @Param("summary") String summary,
                       @Param("now") OffsetDateTime now);

    @Select(COLUMNS + " WHERE workspace_id = #{workspaceId} ORDER BY created_at ASC, id ASC")
    List<OperationRowData> listByWorkspace(@Param("workspaceId") String workspaceId);

    @Select(COLUMNS + " WHERE workspace_id = #{workspaceId} AND status = 'proposed'"
            + " ORDER BY created_at ASC, id ASC")
    List<OperationRowData> listPending(@Param("workspaceId") String workspaceId);

    @Select(COLUMNS + " WHERE workspace_id = #{workspaceId} AND id = #{operationId}")
    OperationRowData find(@Param("workspaceId") String workspaceId,
                          @Param("operationId") String operationId);

    @Update("""
            UPDATE operations
            SET status = 'applied', result = #{result}, diff = #{diff},
                backup_path = #{backupPath}, error = NULL, resolved_at = #{now},
                resolved_by_user_id = #{userId}
            WHERE id = #{operationId}
            """)
    int markApplied(@Param("operationId") String operationId,
                    @Param("userId") String userId,
                    @Param("result") String result,
                    @Param("diff") String diff,
                    @Param("backupPath") String backupPath,
                    @Param("now") OffsetDateTime now);

    @Update("UPDATE operations SET status = 'failed', error = #{error} WHERE id = #{operationId}")
    int markFailed(@Param("operationId") String operationId, @Param("error") String error);

    @Update("""
            UPDATE operations
            SET status = 'rejected', error = #{error}, resolved_at = #{now},
                resolved_by_user_id = #{userId}
            WHERE id = #{operationId}
            """)
    int markRejected(@Param("operationId") String operationId,
                     @Param("userId") String userId,
                     @Param("error") String error,
                     @Param("now") OffsetDateTime now);

    /** rebase / 摘要刷新：改写一条待确认提案的参数（链式 digest 或坐标位移）。 */
    @Update("""
            UPDATE operations
            SET arguments = #{arguments}, summary = #{summary}, diff = #{diff}
            WHERE id = #{operationId}
            """)
    int updateArguments(@Param("operationId") String operationId,
                        @Param("arguments") String arguments,
                        @Param("summary") String summary,
                        @Param("diff") String diff);
}
