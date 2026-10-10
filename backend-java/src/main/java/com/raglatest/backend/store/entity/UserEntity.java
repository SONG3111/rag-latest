package com.raglatest.backend.store.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import java.time.OffsetDateTime;

/**
 * users 表实体（MyBatis-Plus 映射）。
 *
 * <p>为什么这一张表用实体 + {@code BaseMapper}、其余表用手写 SQL 的 mapper：
 * 账号是最典型的单表 CRUD（建号/改密/停用/按邮箱查），{@code BaseMapper} 的
 * 主键查询与条件构造器正好覆盖；而工作区/分块/消息那些查询是带子查询、聚合与
 * 批量插入的报表式 SQL，注解 SQL 比构造器更直白也更容易审阅。</p>
 *
 * <p>时间字段用 {@link OffsetDateTime} 而不是 {@code Instant}：pgjdbc 对
 * {@code timestamptz} 的原生映射类型是 OffsetDateTime，交给 MyBatis 的
 * JSR-310 处理器转换零风险；Instant 的处理器依赖驱动对
 * {@code getObject(col, Instant.class)} 的支持，没必要赌。对外 DTO 仍是 Instant。</p>
 */
@TableName("users")
public class UserEntity {

    /** 32 位十六进制，由应用生成（与 workspace id 同形态），故是 INPUT 而非自增。 */
    @TableId(type = IdType.INPUT)
    private String id;

    private String email;

    private String displayName;

    private String passwordHash;

    /** admin | member */
    private String role;

    /** active | disabled */
    private String status;

    private OffsetDateTime lastLoginAt;

    private OffsetDateTime createdAt;

    private OffsetDateTime updatedAt;

    public String getId() {
        return id;
    }

    public void setId(String id) {
        this.id = id;
    }

    public String getEmail() {
        return email;
    }

    public void setEmail(String email) {
        this.email = email;
    }

    public String getDisplayName() {
        return displayName;
    }

    public void setDisplayName(String displayName) {
        this.displayName = displayName;
    }

    public String getPasswordHash() {
        return passwordHash;
    }

    public void setPasswordHash(String passwordHash) {
        this.passwordHash = passwordHash;
    }

    public String getRole() {
        return role;
    }

    public void setRole(String role) {
        this.role = role;
    }

    public String getStatus() {
        return status;
    }

    public void setStatus(String status) {
        this.status = status;
    }

    public OffsetDateTime getLastLoginAt() {
        return lastLoginAt;
    }

    public void setLastLoginAt(OffsetDateTime lastLoginAt) {
        this.lastLoginAt = lastLoginAt;
    }

    public OffsetDateTime getCreatedAt() {
        return createdAt;
    }

    public void setCreatedAt(OffsetDateTime createdAt) {
        this.createdAt = createdAt;
    }

    public OffsetDateTime getUpdatedAt() {
        return updatedAt;
    }

    public void setUpdatedAt(OffsetDateTime updatedAt) {
        this.updatedAt = updatedAt;
    }
}
