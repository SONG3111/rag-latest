package com.raglatest.backend.security;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.util.function.Supplier;
import org.springframework.security.web.csrf.CsrfToken;
import org.springframework.security.web.csrf.CsrfTokenRequestAttributeHandler;
import org.springframework.security.web.csrf.CsrfTokenRequestHandler;
import org.springframework.security.web.csrf.XorCsrfTokenRequestAttributeHandler;
import org.springframework.util.StringUtils;

/**
 * 单页应用的 CSRF 令牌解析策略。
 *
 * <p>逐字迁移自 Spring Security 官方参考文档「Integrating with CSRF Protection /
 * Single Page Applications」给出的 {@code SpaCsrfTokenRequestHandler} 示例：
 * {@code XorCsrfTokenRequestAttributeHandler} 会把令牌异或掩码后写进请求属性
 * （防 BREACH），但 SPA 拿到的是<b>未掩码</b>的 Cookie 值，用它去比对必然失败。
 * 因此：渲染/写入走异或版，从请求头（{@code X-XSRF-TOKEN}）取值校验时走朴素版。</p>
 *
 * <p>为什么必须这么做而不是直接关掉 CSRF：聊天是 POST + 流式响应，无法用
 * {@code EventSource}（只能是 GET），会话又靠 Cookie 维持，两条加起来正好是
 * CSRF 的标准攻击面。</p>
 */
final class SpaCsrfTokenRequestHandler implements CsrfTokenRequestHandler {

    private final CsrfTokenRequestHandler plain = new CsrfTokenRequestAttributeHandler();
    private final CsrfTokenRequestHandler xor = new XorCsrfTokenRequestAttributeHandler();

    @Override
    public void handle(HttpServletRequest request, HttpServletResponse response,
                       Supplier<CsrfToken> csrfToken) {
        this.xor.handle(request, response, csrfToken);
        // 令牌是延迟创建的：不主动取一次，CookieCsrfTokenRepository 就不会写 Cookie，
        // 前端永远等不到 XSRF-TOKEN。官方文档同样在此处调用 csrfToken.get()。
        csrfToken.get();
    }

    @Override
    public String resolveCsrfTokenValue(HttpServletRequest request, CsrfToken csrfToken) {
        String headerValue = request.getHeader(csrfToken.getHeaderName());
        return (StringUtils.hasText(headerValue) ? this.plain : this.xor)
                .resolveCsrfTokenValue(request, csrfToken);
    }
}
