package com.raglatest.backend.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/**
 * 单用户并发对话上限的语义。做成纯单元测试而不是并发集成用例：
 * 这里的契约是"每人同时一轮、释放后可再抢、别人的额度互不影响"，
 * 用信号量直接断言比构造真实并发流（慢且偶发）更精确。
 */
class PerUserChatLimiterTests {

    private final PerUserChatLimiter limiter = new PerUserChatLimiter();

    @Test
    void oneTurnPerUserAtATime() {
        assertThat(limiter.tryAcquire("user-a")).isTrue();
        // 同一个人的第二轮被挡住——这正是"一个人连点把全局舱壁占满"的防线。
        assertThat(limiter.tryAcquire("user-a")).isFalse();
    }

    @Test
    void otherUsersAreNotAffected() {
        assertThat(limiter.tryAcquire("user-a")).isTrue();
        assertThat(limiter.tryAcquire("user-b")).isTrue();
    }

    @Test
    void releaseAllowsTheNextTurn() {
        assertThat(limiter.tryAcquire("user-a")).isTrue();
        limiter.release("user-a");
        assertThat(limiter.tryAcquire("user-a")).isTrue();
    }

    /** 释放必须幂等/容错：终态路径可能重复调用，多放一次会凭空多出一个名额。 */
    @Test
    void releasingAnIdleUserDoesNotCreateExtraPermits() {
        limiter.release("user-a");
        assertThat(limiter.tryAcquire("user-a")).isTrue();
        assertThat(limiter.tryAcquire("user-a")).isFalse();
    }

    /** 没有身份就不放行：限制不适用于"匿名"这种会绕过限流的输入。 */
    @Test
    void missingIdentityIsRejected() {
        assertThat(limiter.tryAcquire(null)).isFalse();
    }
}
