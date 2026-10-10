package com.raglatest.backend.security;

import com.raglatest.backend.identity.UserRecord;
import com.raglatest.backend.identity.UserRepository;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;
import java.io.IOException;
import java.util.Optional;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * 会话里的账号状态保活检查：把"管理员刚把这个号停用了"这类变更立刻生效。
 *
 * <p>会话落在 Postgres（spring-session-jdbc），因此 DB 里的停用不会自动反映到
 * 已经发出的会话上——不检查的话，被停用的人可以一直用到会话过期。</p>
 *
 * <p>代价是每个 {@code /api/**} 请求多一次主键查询。账号规模（十人以内）下这是
 * 一次走索引的点查，相对于一次检索/一轮对话的开销可以忽略；只挂在 {@code /api/**}
 * 上，静态资源与 /health 不受影响。</p>
 */
@Component
public class AccountFreshnessFilter extends OncePerRequestFilter {

    private final UserRepository users;

    public AccountFreshnessFilter(UserRepository users) {
        this.users = users;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return !request.getRequestURI().startsWith("/api/");
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication != null && authentication.isAuthenticated()
                && authentication.getPrincipal() instanceof AuthenticatedUser current) {
            Optional<UserRecord> fresh = users.findById(current.id());
            if (fresh.isEmpty() || !fresh.get().isActive()) {
                HttpSession session = request.getSession(false);
                if (session != null) {
                    session.invalidate();
                }
                SecurityContextHolder.clearContext();
                JsonSecurityHandlers.write(response, HttpServletResponse.SC_UNAUTHORIZED,
                        "账号已被停用，请联系管理员");
                return;
            }
            UserRecord record = fresh.get();
            if (!record.role().equals(current.role())) {
                // 角色被改（升/降级）时立刻换掉权限，不必等重新登录。
                AuthenticatedUser updated = DbUserDetailsService.toPrincipal(record);
                SecurityContextHolder.getContext().setAuthentication(
                        new UsernamePasswordAuthenticationToken(
                                updated, null, updated.getAuthorities()));
            }
        }
        chain.doFilter(request, response);
    }
}
