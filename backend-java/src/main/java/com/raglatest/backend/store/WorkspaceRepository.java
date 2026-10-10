package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.WorkspaceRead;
import com.raglatest.backend.store.mapper.WorkspaceMapper;
import com.raglatest.backend.store.mapper.WorkspaceMapper.WorkspaceRow;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.stereotype.Repository;

/**
 * 工作区读写。SQL 在 {@link WorkspaceMapper}，本类是领域语义层：对外返回 DTO、
 * 把 timestamptz 的 OffsetDateTime 归一到 Instant。
 *
 * <p>归属（owner_user_id）是唯一的租户键，因此列表与详情查询都强制带 owner 参数
 * ——没有"不带 owner 的 find"，避免调用方漏筛。后台任务与内部端点走
 * {@link #exists(String)}。</p>
 */
@Repository
public class WorkspaceRepository {

    /** 与 python uuid4().hex 同形态的 32 位十六进制 id。 */
    public static String newId() {
        return UUID.randomUUID().toString().replace("-", "");
    }

    private final WorkspaceMapper mapper;

    public WorkspaceRepository(WorkspaceMapper mapper) {
        this.mapper = mapper;
    }

    /** 当前用户可见的工作区（多用户下的隔离点：SQL 里就带 owner 条件）。 */
    public List<WorkspaceRead> listOwnedBy(String ownerUserId) {
        return mapper.listByOwner(ownerUserId).stream().map(WorkspaceRepository::toRead).toList();
    }

    /** 归属校验后的详情：不属于该用户时返回 empty（对外表现为 404，不暴露存在性）。 */
    public Optional<WorkspaceRead> findOwned(String workspaceId, String ownerUserId) {
        return Optional.ofNullable(mapper.findOwned(workspaceId, ownerUserId))
                .map(WorkspaceRepository::toRead);
    }

    /** 不带归属的存在性判断：内部端点与后台任务使用。 */
    public boolean exists(String workspaceId) {
        return mapper.countById(workspaceId) > 0;
    }

    public WorkspaceRead create(String name, String description, String ownerUserId) {
        String id = newId();
        Instant now = Instant.now();
        mapper.insert(id, ownerUserId, name, description, OffsetDateTime.now());
        return new WorkspaceRead(id, name, description, now, now, 0);
    }

    /** 级联删除由 V1 迁移里的 ON DELETE CASCADE 承担（files/chunks/messages/...）。 */
    public void delete(String workspaceId) {
        // 删除按 id 直接执行：调用方（WorkspaceController）已经过了归属校验。
        // 不在此处再查一次 owner，是为了让"校验在 Web 层"这条边界保持单一。
        mapper.delete(workspaceId);
    }

    private static WorkspaceRead toRead(WorkspaceRow row) {
        return new WorkspaceRead(row.id(), row.name(), row.description(),
                toInstant(row.createdAt()), toInstant(row.updatedAt()), row.fileCount());
    }

    static Instant toInstant(OffsetDateTime timestamp) {
        return timestamp == null ? null : timestamp.toInstant();
    }
}
