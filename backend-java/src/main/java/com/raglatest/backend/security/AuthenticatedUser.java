package com.raglatest.backend.security;

import java.io.Serial;
import java.io.Serializable;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import org.springframework.security.core.CredentialsContainer;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.userdetails.UserDetails;

/**
 * 登录主体。既是 Spring Security 的 {@link UserDetails}，也是存进会话的那个对象
 * （spring-session-jdbc 用 Java 序列化落地，因此必须 Serializable）。
 *
 * <p>实现 {@link CredentialsContainer#eraseCredentials()}：认证成功后 ProviderManager
 * 会把密码哈希从主体里擦掉，避免 bcrypt 串被序列化进 SPRING_SESSION_ATTRIBUTES
 * ——会话表里不需要有凭据副本。</p>
 */
public final class AuthenticatedUser implements UserDetails, CredentialsContainer, Serializable {

    @Serial
    private static final long serialVersionUID = 1L;

    private final String id;
    private final String email;
    private final String displayName;
    private final String role;
    private final String status;
    private transient String passwordHash;

    public AuthenticatedUser(String id, String email, String displayName, String role,
                             String status, String passwordHash) {
        this.id = id;
        this.email = email;
        this.displayName = displayName;
        this.role = role;
        this.status = status;
        this.passwordHash = passwordHash;
    }

    public String id() {
        return id;
    }

    public String email() {
        return email;
    }

    public String displayName() {
        return displayName;
    }

    public String role() {
        return role;
    }

    public String status() {
        return status;
    }

    public boolean isAdmin() {
        return "admin".equals(role);
    }

    @Override
    public Collection<? extends GrantedAuthority> getAuthorities() {
        // 角色名转成 Spring Security 的 ROLE_ 前缀约定：hasRole("ADMIN") 匹配 ROLE_ADMIN。
        return List.of(new SimpleGrantedAuthority("ROLE_" + role.toUpperCase(Locale.ROOT)));
    }

    @Override
    public String getPassword() {
        return passwordHash;
    }

    @Override
    public String getUsername() {
        return email;
    }

    @Override
    public boolean isAccountNonExpired() {
        return true;
    }

    @Override
    public boolean isAccountNonLocked() {
        return true;
    }

    @Override
    public boolean isCredentialsNonExpired() {
        return true;
    }

    /** 停用的账号在认证阶段就会被拒（DisabledException → 401）。 */
    @Override
    public boolean isEnabled() {
        return "active".equals(status);
    }

    @Override
    public void eraseCredentials() {
        this.passwordHash = null;
    }
}
