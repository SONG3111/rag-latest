package com.raglatest.backend.security;

import com.raglatest.backend.config.RagProperties;
import com.raglatest.backend.identity.AccountService;
import com.raglatest.backend.identity.UserRecord;
import java.security.SecureRandom;
import java.util.Base64;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

/**
 * 首启引导：空库时创建第一个管理员，否则整套系统没有任何入口（没有自助注册）。
 *
 * <p>两条路径，与 Spring Boot 默认用户的行为一致：配置了
 * {@code RAG_BOOTSTRAP_ADMIN_PASSWORD} 就用它；没配就生成随机密码并<b>只打印一次</b>
 * ——把随机口令写进配置文件反而是更糟的做法。已存在任何账号时本类不碰数据库，
 * 所以改这个环境变量不会静默重置已有管理员密码。</p>
 */
@Component
@Order(10)
public class BootstrapAdminInitializer implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(BootstrapAdminInitializer.class);
    private static final String DEFAULT_EMAIL = "admin@localhost";
    private static final int RANDOM_PASSWORD_BYTES = 18;

    private final AccountService accounts;
    private final RagProperties properties;
    private final SecureRandom random = new SecureRandom();

    public BootstrapAdminInitializer(AccountService accounts, RagProperties properties) {
        this.accounts = accounts;
        this.properties = properties;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (!accounts.list().isEmpty()) {
            return;
        }
        RagProperties.Security security = properties.security();
        String email = security.bootstrapAdminEmail().isBlank()
                ? DEFAULT_EMAIL
                : security.bootstrapAdminEmail();
        String configuredPassword = security.bootstrapAdminPassword();
        boolean generated = configuredPassword.isBlank();
        String password = generated ? randomPassword() : configuredPassword;

        UserRecord admin = accounts.create(email, "管理员", password, UserRecord.ROLE_ADMIN);
        if (generated) {
            log.warn("首次启动：已创建管理员账号 {}（密码为随机生成，仅本次打印，请立即登录后修改）：{}",
                    admin.email(), password);
        } else {
            log.info("首次启动：已按 RAG_BOOTSTRAP_ADMIN_EMAIL/PASSWORD 创建管理员账号 {}", admin.email());
        }
    }

    /** Base64URL 编码 18 字节 → 24 字符，避免 +/= 在 shell 里被转义。 */
    private String randomPassword() {
        byte[] buffer = new byte[RANDOM_PASSWORD_BYTES];
        random.nextBytes(buffer);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(buffer);
    }
}
