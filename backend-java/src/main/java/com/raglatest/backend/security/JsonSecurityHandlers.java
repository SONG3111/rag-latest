package com.raglatest.backend.security;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.security.access.AccessDeniedException;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.web.AuthenticationEntryPoint;
import org.springframework.security.web.access.AccessDeniedHandler;
import tools.jackson.databind.node.JsonNodeFactory;

/**
 * 鉴权失败的响应形状与 {@code ApiExceptionHandler} 保持一致：{@code {"detail": "..."}}。
 * 前端的 request()/streamChat() 已经按这个形状解析错误，不能因为换了鉴权层就
 * 让 401/403 变成 Spring 默认的 HTML 或空体。
 */
final class JsonSecurityHandlers {

    private JsonSecurityHandlers() {
    }

    static void write(HttpServletResponse response, int status, String detail) throws IOException {
        response.setStatus(status);
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setCharacterEncoding(StandardCharsets.UTF_8.name());
        response.setHeader(HttpHeaders.CACHE_CONTROL, "no-store");
        response.getWriter().write(JsonNodeFactory.instance.objectNode().put("detail", detail).toString());
    }

    /** 未登录（或会话过期）：401 + 可读中文。 */
    static AuthenticationEntryPoint entryPoint() {
        return (HttpServletRequest request, HttpServletResponse response, AuthenticationException ex) ->
                write(response, HttpServletResponse.SC_UNAUTHORIZED, "请先登录");
    }

    /** 已登录但权限不足：403。 */
    static AccessDeniedHandler accessDeniedHandler() {
        return (HttpServletRequest request, HttpServletResponse response, AccessDeniedException ex) ->
                write(response, HttpServletResponse.SC_FORBIDDEN, "没有权限执行该操作");
    }
}
