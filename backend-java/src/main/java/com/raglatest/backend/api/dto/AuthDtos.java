package com.raglatest.backend.api.dto;

import com.raglatest.backend.identity.UserRecord;
import jakarta.validation.constraints.Email;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import java.time.Instant;

/**
 * 账号相关的请求/响应。命名策略同 {@link Dtos}（全局 SNAKE_CASE，见 application.yml），
 * 因此前端看到的是 {@code display_name} / {@code last_login_at}。
 */
public final class AuthDtos {

    private AuthDtos() {
    }

    public record LoginRequest(
            @NotBlank @Email @Size(max = 255) String email,
            @NotBlank @Size(max = 200) String password) {}

    /** 对外账号视图：绝不含 password_hash。 */
    public record UserRead(
            String id, String email, String displayName, String role, String status,
            Instant lastLoginAt, Instant createdAt) {

        public static UserRead from(UserRecord record) {
            return new UserRead(record.id(), record.email(), record.displayName(), record.role(),
                    record.status(), record.lastLoginAt(), record.createdAt());
        }
    }

    public record CreateUserRequest(
            @NotBlank @Email @Size(max = 255) String email,
            @NotBlank @Size(max = 100) String displayName,
            @NotBlank @Size(min = 8, max = 200) String password,
            @NotBlank @Pattern(regexp = "admin|member") String role) {}

    public record ResetPasswordRequest(
            @NotBlank @Size(min = 8, max = 200) String password) {}

    public record SetStatusRequest(
            @NotBlank @Pattern(regexp = "active|disabled") String status) {}

    public record SetRoleRequest(
            @NotBlank @Pattern(regexp = "admin|member") String role) {}

    /**
     * CSRF 令牌。前端在首次变更类请求前调一次，用来"催"出 XSRF-TOKEN Cookie
     * （令牌是延迟创建的，不取一次就不会写 Cookie）。
     */
    public record CsrfTokenRead(String token, String headerName) {}
}
