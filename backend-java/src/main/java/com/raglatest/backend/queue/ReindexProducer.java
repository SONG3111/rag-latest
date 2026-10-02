package com.raglatest.backend.queue;

import com.raglatest.backend.store.DocumentFileRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.AmqpException;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.ObjectMapper;

/**
 * 索引任务的生产端：把 {@link ReindexTask} 序列化为 JSON 字符串投递到持久化队列。
 *
 * <p>消息体刻意走 String + Jackson 3 手动序列化（而非 spring-amqp 的
 * Jackson2JsonMessageConverter）：后者绑定 Jackson 2，与 Boot 4 的 Jackson 3 有
 * 已知摩擦（同 pom.xml 里 resilience4j#2351 的规避先例）。</p>
 *
 * <p>投递失败（broker 不可达）不让上传/apply 请求失败——文件已写盘、提案已落库，
 * 此时把文件标 failed 让状态可见、可经 reindex 端点手动重试，优于把整个请求打成 500。</p>
 */
@Component
public class ReindexProducer {

    private static final Logger log = LoggerFactory.getLogger(ReindexProducer.class);

    private final RabbitTemplate rabbit;
    private final ObjectMapper mapper;
    private final DocumentFileRepository files;

    public ReindexProducer(RabbitTemplate rabbit, ObjectMapper mapper,
                           DocumentFileRepository files) {
        this.rabbit = rabbit;
        this.mapper = mapper;
        this.files = files;
    }

    public void enqueue(String workspaceId, String fileId, String relPath) {
        String payload = mapper.writeValueAsString(new ReindexTask(workspaceId, fileId, relPath));
        try {
            rabbit.convertAndSend(AmqpConfig.INDEXING_EXCHANGE,
                    AmqpConfig.INDEXING_ROUTING_KEY, payload);
        } catch (AmqpException ex) {
            log.error("could not queue reindex for {} ({}): {}", relPath, fileId, ex.getMessage());
            files.markFailed(fileId, "could not queue reindex: " + ex.getMessage());
        }
    }
}
