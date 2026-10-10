package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Delete;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/** document_files 的 SQL（含索引状态机的四条状态迁移）。 */
@Mapper
public interface DocumentFileMapper {

    record FileRow(
            String id,
            String workspaceId,
            String relPath,
            String kind,
            long sizeBytes,
            String checksum,
            String status,
            int chunkCount,
            String error,
            OffsetDateTime indexedAt,
            OffsetDateTime createdAt) {}

    /** 启动恢复用：跨工作区列出处于给定状态的文件。 */
    record FileStuckRow(String workspaceId, String id, String relPath) {}

    String COLUMNS = """
            SELECT id, workspace_id, rel_path, kind, size_bytes, checksum,
                   status, chunk_count, error, indexed_at, created_at
            FROM document_files
            """;

    @Select(COLUMNS + " WHERE workspace_id = #{workspaceId} ORDER BY created_at ASC")
    List<FileRow> listByWorkspace(@Param("workspaceId") String workspaceId);

    @Select(COLUMNS + " WHERE id = #{fileId} AND workspace_id = #{workspaceId}")
    FileRow findInWorkspace(@Param("workspaceId") String workspaceId, @Param("fileId") String fileId);

    @Select(COLUMNS + " WHERE workspace_id = #{workspaceId} AND rel_path = #{relPath}")
    FileRow findByRelPath(@Param("workspaceId") String workspaceId, @Param("relPath") String relPath);

    @Select("""
            <script>
            SELECT workspace_id, id, rel_path FROM document_files
            WHERE status IN
            <foreach collection="statuses" item="s" open="(" separator="," close=")">#{s}</foreach>
            </script>
            """)
    List<FileStuckRow> findByStatuses(@Param("statuses") List<String> statuses);

    @Insert("""
            INSERT INTO document_files
                (id, workspace_id, rel_path, kind, size_bytes, checksum, status, chunk_count, created_at)
            VALUES (#{id}, #{workspaceId}, #{relPath}, #{kind}, #{sizeBytes}, #{checksum},
                    'pending', 0, #{now})
            """)
    int insertPending(@Param("id") String id,
                      @Param("workspaceId") String workspaceId,
                      @Param("relPath") String relPath,
                      @Param("kind") String kind,
                      @Param("sizeBytes") long sizeBytes,
                      @Param("checksum") String checksum,
                      @Param("now") OffsetDateTime now);

    @Delete("DELETE FROM document_files WHERE id = #{fileId}")
    int delete(@Param("fileId") String fileId);

    @Update("UPDATE document_files SET status = 'indexing' WHERE id = #{fileId}")
    int markIndexing(@Param("fileId") String fileId);

    @Update("""
            UPDATE document_files
            SET status = 'indexed', chunk_count = #{chunkCount}, checksum = #{checksum},
                error = NULL, indexed_at = #{now}
            WHERE id = #{fileId}
            """)
    int markIndexed(@Param("fileId") String fileId,
                    @Param("chunkCount") int chunkCount,
                    @Param("checksum") String checksum,
                    @Param("now") OffsetDateTime now);

    @Update("UPDATE document_files SET status = 'failed', error = #{error} WHERE id = #{fileId}")
    int markFailed(@Param("fileId") String fileId, @Param("error") String error);
}
