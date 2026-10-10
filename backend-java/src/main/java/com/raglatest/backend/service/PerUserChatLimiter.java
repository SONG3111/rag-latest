package com.raglatest.backend.service;

import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Semaphore;
import org.springframework.stereotype.Component;

/**
 * 每个用户同时只允许一轮对话在跑。
 *
 * <p>为什么需要它：全局聊天舱壁是<b>共享</b>资源（默认 4 席）。没有单用户限制时，
 * 一个人连点几次就能把全部席位占满，其他人一律收到"当前对话请求过多"——多人
 * 共用同一套服务时，这是最容易招人骂的失败模式。本限制把"用满全局容量"从
 * "谁能点"变成"谁先到"，个体行为不再影响其他人的可用性。</p>
 *
 * <p>单实例内存实现即可：服务本身是单实例部署（会话落 Postgres 是为了重启不掉线，
 * 不是多实例分片）。若将来横向扩展，这里要换成 Redis 信号量——届时把本类换掉即可，
 * 调用方（ChatController）不受影响。</p>
 */
@Component
public class PerUserChatLimiter {

    /** 每用户并发轮数。1 是有意的：一轮对话本身可能跑十几秒到几分钟。 */
    private static final int PERMITS_PER_USER = 1;

    private final ConcurrentHashMap<String, Semaphore> permits = new ConcurrentHashMap<>();

    /** 非阻塞抢占；已有一轮在跑则返回 false（调用方转成 503 快速失败）。 */
    public boolean tryAcquire(String userId) {
        if (userId == null) {
            // 没有身份就不该走到这里（控制器已要求登录）；放行会让限制形同虚设。
            return false;
        }
        return permits.computeIfAbsent(userId, key -> new Semaphore(PERMITS_PER_USER)).tryAcquire();
    }

    public void release(String userId) {
        Semaphore semaphore = userId == null ? null : permits.get(userId);
        if (semaphore != null) {
            semaphore.release();
        }
    }
}
