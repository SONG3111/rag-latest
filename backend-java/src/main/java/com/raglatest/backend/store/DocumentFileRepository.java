package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.store.mapper.DocumentFileMapper;
import com.raglatest.backend.store.mapper.DocumentFileMapper.FileRow;
import com.raglatest.backend.store.mapper.DocumentFileMapper.FileStuckRow;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Repository;

/**
 * 工作区文件登记表。SQL 在 {@link DocumentFileMapper}；本类做行 → DTO 映射与
 * 索引状态机的语义封装（pending → indexing → indexed / failed）。
 */
@Repository
public class DocumentFileRepository {

    private final DocumentFileMapper mapper;

    public DocumentFileRepository(DocumentFileMapper mapper) {
        this.mapper = mapper;
    }

    public List<FileRead> listByWorkspace(String workspaceId) {
        return mapper.listByWorkspace(workspaceId).stream().map(DocumentFileRepository::toRead).toList();
    }

    public Optional<FileRead> findInWorkspace(String workspaceId, String fileId) {
        return Optional.ofNullable(mapper.findInWorkspace(workspaceId, fileId))
                .map(DocumentFileRepository::toRead);
    }

    /** 按相对路径找文件：审批写入/还原后的后台重建索引用它定位已登记的文件。 */
    public Optional<FileRead> findByRelPath(String workspaceId, String relPath) {
        return Optional.ofNullable(mapper.findByRelPath(workspaceId, relPath))
                .map(DocumentFileRepository::toRead);
    }

    /** 启动恢复用：跨工作区列出处于给定状态的文件（FileRead 不含 workspace_id）。 */
    public List<FileStuck> findByStatuses(String... statuses) {
        return mapper.findByStatuses(List.of(statuses)).stream()
                .map(DocumentFileRepository::toStuck)
                .toList();
    }

    public record FileStuck(String workspaceId, String id, String relPath) {}

    public FileRead insertPending(String workspaceId, String relPath, String kind,
                                  long sizeBytes, String checksum) {
        String id = WorkspaceRepository.newId();
        OffsetDateTime now = OffsetDateTime.now();
        mapper.insertPending(id, workspaceId, relPath, kind, sizeBytes, checksum, now);
        return new FileRead(id, relPath, kind, sizeBytes,
                "pending", 0, null, null, now.toInstant());
    }

    /** chunks 行经 document_files 的 ON DELETE CASCADE 一并清掉。 */
    public void delete(String fileId) {
        mapper.delete(fileId);
    }

    /** 写入/还原已落盘但后台重建未跑完：先同步标 indexing，侧栏立刻可见。 */
    public void markIndexing(String fileId) {
        mapper.markIndexing(fileId);
    }

    /** 索引成功：写入分块数、文件指纹与完成时间，清空错误。 */
    public void markIndexed(String fileId, int chunkCount, String checksum) {
        mapper.markIndexed(fileId, chunkCount, checksum == null ? "" : checksum,
                OffsetDateTime.now());
    }

    /** 索引失败：记录状态与可读原因，保留文件以便重试。 */
    public void markFailed(String fileId, String error) {
        mapper.markFailed(fileId, error);
    }

    private static FileRead toRead(FileRow row) {
        return new FileRead(
                row.id(),
                row.relPath(),
                row.kind(),
                row.sizeBytes(),
                row.status(),
                row.chunkCount(),
                row.error(),
                WorkspaceRepository.toInstant(row.indexedAt()),
                WorkspaceRepository.toInstant(row.createdAt()));
    }

    private static FileStuck toStuck(FileStuckRow row) {
        return new FileStuck(row.workspaceId(), row.id(), row.relPath());
    }
}
