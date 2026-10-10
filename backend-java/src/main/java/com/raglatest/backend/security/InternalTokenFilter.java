package com.raglatest.backend.security;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;

/**
 * {@code /internal/**} 的守门人：只放行携带正确 {@code X-Internal-Token} 的调用
 * （即 backend-java 自己，实际调用方是 ai-service 回读语料）。
 *
 * <p>这些端点没有会话可言（服务间调用），所以在授权规则里是 permitAll，唯一
 * 的门就是本过滤器——因此它必须同时覆盖"没有令牌"与"令牌不对"两种情况，
 * 且返回 401 而不是 404，便于排障时一眼看出是鉴权问题。</p>
 */
@Component
public class InternalTokenFilter extends OncePerRequestFilter {

    private static final String INTERNAL_PREFIX = "/internal/";

    private final InternalToken token;

    public InternalTokenFilter(InternalToken token) {
        this.token = token;
    }

    @Override
    protected boolean shouldNotFilter(HttpServletRequest request) {
        return !request.getRequestURI().startsWith(INTERNAL_PREFIX);
    }

    @Override
    protected void doFilterInternal(HttpServletRequest request, HttpServletResponse response,
                                    FilterChain chain) throws ServletException, IOException {
        if (!token.matches(request.getHeader(InternalToken.HEADER))) {
            response.setStatus(HttpStatus.UNAUTHORIZED.value());
            response.setContentType(MediaType.APPLICATION_JSON_VALUE);
            response.setCharacterEncoding("UTF-8");
            response.setHeader(HttpHeaders.CACHE_CONTROL, "no-store");
            response.getWriter().write("{\"detail\":\"internal token required\"}");
            return;
        }
        chain.doFilter(request, response);
    }
}
