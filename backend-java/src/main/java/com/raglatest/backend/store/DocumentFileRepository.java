package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.FileRead;
import java.sql.Timestamp;
import java.util.List;
import java.util.Optional;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class DocumentFileRepository {

    private final JdbcClient jdbc;

    public DocumentFileRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    private static final String SELECT = """
            SELECT id, workspace_id, rel_path, kind, size_bytes, checksum,
                   status, chunk_count, error, indexed_at, created_at
            FROM document_files
            """;

    public List<FileRead> listByWorkspace(String workspaceId) {
        return jdbc.sql(SELECT + " WHERE workspace_id = :ws ORDER BY created_at ASC")
                .param("ws", workspaceId)
                .query(this::mapRow)
                .list();
    }

    public Optional<FileRead> findInWorkspace(String workspaceId, String fileId) {
        return jdbc.sql(SELECT + " WHERE id = :id AND workspace_id = :ws")
                .param("id", fileId)
                .param("ws", workspaceId)
                .query(this::mapRow)
                .optional();
    }

    /** 按相对路径找文件：审批写入/还原后的后台重建索引用它定位已登记的文件。 */
    public Optional<FileRead> findByRelPath(String workspaceId, String relPath) {
        return jdbc.sql(SELECT + " WHERE workspace_id = :ws AND rel_path = :relPath")
                .param("ws", workspaceId)
                .param("relPath", relPath)
                .query(this::mapRow)
                .optional();
    }

    public FileRead insertPending(String workspaceId, String relPath, String kind,
                                  long sizeBytes, String checksum) {
        String id = WorkspaceRepository.newId();
        Timestamp now = Timestamp.from(java.time.Instant.now());
        jdbc.sql("""
                INSERT INTO document_files
                    (id, workspace_id, rel_path, kind, size_bytes, checksum, status,
                     chunk_count, created_at)
                VALUES (:id, :ws, :relPath, :kind, :size, :checksum, 'pending', 0, :created)
                """)
                .param("id", id)
                .param("ws", workspaceId)
                .param("relPath", relPath)
                .param("kind", kind)
                .param("size", sizeBytes)
                .param("checksum", checksum)
                .param("created", now)
                .update();
        return new FileRead(id, relPath, kind, sizeBytes,
                "pending", 0, null, null, now.toInstant());
    }

    /** chunks 行经 document_files 的 ON DELETE CASCADE 一并清掉。 */
    public void delete(String fileId) {
        jdbc.sql("DELETE FROM document_files WHERE id = :id").param("id", fileId).update();
    }

    /** 索引成功：写入分块数、文件指纹与完成时间，清空错误。 */
    public void markIndexed(String fileId, int chunkCount, String checksum) {
        jdbc.sql("""
                        UPDATE document_files
                        SET status = 'indexed', chunk_count = :count, checksum = :checksum,
                            error = NULL, indexed_at = :at
                        WHERE id = :id
                        """)
                .param("count", chunkCount)
                .param("checksum", checksum == null ? "" : checksum)
                .param("at", Timestamp.from(java.time.Instant.now()))
                .param("id", fileId)
                .update();
    }

    /** 索引失败：记录状态与可读原因，保留文件以便重试。 */
    public void markFailed(String fileId, String error) {
        jdbc.sql("UPDATE document_files SET status = 'failed', error = :error WHERE id = :id")
                .param("error", error)
                .param("id", fileId)
                .update();
    }

    private FileRead mapRow(java.sql.ResultSet rs, int i) throws java.sql.SQLException {
        return new FileRead(
                rs.getString("id"),
                rs.getString("rel_path"),
                rs.getString("kind"),
                rs.getLong("size_bytes"),
                rs.getString("status"),
                rs.getInt("chunk_count"),
                rs.getString("error"),
                WorkspaceRepository.toInstant(rs.getTimestamp("indexed_at")),
                WorkspaceRepository.toInstant(rs.getTimestamp("created_at")));
    }
}
