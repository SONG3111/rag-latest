package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;

/**
 * chunks 的 SQL：检索语料全量读（/internal 端点）与 reindex 的"删旧插新"。
 *
 * <p>JSON 载荷列（meta / token_counts）在 mapper 层是 String，由 Repository 用
 * Jackson 解析成 JsonNode —— 不注册 JsonNode TypeHandler，因为 MyBatis-Plus 自带的
 * {@code JacksonTypeHandler} 绑 Jackson 2，而本项目（Boot 4）用的是 Jackson 3。</p>
 */
@Mapper
public interface ChunkMapper {

    record FileIdPathRow(String id, String relPath) {}

    record ChunkRow(
            String id,
            String fileId,
            String parentId,
            String level,
            String text,
            String location,
            String meta,
            String tokenCounts,
            int tokenLength,
            int ordinal) {}

    record ChunkInsert(
            String id,
            String workspaceId,
            String fileId,
            String parentId,
            String level,
            int ordinal,
            String text,
            String location,
            String meta,
            String tokenCounts,
            int tokenLength,
            OffsetDateTime createdAt) {}

    @Select("SELECT id, rel_path FROM document_files WHERE workspace_id = #{workspaceId}")
    List<FileIdPathRow> listFilePaths(@Param("workspaceId") String workspaceId);

    @Select("""
            SELECT id, file_id, parent_id, level, text, location,
                   meta, token_counts, token_length, ordinal
            FROM chunks WHERE workspace_id = #{workspaceId}
            ORDER BY ordinal ASC, created_at ASC, id ASC
            """)
    List<ChunkRow> loadChunks(@Param("workspaceId") String workspaceId);

    @Delete("DELETE FROM chunks WHERE file_id = #{fileId}")
    int deleteByFile(@Param("fileId") String fileId);

    /**
     * 批量插入。调用方负责分批（Postgres 单语句参数上限 65535，12 列 × 5000 行
     * 就会撞线），见 {@code ChunkRepository.replaceFileChunks}。
     */
    @Insert("""
            <script>
            INSERT INTO chunks
                (id, workspace_id, file_id, parent_id, level, ordinal, text,
                 location, meta, token_counts, token_length, created_at)
            VALUES
            <foreach collection="rows" item="r" separator=",">
                (#{r.id}, #{r.workspaceId}, #{r.fileId}, #{r.parentId}, #{r.level},
                 #{r.ordinal}, #{r.text}, #{r.location}, #{r.meta}, #{r.tokenCounts},
                 #{r.tokenLength}, #{r.createdAt})
            </foreach>
            </script>
            """)
    int insertChunks(@Param("rows") List<ChunkInsert> rows);
}
