package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.AuthDtos.CreateUserRequest;
import com.raglatest.backend.api.dto.AuthDtos.ResetPasswordRequest;
import com.raglatest.backend.api.dto.AuthDtos.SetRoleRequest;
import com.raglatest.backend.api.dto.AuthDtos.SetStatusRequest;
import com.raglatest.backend.api.dto.AuthDtos.UserRead;
import com.raglatest.backend.identity.AccountService;
import com.raglatest.backend.security.CurrentUser;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 管理员专属的账号治理端点（URL 级鉴权：{@code /api/admin/**} 要求 ROLE_ADMIN）。
 *
 * <p>管理员的权限边界刻意收窄：只能管账号，看不到别人的工作区、文件与会话
 * ——"运维视角"（能读全员内容）需要单独的产品决策，不做默认放行。</p>
 */
@RestController
@RequestMapping("/api/admin/users")
public class AdminUserController {

    private final AccountService accounts;

    public AdminUserController(AccountService accounts) {
        this.accounts = accounts;
    }

    @GetMapping
    public List<UserRead> list() {
        return accounts.list().stream().map(UserRead::from).toList();
    }

    @PostMapping
    public ResponseEntity<UserRead> create(@Valid @RequestBody CreateUserRequest payload) {
        UserRead created = UserRead.from(accounts.create(
                payload.email(), payload.displayName(), payload.password(), payload.role()));
        return ResponseEntity.status(HttpStatus.CREATED).body(created);
    }

    @PostMapping("/{userId}/password")
    public UserRead resetPassword(@PathVariable String userId,
                                  @Valid @RequestBody ResetPasswordRequest payload) {
        return UserRead.from(accounts.resetPassword(userId, payload.password()));
    }

    @PostMapping("/{userId}/status")
    public UserRead setStatus(@PathVariable String userId,
                              @Valid @RequestBody SetStatusRequest payload) {
        return UserRead.from(accounts.setStatus(CurrentUser.id(), userId, payload.status()));
    }

    @PostMapping("/{userId}/role")
    public UserRead setRole(@PathVariable String userId,
                            @Valid @RequestBody SetRoleRequest payload) {
        return UserRead.from(accounts.setRole(CurrentUser.id(), userId, payload.role()));
    }
}
