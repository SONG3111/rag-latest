package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * workspaces 的 SQL。所有查询都带 {@code owner_user_id}：这就是本轮的租户键，
 * 归属过滤放在 SQL 而不是"先查出来再在 Java 里筛"，避免出现忘记筛的调用点。
 */
@Mapper
public interface WorkspaceMapper {

    /**
     * 行记录用 {@link OffsetDateTime} 承接 timestamptz（pgjdbc 的原生映射类型），
     * 由 Repository 转成对外的 Instant —— 比赌驱动的 Instant 处理器稳。
     */
    record WorkspaceRow(
            String id,
            String name,
            String description,
            String ownerUserId,
            OffsetDateTime createdAt,
            OffsetDateTime updatedAt,
            int fileCount) {}

    String SELECT_WITH_COUNT = """
            SELECT w.id, w.name, w.description, w.owner_user_id, w.created_at, w.updated_at,
                   (SELECT COUNT(*) FROM document_files f WHERE f.workspace_id = w.id) AS file_count
            FROM workspaces w
            """;

    @Select(SELECT_WITH_COUNT + " WHERE w.owner_user_id = #{ownerUserId} ORDER BY w.created_at DESC")
    List<WorkspaceRow> listByOwner(@Param("ownerUserId") String ownerUserId);

    @Select(SELECT_WITH_COUNT + " WHERE w.id = #{id} AND w.owner_user_id = #{ownerUserId}")
    WorkspaceRow findOwned(@Param("id") String id, @Param("ownerUserId") String ownerUserId);

    /** 不带归属过滤的存在性判断：后台任务（索引恢复等）与内部端点使用。 */
    @Select("SELECT COUNT(*) FROM workspaces WHERE id = #{id}")
    long countById(@Param("id") String id);

    @Insert("""
            INSERT INTO workspaces (id, owner_user_id, name, description, created_at, updated_at)
            VALUES (#{id}, #{ownerUserId}, #{name}, #{description}, #{now}, #{now})
            """)
    int insert(@Param("id") String id,
               @Param("ownerUserId") String ownerUserId,
               @Param("name") String name,
               @Param("description") String description,
               @Param("now") OffsetDateTime now);

    /** 级联删除由外键 ON DELETE CASCADE 完成（files/chunks/messages/...）。 */
    @Delete("DELETE FROM workspaces WHERE id = #{id}")
    int delete(@Param("id") String id);
}
