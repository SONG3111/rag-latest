package com.raglatest.backend.identity;

import com.raglatest.backend.store.WorkspaceRepository;
import com.raglatest.backend.store.entity.UserEntity;
import com.raglatest.backend.store.mapper.UserMapper;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Optional;
import org.springframework.stereotype.Repository;

/**
 * users 表的读写。CRUD 走 MyBatis-Plus 的 {@code BaseMapper}（单表账号是最典型的
 * 适用场景），唯一性/最后管理员这类判定走注解 SQL。
 *
 * <p>密码只在调用方（{@link AccountService}）加解密，本类只搬运哈希字符串——
 * 仓储不认识明文，避免"某天有人在这里顺手打了个日志"。</p>
 */
@Repository
public class UserRepository {

    private final UserMapper mapper;

    public UserRepository(UserMapper mapper) {
        this.mapper = mapper;
    }

    public Optional<UserRecord> findByEmail(String email) {
        return Optional.ofNullable(mapper.findByEmail(email)).map(UserRepository::toRecord);
    }

    public Optional<UserRecord> findById(String id) {
        return Optional.ofNullable(mapper.selectById(id)).map(UserRepository::toRecord);
    }

    public List<UserRecord> listAll() {
        return mapper.listAll().stream().map(UserRepository::toRecord).toList();
    }

    public boolean isEmpty() {
        // 用显式 SQL 而不是 BaseMapper.selectCount(null)：后者的空 wrapper 语义
        // 在 MyBatis-Plus 各版本间有过改动，账号计数这种"必须永远简单"的查询
        // 不值得跟着外部库的语义漂移走。
        return mapper.countAll() == 0L;
    }

    public UserRecord create(String email, String displayName, String passwordHash, String role) {
        String id = WorkspaceRepository.newId();
        OffsetDateTime now = OffsetDateTime.now();
        UserEntity entity = new UserEntity();
        entity.setId(id);
        entity.setEmail(email);
        entity.setDisplayName(displayName);
        entity.setPasswordHash(passwordHash);
        entity.setRole(role);
        entity.setStatus(UserRecord.STATUS_ACTIVE);
        entity.setCreatedAt(now);
        entity.setUpdatedAt(now);
        mapper.insert(entity);
        return toRecord(entity);
    }

    public void updatePassword(String id, String passwordHash) {
        UserEntity patch = new UserEntity();
        patch.setId(id);
        patch.setPasswordHash(passwordHash);
        patch.setUpdatedAt(OffsetDateTime.now());
        mapper.updateById(patch);
    }

    public void updateStatus(String id, String status) {
        UserEntity patch = new UserEntity();
        patch.setId(id);
        patch.setStatus(status);
        patch.setUpdatedAt(OffsetDateTime.now());
        mapper.updateById(patch);
    }

    public void updateRole(String id, String role) {
        UserEntity patch = new UserEntity();
        patch.setId(id);
        patch.setRole(role);
        patch.setUpdatedAt(OffsetDateTime.now());
        mapper.updateById(patch);
    }

    public void touchLastLogin(String id) {
        mapper.touchLastLogin(id);
    }

    public long countOtherActiveAdmins(String exceptUserId) {
        return mapper.countOtherActiveAdmins(exceptUserId);
    }

    private static UserRecord toRecord(UserEntity entity) {
        return new UserRecord(
                entity.getId(),
                entity.getEmail(),
                entity.getDisplayName(),
                entity.getPasswordHash(),
                entity.getRole(),
                entity.getStatus(),
                toInstant(entity.getLastLoginAt()),
                toInstant(entity.getCreatedAt()),
                toInstant(entity.getUpdatedAt()));
    }

    private static Instant toInstant(OffsetDateTime value) {
        return value == null ? null : value.toInstant();
    }
}
