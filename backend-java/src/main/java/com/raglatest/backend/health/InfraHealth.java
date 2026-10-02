package com.raglatest.backend.health;

import com.raglatest.backend.queue.AmqpConfig;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.AmqpAdmin;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.data.redis.connection.RedisConnectionFactory;
import org.springframework.stereotype.Component;

/**
 * 基础设施探针：Redis 与 RabbitMQ 的可达性，供 /health 聚合展示。
 *
 * <p>探测必须廉价且永不抛异常——健康检查自身失败会误导排障方向
 * （与 HealthController 的降级语义一致）。AMQP 无轻量 ping 命令，
 * 用队列属性查询代替（getQueueInfo 对不存在的队列返回 null，拓扑未声明
 * 完成或 broker 不可达都视为 down）。</p>
 *
 * <p>AmqpAdmin 可选注入：测试配置里 spring.rabbitmq.dynamic=false 不注册该 bean，
 * 此时队列探测恒为 down（-1），不影响上下文装配。</p>
 */
@Component
public class InfraHealth {

    private static final Logger log = LoggerFactory.getLogger(InfraHealth.class);

    private final RedisConnectionFactory redis;
    private final AmqpAdmin amqpAdmin;

    public InfraHealth(RedisConnectionFactory redis, ObjectProvider<AmqpAdmin> amqpAdmin) {
        this.redis = redis;
        this.amqpAdmin = amqpAdmin.getIfAvailable();
    }

    /**
     * Redis 可达性：用 serverCommands().time() 代替 ping——Spring Data Redis 4.1
     * 移除了 ping()，time() 同为 O(1) 服务器命令且不触碰数据。
     */
    public boolean redisUp() {
        try (var connection = redis.getConnection()) {
            return connection.serverCommands().time() != null;
        } catch (Exception ex) {
            log.debug("redis probe failed: {}", ex.getMessage());
            return false;
        }
    }

    public boolean queueUp() {
        return queueInfo() != null;
    }

    /** 主队列的消息堆积数；不可达时返回 -1（观测端点用，不参与健康判定）。 */
    public int queueDepth() {
        var info = queueInfo();
        return info != null ? (int) info.getMessageCount() : -1;
    }

    private org.springframework.amqp.core.QueueInformation queueInfo() {
        if (amqpAdmin == null) {
            return null;
        }
        try {
            return amqpAdmin.getQueueInfo(AmqpConfig.INDEXING_QUEUE);
        } catch (Exception ex) {
            log.debug("rabbitmq queue probe failed: {}", ex.getMessage());
            return null;
        }
    }
}
