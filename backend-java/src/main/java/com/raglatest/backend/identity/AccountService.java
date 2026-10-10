package com.raglatest.backend.identity;

import com.raglatest.backend.api.ApiException;
import com.raglatest.backend.identity.UserRecord;
import java.util.List;
import java.util.Locale;
import org.springframework.http.HttpStatus;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.stereotype.Service;

/**
 * 账号治理：建号、改密、启停与升降级，外加两条"不要把系统锁死"的不变量。
 *
 * <p>没有自助注册：账号只由管理员创建（或首启引导创建）。这既符合"内部工具"的
 * 使用场景，也让邀请/审批之类的产品决策不必现在做。</p>
 */
@Service
public class AccountService {

    /** 密码下限：8 位（与常见内部系统的默认要求一致，不做复杂度强制）。 */
    public static final int MIN_PASSWORD_LENGTH = 8;

    private final UserRepository users;
    private final PasswordEncoder encoder;

    public AccountService(UserRepository users, PasswordEncoder encoder) {
        this.users = users;
        this.encoder = encoder;
    }

    public List<UserRecord> list() {
        return users.listAll();
    }

    public UserRecord create(String email, String displayName, String rawPassword, String role) {
        String normalizedEmail = normalizeEmail(email);
        requireValidRole(role);
        requirePassword(rawPassword);
        if (users.findByEmail(normalizedEmail).isPresent()) {
            throw new ApiException(HttpStatus.CONFLICT, "该邮箱已存在");
        }
        return users.create(normalizedEmail, displayName.strip(), encoder.encode(rawPassword), role);
    }

    public UserRecord resetPassword(String userId, String rawPassword) {
        UserRecord user = users.findById(userId)
                .orElseThrow(() -> ApiException.notFound("账号不存在"));
        requirePassword(rawPassword);
        users.updatePassword(user.id(), encoder.encode(rawPassword));
        return users.findById(user.id()).orElseThrow();
    }

    /**
     * 启停账号。两条护栏都在这里：不能停用自己（否则一击就把自己关在门外），
     * 也不能停用最后一个可用管理员（否则没人能再建号/解锁）。
     */
    public UserRecord setStatus(String operatorId, String userId, String status) {
        if (!UserRecord.STATUS_ACTIVE.equals(status) && !UserRecord.STATUS_DISABLED.equals(status)) {
            throw ApiException.badRequest("status 只能是 active 或 disabled");
        }
        UserRecord user = users.findById(userId)
                .orElseThrow(() -> ApiException.notFound("账号不存在"));
        if (UserRecord.STATUS_DISABLED.equals(status) && user.id().equals(operatorId)) {
            throw new ApiException(HttpStatus.CONFLICT, "不能停用自己的账号");
        }
        if (UserRecord.STATUS_DISABLED.equals(status) && user.isAdmin() && user.isActive()
                && users.countOtherActiveAdmins(user.id()) == 0) {
            throw new ApiException(HttpStatus.CONFLICT, "至少要保留一个可用的管理员");
        }
        users.updateStatus(user.id(), status);
        return users.findById(user.id()).orElseThrow();
    }

    /** 升降级。降级受与停用相同的"最后一个管理员"保护。 */
    public UserRecord setRole(String operatorId, String userId, String role) {
        requireValidRole(role);
        UserRecord user = users.findById(userId)
                .orElseThrow(() -> ApiException.notFound("账号不存在"));
        boolean demoting = user.isAdmin() && !UserRecord.ROLE_ADMIN.equals(role);
        if (demoting && user.id().equals(operatorId)) {
            throw new ApiException(HttpStatus.CONFLICT, "不能改自己的角色");
        }
        if (demoting && user.isActive() && users.countOtherActiveAdmins(user.id()) == 0) {
            throw new ApiException(HttpStatus.CONFLICT, "至少要保留一个可用的管理员");
        }
        users.updateRole(user.id(), role);
        return users.findById(user.id()).orElseThrow();
    }

    public void touchLastLogin(String userId) {
        users.touchLastLogin(userId);
    }

    /** 邮箱统一小写入库：与 users 上的 lower(email) 唯一索引保持同一口径。 */
    public static String normalizeEmail(String email) {
        return email == null ? "" : email.strip().toLowerCase(Locale.ROOT);
    }

    private static void requireValidRole(String role) {
        if (!UserRecord.ROLE_ADMIN.equals(role) && !UserRecord.ROLE_MEMBER.equals(role)) {
            throw ApiException.badRequest("role 只能是 admin 或 member");
        }
    }

    private static void requirePassword(String rawPassword) {
        if (rawPassword == null || rawPassword.length() < MIN_PASSWORD_LENGTH) {
            throw ApiException.badRequest("密码至少 " + MIN_PASSWORD_LENGTH + " 位");
        }
    }
}
