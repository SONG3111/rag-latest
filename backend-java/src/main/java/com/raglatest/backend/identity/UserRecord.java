package com.raglatest.backend.identity;

import java.time.Instant;

/**
 * 账号的领域视图（与 users 表一一对应）。
 *
 * <p>角色语义参考 Dify 的 {@code TenantAccountRole}（Apache-2.0）：那边把
 * owner/admin/editor/normal/dataset_operator 挂在「账号 ↔ 工作区」的关联表上。
 * 本项目当前只做单层隔离（一个工作区一个 owner），所以角色砍到平台两级
 * （admin/member），工作区归属落在 {@code workspaces.owner_user_id}；等做共享
 * 工作区时，把 owner/editor/viewer 挂到成员表上即可，user 行不用动。</p>
 */
public record UserRecord(
        String id,
        String email,
        String displayName,
        String passwordHash,
        String role,
        String status,
        Instant lastLoginAt,
        Instant createdAt,
        Instant updatedAt) {

    public static final String ROLE_ADMIN = "admin";
    public static final String ROLE_MEMBER = "member";
    public static final String STATUS_ACTIVE = "active";
    public static final String STATUS_DISABLED = "disabled";

    public boolean isAdmin() {
        return ROLE_ADMIN.equals(role);
    }

    public boolean isActive() {
        return STATUS_ACTIVE.equals(status);
    }
}
