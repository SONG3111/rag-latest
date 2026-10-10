package com.raglatest.backend.support;

import com.raglatest.backend.identity.UserRepository;

/**
 * 测试账号常量，与 src/test/resources/application.yml 里的
 * {@code rag.security.bootstrap-admin-*} 保持一致。
 *
 * <p>用固定的引导管理员而不是"每个测试自己建号"：空库首个上下文会自动创建它
 * （BootstrapAdminInitializer），于是测试拿到的就是一个真实存在、真实登录过的
 * 账号——顺带把首启引导这条路径也覆盖了。</p>
 */
public final class TestAccounts {

    public static final String ADMIN_EMAIL = "admin@test.local";
    public static final String ADMIN_PASSWORD = "test-admin-password";

    /** 与 src/test/resources/application.yml 的 rag.internal-token 一致。 */
    public static final String INTERNAL_TOKEN = "test-internal-token";

    private TestAccounts() {
    }

    /** 直接建数据（不走 HTTP）的测试需要它当 owner。 */
    public static String ownerId(UserRepository users) {
        return users.findByEmail(ADMIN_EMAIL)
                .orElseThrow(() -> new IllegalStateException("引导管理员未被创建"))
                .id();
    }
}
