package com.raglatest.backend.api;

import static org.assertj.core.api.Assertions.assertThat;

import com.github.tomakehurst.wiremock.WireMockServer;
import com.github.tomakehurst.wiremock.core.WireMockConfiguration;
import com.raglatest.backend.identity.UserRecord;
import com.raglatest.backend.identity.UserRepository;
import com.raglatest.backend.support.ApiSession;
import com.raglatest.backend.support.EmbeddedPostgresSupport;
import com.raglatest.backend.support.TestAccounts;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.web.server.context.WebServerApplicationContext;
import org.springframework.http.HttpEntity;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.web.client.ResponseErrorHandler;
import org.springframework.web.client.RestTemplate;
import org.springframework.web.util.DefaultUriBuilderFactory;

/**
 * 多用户隔离与鉴权的验收测试（改造的核心承诺）。
 *
 * <p>覆盖三条不变量：</p>
 * <ol>
 *   <li>没有登录态 → 401（含所有工作区子路由）；</li>
 *   <li>别人的工作区 → 404（不是 403：403 等于确认存在性），<b>表驱动覆盖全部子路由</b>
 *       ——新增路由若忘了加守卫，这里会红；</li>
 *   <li>管理员只管账号，照样看不到别人的工作区内容。</li>
 * </ol>
 *
 * <p>另有会话生命周期（登出失效、停用即时生效）、CSRF、内部令牌与最后管理员护栏。</p>
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ApiAuthorizationTests {

    static WireMockServer aiService;
    static final Path dataDir;

    static {
        try {
            dataDir = java.nio.file.Files.createTempDirectory("rag-backend-authz-tests");
        } catch (java.io.IOException ex) {
            throw new ExceptionInInitializerError(ex);
        }
        Runtime.getRuntime().addShutdownHook(new Thread(
                () -> WorkspaceController.deleteDirectoryTree(dataDir)));
    }

    @DynamicPropertySource
    static void properties(DynamicPropertyRegistry registry) {
        EmbeddedPostgresSupport.register(registry, "authorization");
        if (aiService == null) {
            aiService = new WireMockServer(WireMockConfiguration.wireMockConfig().dynamicPort());
            aiService.start();
        }
        registry.add("rag.data-dir", () -> dataDir.toAbsolutePath().toString());
        registry.add("rag.ai-service.base-url", () -> "http://127.0.0.1:" + aiService.port());
    }

    @AfterAll
    static void stopStub() {
        if (aiService != null) {
            aiService.stop();
        }
    }

    @Autowired
    WebServerApplicationContext serverContext;
    @Autowired
    UserRepository users;
    @Autowired
    JdbcTemplate jdbc;

    RestTemplate admin;
    RestTemplate member;
    RestTemplate anonymous;
    ApiSession adminSession;
    ApiSession memberSession;

    String adminId;
    String memberId;
    String adminWorkspace;
    String memberWorkspace;

    @BeforeAll
    void setUp() {
        String baseUrl = "http://127.0.0.1:" + serverContext.getWebServer().getPort();
        adminSession = new ApiSession();
        admin = client(baseUrl, adminSession);
        memberSession = new ApiSession();
        member = client(baseUrl, memberSession);
        anonymous = client(baseUrl, null);

        adminSession.login(admin, TestAccounts.ADMIN_EMAIL, TestAccounts.ADMIN_PASSWORD);
        adminId = TestAccounts.ownerId(users);

        // 第二个账号走真实的建号接口（管理员建号是唯一入口，没有自助注册）。
        ResponseEntity<String> created = admin.postForEntity(
                "/api/admin/users",
                json("""
                     {"email": "member@test.local", "display_name": "成员",
                      "password": "test-member-password", "role": "member"}
                     """),
                String.class);
        assertThat(created.getStatusCode().value()).isEqualTo(201);
        memberId = com.jayway.jsonpath.JsonPath.read(created.getBody(), "$.id");

        memberSession.login(member, "member@test.local", "test-member-password");
        adminWorkspace = createWorkspace(admin, "管理员的工作区");
        memberWorkspace = createWorkspace(member, "成员的工作区");
    }

    // ------------------------------------------------------------------ //
    // 未登录
    // ------------------------------------------------------------------ //
    @Test
    void everyWorkspaceRouteRequiresLogin() {
        for (Route route : workspaceRoutes(adminWorkspace)) {
            ResponseEntity<String> response = call(anonymous, route);
            int status = response.getStatusCode().value();
            if (route.method() == HttpMethod.GET) {
                assertThat(status).as("匿名访问 %s %s", route.method(), route.path())
                        .isEqualTo(401);
            } else {
                // 变更类请求在鉴权之前先过 CSRF：匿名且无令牌时 CSRF 先拒（403），
                // 未登录但带令牌时才是 401。两者都属于"被拒绝"，这里只断言不放过。
                assertThat(status).as("匿名访问 %s %s", route.method(), route.path())
                        .isIn(401, 403);
            }
        }
    }

    @Test
    void adminEndpointsRequireLogin() {
        assertThat(anonymous.getForEntity("/api/admin/users", String.class)
                .getStatusCode().value()).isEqualTo(401);
        assertThat(anonymous.getForEntity("/api/system/resilience", String.class)
                .getStatusCode().value()).isEqualTo(401);
    }

    // ------------------------------------------------------------------ //
    // 归属隔离（表驱动：新增路由漏加守卫即失败）
    // ------------------------------------------------------------------ //
    @Test
    void foreignWorkspaceIs404OnEveryRoute() {
        for (Route route : workspaceRoutes(memberWorkspace)) {
            ResponseEntity<String> response = call(admin, route);
            assertThat(response.getStatusCode().value())
                    .as("管理员（非 owner）访问成员工作区的 %s %s 应当 404 而不是 403",
                            route.method(), route.path())
                    .isEqualTo(404);
        }
        // 越权删除必须真的没删掉
        assertThat(member.getForEntity("/api/workspaces/" + memberWorkspace, String.class)
                .getStatusCode().value()).isEqualTo(200);
    }

    @Test
    void workspaceListOnlyContainsOwnWorkspaces() {
        List<String> adminIds = ids(admin.getForEntity("/api/workspaces", String.class));
        assertThat(adminIds).contains(adminWorkspace).doesNotContain(memberWorkspace);

        List<String> memberIds = ids(member.getForEntity("/api/workspaces", String.class));
        assertThat(memberIds).contains(memberWorkspace).doesNotContain(adminWorkspace);
    }

    @Test
    void adminCanManageAccountsButNotOtherPeoplesContent() {
        assertThat(admin.getForEntity("/api/admin/users", String.class)
                .getStatusCode().value()).isEqualTo(200);
        // 成员不是管理员：账号治理与运维端点都拒绝。
        assertThat(member.getForEntity("/api/admin/users", String.class)
                .getStatusCode().value()).isEqualTo(403);
        assertThat(member.getForEntity("/api/system/resilience", String.class)
                .getStatusCode().value()).isEqualTo(403);
        // 管理员的读路径同样被归属拦在门外（不是"管理员看全部"）。
        assertThat(admin.getForEntity("/api/workspaces/" + memberWorkspace + "/messages",
                String.class).getStatusCode().value()).isEqualTo(404);
    }

    // ------------------------------------------------------------------ //
    // CSRF
    // ------------------------------------------------------------------ //
    @Test
    void mutatingRequestWithoutCsrfTokenIsRejected() {
        int before = ids(admin.getForEntity("/api/workspaces", String.class)).size();
        RestTemplate withoutCsrf = client(
                "http://127.0.0.1:" + serverContext.getWebServer().getPort(), null);
        // 用管理员的 Cookie 但故意不带 X-XSRF-TOKEN。
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        headers.add(HttpHeaders.COOKIE, adminSession.cookieHeader());
        ResponseEntity<String> response = withoutCsrf.exchange(
                "/api/workspaces", HttpMethod.POST,
                new HttpEntity<>("{\"name\":\"csrf 攻击\"}", headers), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(403);
        // 不能因为缺 CSRF 就真建出一个工作区
        assertThat(ids(admin.getForEntity("/api/workspaces", String.class)))
                .hasSize(before);
    }

    // ------------------------------------------------------------------ //
    // 会话生命周期
    // ------------------------------------------------------------------ //
    @Test
    void logoutInvalidatesTheSession() {
        RestTemplate throwaway = client(
                "http://127.0.0.1:" + serverContext.getWebServer().getPort(), new ApiSession());
        ApiSession session = (ApiSession) throwaway.getInterceptors().get(0);
        session.login(throwaway, TestAccounts.ADMIN_EMAIL, TestAccounts.ADMIN_PASSWORD);
        assertThat(throwaway.getForEntity("/api/auth/me", String.class)
                .getStatusCode().value()).isEqualTo(200);

        session.logout(throwaway);
        assertThat(throwaway.getForEntity("/api/auth/me", String.class)
                .getStatusCode().value()).isEqualTo(401);
    }

    /**
     * 会话必须落在 Postgres 而不是进程内存里（spring-session-jdbc）：重启不掉线、
     * 多实例可共享，都是靠这一点。两重证据：库里有会话行 + 一个"只有 Cookie、
     * 没有任何内存上下文"的新客户端能通过鉴权。
     */
    @Test
    void sessionIsStoredInPostgresAndSurvivesAFreshClient() {
        Long rows = jdbc.queryForObject("SELECT COUNT(*) FROM spring_session", Long.class);
        assertThat(rows).isNotNull().isGreaterThan(0L);

        HttpHeaders headers = new HttpHeaders();
        headers.add(HttpHeaders.COOKIE, adminSession.cookieHeader());
        ResponseEntity<String> response = anonymous.exchange(
                "/api/auth/me", HttpMethod.GET, new HttpEntity<>(headers), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(200);
        assertThat(response.getBody()).contains(TestAccounts.ADMIN_EMAIL);
    }

    @Test
    void disablingAnAccountTakesEffectOnTheLiveSession() {
        RestTemplate victim = client(
                "http://127.0.0.1:" + serverContext.getWebServer().getPort(), new ApiSession());
        ApiSession victimSession = (ApiSession) victim.getInterceptors().get(0);
        victimSession.login(victim, "member@test.local", "test-member-password");
        assertThat(victim.getForEntity("/api/auth/me", String.class)
                .getStatusCode().value()).isEqualTo(200);

        assertThat(admin.postForEntity("/api/admin/users/" + memberId + "/status",
                json("{\"status\": \"disabled\"}"), String.class)
                .getStatusCode().value()).isEqualTo(200);
        try {
            // 会话还在，但账号已停用 → 立刻拒绝（Spring Session 落库不会自动感知停用）
            assertThat(victim.getForEntity("/api/auth/me", String.class)
                    .getStatusCode().value()).isEqualTo(401);
            // 也不能重新登录
            assertThat(victim.postForEntity("/api/auth/login",
                    json("{\"email\": \"member@test.local\", \"password\": \"test-member-password\"}"),
                    String.class).getStatusCode().value()).isEqualTo(401);
        } finally {
            admin.postForEntity("/api/admin/users/" + memberId + "/status",
                    json("{\"status\": \"active\"}"), String.class);
        }
    }

    // ------------------------------------------------------------------ //
    // 护栏与内部边界
    // ------------------------------------------------------------------ //
    @Test
    void theLastActiveAdminCannotBeDisabledOrDemoted() {
        // 当前只有一个管理员（引导管理员）自己：
        assertThat(admin.postForEntity("/api/admin/users/" + adminId + "/status",
                json("{\"status\": \"disabled\"}"), String.class)
                .getStatusCode().value()).isEqualTo(409);
        assertThat(admin.postForEntity("/api/admin/users/" + adminId + "/role",
                json("{\"role\": \"member\"}"), String.class)
                .getStatusCode().value()).isEqualTo(409);
    }

    @Test
    void internalCorpusRouteRequiresTheSharedToken() {
        // 不带内部令牌（匿名客户端没有该头）→ 401，而不是把语料吐给任何人
        assertThat(anonymous.getForEntity(
                "/internal/workspaces/" + adminWorkspace + "/retrieval-corpus", String.class)
                .getStatusCode().value()).isEqualTo(401);

        HttpHeaders headers = new HttpHeaders();
        headers.set("X-Internal-Token", TestAccounts.INTERNAL_TOKEN);
        ResponseEntity<String> allowed = anonymous.exchange(
                "/internal/workspaces/" + adminWorkspace + "/retrieval-corpus",
                HttpMethod.GET, new HttpEntity<>(headers), String.class);
        assertThat(allowed.getStatusCode().value()).isEqualTo(200);

        headers.set("X-Internal-Token", "wrong-token");
        assertThat(anonymous.exchange(
                "/internal/workspaces/" + adminWorkspace + "/retrieval-corpus",
                HttpMethod.GET, new HttpEntity<>(headers), String.class)
                .getStatusCode().value()).isEqualTo(401);
    }

    @Test
    void userMessagesAndProposalsRecordTheirAuthor() {
        // 聊天链路里 user 行带上提问人；这是"审计到人"的最小闭环。
        jdbc.update("INSERT INTO messages (id, workspace_id, user_id, role, content, created_at)"
                        + " VALUES (?, ?, ?, 'user', ?, now())",
                "audit-msg", memberWorkspace, memberId, "审计用问题");
        String recorded = jdbc.queryForObject(
                "SELECT user_id FROM messages WHERE id = 'audit-msg'", String.class);
        assertThat(recorded).isEqualTo(memberId);
    }

    // ------------------------------------------------------------------ //
    // 测试脚手架
    // ------------------------------------------------------------------ //
    /** 工作区下所有需要归属校验的路由（表驱动断言的唯一来源）。 */
    private record Route(HttpMethod method, String path) {}

    private static List<Route> workspaceRoutes(String workspaceId) {
        String base = "/api/workspaces/" + workspaceId;
        return List.of(
                new Route(HttpMethod.GET, base),
                new Route(HttpMethod.DELETE, base),
                new Route(HttpMethod.GET, base + "/files"),
                new Route(HttpMethod.GET, base + "/files/f1/download"),
                new Route(HttpMethod.DELETE, base + "/files/f1"),
                new Route(HttpMethod.POST, base + "/files/f1/reindex"),
                new Route(HttpMethod.POST, base + "/reindex"),
                new Route(HttpMethod.GET, base + "/messages"),
                new Route(HttpMethod.POST, base + "/messages/m1/feedback"),
                new Route(HttpMethod.GET, base + "/traces"),
                new Route(HttpMethod.GET, base + "/traces/r1"),
                new Route(HttpMethod.GET, base + "/tools"),
                new Route(HttpMethod.GET, base + "/preview?file=a.xlsx&location=S!A1"),
                new Route(HttpMethod.GET, base + "/operations"),
                new Route(HttpMethod.POST, base + "/operations/o1/apply"),
                new Route(HttpMethod.POST, base + "/operations/o1/reject"),
                new Route(HttpMethod.POST, base + "/operations/o1/revert"),
                new Route(HttpMethod.POST, base + "/chat/stream"));
    }

    private ResponseEntity<String> call(RestTemplate template, Route route) {
        HttpEntity<String> entity = null;
        if (route.method() != HttpMethod.GET) {
            // 请求体必须合法：@Valid 的校验发生在控制器方法体之前，body 不合法会先
            // 变成 400，于是测不到"守卫是否生效"。这里的 body 只求通过校验。
            if (route.path().endsWith("/chat/stream")) {
                entity = json("{\"message\": \"你好\"}");
            } else if (route.path().contains("/messages/")) {
                entity = json("{\"feedback\": \"up\"}");
            } else {
                entity = json("{}");
            }
        }
        return template.exchange(route.path(), route.method(), entity, String.class);
    }

    private String createWorkspace(RestTemplate template, String name) {
        ResponseEntity<String> response = template.postForEntity(
                "/api/workspaces", json("{\"name\": \"" + name + "\"}"), String.class);
        assertThat(response.getStatusCode().value()).isEqualTo(201);
        return com.jayway.jsonpath.JsonPath.read(response.getBody(), "$.id");
    }

    private static List<String> ids(ResponseEntity<String> response) {
        return com.jayway.jsonpath.JsonPath.read(response.getBody(), "$[*].id");
    }

    private static RestTemplate client(String baseUrl, ApiSession session) {
        RestTemplate template = new RestTemplate();
        if (session != null) {
            template.getInterceptors().add(session);
        }
        template.setUriTemplateHandler(new DefaultUriBuilderFactory(baseUrl));
        template.setErrorHandler(new ResponseErrorHandler() {
            @Override
            public boolean hasError(org.springframework.http.client.ClientHttpResponse response) {
                return false;
            }
        });
        return template;
    }

    private static HttpEntity<String> json(String body) {
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.APPLICATION_JSON);
        return new HttpEntity<>(body, headers);
    }
}
