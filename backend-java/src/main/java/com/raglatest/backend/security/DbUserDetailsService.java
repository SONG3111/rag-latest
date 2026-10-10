package com.raglatest.backend.security;

import com.raglatest.backend.identity.UserRecord;
import com.raglatest.backend.identity.UserRepository;
import org.springframework.security.core.userdetails.UserDetails;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.security.core.userdetails.UsernameNotFoundException;
import org.springframework.stereotype.Service;

/**
 * 从 users 表装载登录主体。登录按邮箱（大小写不敏感，见 users 的 lower(email) 唯一索引）。
 */
@Service
public class DbUserDetailsService implements UserDetailsService {

    private final UserRepository users;

    public DbUserDetailsService(UserRepository users) {
        this.users = users;
    }

    @Override
    public UserDetails loadUserByUsername(String email) throws UsernameNotFoundException {
        UserRecord record = users.findByEmail(email)
                .orElseThrow(() -> new UsernameNotFoundException("account not found"));
        return toPrincipal(record);
    }

    static AuthenticatedUser toPrincipal(UserRecord record) {
        return new AuthenticatedUser(record.id(), record.email(), record.displayName(),
                record.role(), record.status(), record.passwordHash());
    }
}
