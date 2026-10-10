package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.AuthDtos.CsrfTokenRead;
import com.raglatest.backend.api.dto.AuthDtos.LoginRequest;
import com.raglatest.backend.api.dto.AuthDtos.UserRead;
import com.raglatest.backend.identity.AccountService;
import com.raglatest.backend.identity.UserRecord;
import com.raglatest.backend.security.AuthenticatedUser;
import com.raglatest.backend.security.CurrentUser;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;
import jakarta.validation.Valid;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.AuthenticationException;
import org.springframework.security.core.context.SecurityContext;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.context.SecurityContextRepository;
import org.springframework.security.web.csrf.CsrfToken;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 登录/登出/身份查询/CSRF 引导。刻意不用 Spring Security 的表单登录：
 * 前端是 SPA，错误体必须是 {@code {"detail": ...}}（与 ApiExceptionHandler 一致），
 * 而表单登录会返回 302 与 HTML 错误页。
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private static final Logger log = LoggerFactory.getLogger(AuthController.class);

    private final AuthenticationManager authenticationManager;
    private final SecurityContextRepository securityContextRepository;
    private final AccountService accounts;

    public AuthController(AuthenticationManager authenticationManager,
                          SecurityContextRepository securityContextRepository,
                          AccountService accounts) {
        this.authenticationManager = authenticationManager;
        this.securityContextRepository = securityContextRepository;
        this.accounts = accounts;
    }

    /**
     * 登录成功返回与 {@code /me} 相同的主体（前端省一次往返），并下发会话 Cookie。
     * 失败一律 401 同一文案：区分"账号不存在"与"密码错误"等于送出一个账号枚举接口。
     */
    @PostMapping("/login")
    public UserRead login(@Valid @RequestBody LoginRequest payload,
                          HttpServletRequest request,
                          HttpServletResponse response) {
        Authentication authentication;
        try {
            authentication = authenticationManager.authenticate(
                    new UsernamePasswordAuthenticationToken(
                            AccountService.normalizeEmail(payload.email()), payload.password()));
        } catch (AuthenticationException ex) {
            log.info("login rejected for {}: {}", payload.email(), ex.getClass().getSimpleName());
            throw new ApiException(HttpStatus.UNAUTHORIZED, "邮箱或密码不正确");
        }

        // 会话固定攻击防护：先确保有会话，再换掉会话 id，最后把认证结果写进会话。
        request.getSession(true);
        request.changeSessionId();
        SecurityContext context = SecurityContextHolder.createEmptyContext();
        context.setAuthentication(authentication);
        SecurityContextHolder.setContext(context);
        securityContextRepository.saveContext(context, request, response);

        AuthenticatedUser user = (AuthenticatedUser) authentication.getPrincipal();
        accounts.touchLastLogin(user.id());
        return UserRead.from(requireRecord(user.id()));
    }

    @PostMapping("/logout")
    public ResponseEntity<Void> logout(HttpServletRequest request) {
        HttpSession session = request.getSession(false);
        if (session != null) {
            session.invalidate();
        }
        SecurityContextHolder.clearContext();
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/me")
    public UserRead me() {
        return UserRead.from(requireRecord(CurrentUser.id()));
    }

    /**
     * CSRF 令牌引导：取一次令牌即写出 XSRF-TOKEN Cookie（见 SpaCsrfTokenRequestHandler）。
     * 前端在登录后与冷启动恢复会话后各调一次。
     */
    @GetMapping("/csrf")
    public CsrfTokenRead csrf(CsrfToken token) {
        return new CsrfTokenRead(token.getToken(), token.getHeaderName());
    }

    /** 从库里回读一次再返回：登录响应的 last_login_at 才是刚写进去的那个值。 */
    private UserRecord requireRecord(String userId) {
        return accounts.list().stream()
                .filter(record -> record.id().equals(userId))
                .findFirst()
                .orElseThrow(() -> new ApiException(HttpStatus.UNAUTHORIZED, "请先登录"));
    }
}
