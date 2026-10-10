package com.raglatest.backend.security;

import com.raglatest.backend.api.ApiException;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.context.SecurityContextHolder;

/**
 * 当前登录主体的读取入口。控制器一律经它拿 userId，而不是各自去翻
 * SecurityContextHolder——换认证方式时只有这一处需要改。
 */
public final class CurrentUser {

    private CurrentUser() {
    }

    public static AuthenticatedUser require() {
        Authentication authentication = SecurityContextHolder.getContext().getAuthentication();
        if (authentication == null || !(authentication.getPrincipal() instanceof AuthenticatedUser user)) {
            // 正常路径下 Security 的鉴权前置过滤器已经拦掉未登录请求；
            // 这里再兜一次是防止"某条路由被 permitAll 漏进业务代码"。
            throw new ApiException(HttpStatus.UNAUTHORIZED, "未登录");
        }
        return user;
    }

    public static String id() {
        return require().id();
    }
}
