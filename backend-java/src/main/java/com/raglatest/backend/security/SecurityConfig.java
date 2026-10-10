package com.raglatest.backend.security;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.authentication.AuthenticationManager;
import org.springframework.security.authentication.ProviderManager;
import org.springframework.security.authentication.dao.DaoAuthenticationProvider;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.annotation.web.configuration.EnableWebSecurity;
import org.springframework.security.config.annotation.web.configurers.AbstractHttpConfigurer;
import org.springframework.security.core.userdetails.UserDetailsService;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.access.intercept.AuthorizationFilter;
import org.springframework.security.web.context.HttpSessionSecurityContextRepository;
import org.springframework.security.web.context.SecurityContextHolderFilter;
import org.springframework.security.web.context.SecurityContextRepository;
import org.springframework.security.web.csrf.CookieCsrfTokenRepository;

/**
 * 鉴权装配。要点：
 *
 * <ul>
 *   <li><b>会话而非令牌</b>：登录态存 Postgres（spring-session-jdbc），因此可以
 *       服务端强制下线；这也是不用 JWT 的原因——JWT 要撤销就得再建一张黑名单表，
 *       等于把会话表换个名字重新发明一遍。</li>
 *   <li><b>没有表单登录页</b>：登录是 JSON 端点（见 {@code AuthController}），
 *       因为前端是 SPA，且错误契约要沿用 {@code {"detail": ...}}。</li>
 *   <li><b>授权集中在 URL 规则</b>：{@code /api/admin/**} 与 {@code /api/system/**}
 *       要求 admin，其余一律要求登录；工作区级的归属校验在业务层
 *       （{@code WorkspaceAccessGuard}），因为它需要"workspace 归谁"这一事实。</li>
 *   <li><b>CSRF 走 SPA 官方配方</b>：Cookie 可读 + {@code X-XSRF-TOKEN} 头。</li>
 * </ul>
 */
@Configuration
@EnableWebSecurity
public class SecurityConfig {

    @Bean
    public PasswordEncoder passwordEncoder() {
        return new BCryptPasswordEncoder();
    }

    @Bean
    public AuthenticationManager authenticationManager(UserDetailsService userDetailsService,
                                                       PasswordEncoder passwordEncoder) {
        DaoAuthenticationProvider provider = new DaoAuthenticationProvider(userDetailsService);
        provider.setPasswordEncoder(passwordEncoder);
        // 用户不存在与密码错误对外都是同一个 401 文案，不做账号枚举。
        provider.setHideUserNotFoundExceptions(true);
        return new ProviderManager(provider);
    }

    /**
     * 会话式 SecurityContext 的落盘点。手动登录（AuthController）需要显式
     * saveContext 才会写进会话，因此这里把仓库暴露成 Bean 共享给两处。
     */
    @Bean
    public SecurityContextRepository securityContextRepository() {
        return new HttpSessionSecurityContextRepository();
    }

    @Bean
    public SecurityFilterChain securityFilterChain(
            HttpSecurity http,
            InternalTokenFilter internalTokenFilter,
            AccountFreshnessFilter accountFreshnessFilter,
            SecurityContextRepository securityContextRepository) throws Exception {
        http
                .securityContext(context -> context
                        .securityContextRepository(securityContextRepository))
                .csrf(csrf -> csrf
                        // Cookie 必须可被 JS 读取，SPA 才能把值回填到请求头。
                        .csrfTokenRepository(CookieCsrfTokenRepository.withHttpOnlyFalse())
                        .csrfTokenRequestHandler(new SpaCsrfTokenRequestHandler()))
                .authorizeHttpRequests(auth -> auth
                        // 健康探测在登录前就要可用（前端据此显示降级横幅）。
                        .requestMatchers("/health", "/api/auth/login", "/api/auth/csrf").permitAll()
                        // 内部端点没有会话，唯一门槛是 InternalTokenFilter（见该类的注释）。
                        .requestMatchers("/internal/**").permitAll()
                        .requestMatchers("/api/admin/**").hasRole("ADMIN")
                        .requestMatchers("/api/system/**").hasRole("ADMIN")
                        .anyRequest().authenticated())
                .exceptionHandling(ex -> ex
                        .authenticationEntryPoint(JsonSecurityHandlers.entryPoint())
                        .accessDeniedHandler(JsonSecurityHandlers.accessDeniedHandler()))
                .formLogin(AbstractHttpConfigurer::disable)
                .httpBasic(AbstractHttpConfigurer::disable)
                // 登出走自定义的 /api/auth/logout（要返回 204，不跳转）。
                .logout(AbstractHttpConfigurer::disable)
                .addFilterBefore(internalTokenFilter, AuthorizationFilter.class)
                .addFilterAfter(accountFreshnessFilter, SecurityContextHolderFilter.class)
                .sessionManagement(session -> session
                        .sessionFixation(fixation -> fixation.changeSessionId()));
        return http.build();
    }
}
