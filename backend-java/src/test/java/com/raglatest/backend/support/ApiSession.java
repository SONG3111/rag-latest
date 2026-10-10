package com.raglatest.backend.support;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpRequest;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.http.client.ClientHttpRequestExecution;
import org.springframework.http.client.ClientHttpRequestInterceptor;
import org.springframework.http.client.ClientHttpResponse;
import org.springframework.web.client.RestTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 测试用登录会话：一个透明的 Cookie 罐 + CSRF 头注入器。
 *
 * <p>做成 {@link ClientHttpRequestInterceptor} 而不是包装每个调用点，是为了让既有
 * 用例的 {@code rest.getForEntity(...)} 一行都不用改——那些断言是在"匿名可访问"
 * 的世界里写的，多用户改造后它们需要的是"已登录"，而不是重写测试意图。</p>
 *
 * <p>流程对齐真实前端：先 GET /api/auth/csrf 拿到令牌与 XSRF-TOKEN Cookie，再带着它
 * 登录（登录本身也是变更类请求，同样受 CSRF 保护——登录 CSRF 是真实攻击面）。</p>
 */
public final class ApiSession implements ClientHttpRequestInterceptor {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    /** 与 CookieCsrfTokenRepository 的默认 Cookie 名一致。 */
    private static final String CSRF_COOKIE = "XSRF-TOKEN";

    private final Map<String, String> cookies = new LinkedHashMap<>();
    private String csrfToken;
    private String csrfHeader = "X-XSRF-TOKEN";

    @Override
    public ClientHttpResponse intercept(HttpRequest request, byte[] body,
                                        ClientHttpRequestExecution execution) throws IOException {
        // /internal/** 没有会话可言，门槛是共享密钥（服务间调用的模拟）。
        if (request.getURI().getPath().startsWith("/internal/")) {
            request.getHeaders().set("X-Internal-Token", TestAccounts.INTERNAL_TOKEN);
        }
        if (!cookies.isEmpty()) {
            request.getHeaders().set(HttpHeaders.COOKIE, cookieHeader());
        }
        if (csrfToken != null && mutating(request.getMethod().name())) {
            request.getHeaders().set(csrfHeader, csrfToken);
        }
        ClientHttpResponse response = execution.execute(request, body);
        captureCookies(response);
        return response;
    }

    /**
     * 取一次令牌：写出 XSRF-TOKEN Cookie，并记下要回填的请求头值。
     *
     * <p>令牌必须取自 <b>Cookie</b>，不能用响应体里那个：请求属性里的令牌
     * （也就是端点返回的那个）是异或掩码后的值（防 BREACH），而服务端校验时拿
     * 它和 Cookie 里的原始值比对，掩码值必然对不上——这正是 SPA 配方的关键细节，
     * 前端也从 document.cookie 读同一个值。</p>
     */
    public void prime(RestTemplate rest) {
        ResponseEntity<String> response = rest.getForEntity("/api/auth/csrf", String.class);
        JsonNode body = read(response.getBody());
        if (body != null && body.hasNonNull("header_name")) {
            csrfHeader = body.get("header_name").asString();
        }
        csrfToken = cookies.get(CSRF_COOKIE);
        if (csrfToken == null && body != null) {
            // 兜底（理论上到不了）：Cookie 被拒时才退回响应体，让失败以 403 的形式
            // 显式暴露，而不是静默少一个头。
            csrfToken = body.path("token").asString(null);
        }
    }

    /** 登录；失败直接抛错（测试里"登录不了"应该立刻炸，而不是后面断言莫名 401）。 */
    public void login(RestTemplate rest, String email, String password) {
        prime(rest);
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        String payload = MAPPER.createObjectNode()
                .put("email", email)
                .put("password", password)
                .toString();
        ResponseEntity<String> response =
                rest.postForEntity("/api/auth/login", new HttpEntity<>(payload, headers), String.class);
        if (response.getStatusCode().value() != 200) {
            throw new IllegalStateException(
                    "测试登录失败：" + response.getStatusCode() + " " + response.getBody());
        }
    }

    public void logout(RestTemplate rest) {
        rest.postForEntity("/api/auth/logout", null, String.class);
        cookies.clear();
        csrfToken = null;
    }

    /** 当前 Cookie 头（供需要手工构造请求的用例复用，例如 CSRF 缺失场景）。 */
    public String cookieHeader() {
        StringBuilder header = new StringBuilder();
        cookies.forEach((name, value) -> {
            if (!header.isEmpty()) {
                header.append("; ");
            }
            header.append(name).append('=').append(value);
        });
        return header.toString();
    }

    private void captureCookies(ClientHttpResponse response) throws IOException {
        // 按名字取值而不是遍历 entrySet()：Servlet 的 HttpHeaders 在 Spring 7 里
        // 不再暴露 Map 视图，且 Set-Cookie 本来就是多值头。
        List<String> setCookies = response.getHeaders().get(HttpHeaders.SET_COOKIE);
        if (setCookies == null) {
            return;
        }
        for (String value : setCookies) {
            storeCookie(value);
        }
    }

    /** 只取 name=value；过期时间/Path/SameSite 这些属性对"回填 Cookie 头"没有意义。 */
    private void storeCookie(String setCookie) {
        int separator = setCookie.indexOf(';');
        String pair = (separator < 0 ? setCookie : setCookie.substring(0, separator)).strip();
        int equals = pair.indexOf('=');
        if (equals <= 0) {
            return;
        }
        cookies.put(pair.substring(0, equals), pair.substring(equals + 1));
    }

    private static boolean mutating(String method) {
        return "POST".equals(method) || "PUT".equals(method)
                || "PATCH".equals(method) || "DELETE".equals(method);
    }

    private static JsonNode read(String body) {
        if (body == null || body.isBlank()) {
            return null;
        }
        return MAPPER.readTree(body);
    }
}
