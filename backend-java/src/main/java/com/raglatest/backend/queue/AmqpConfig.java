package com.raglatest.backend.queue;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.core.Binding;
import org.springframework.amqp.core.BindingBuilder;
import org.springframework.amqp.core.DirectExchange;
import org.springframework.amqp.core.Queue;
import org.springframework.amqp.core.QueueBuilder;
import org.springframework.amqp.rabbit.config.SimpleRabbitListenerContainerFactory;
import org.springframework.amqp.rabbit.connection.ConnectionFactory;
import org.springframework.boot.amqp.autoconfigure.SimpleRabbitListenerContainerFactoryConfigurer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

/**
 * 索引任务队列的声明式拓扑：RabbitAdmin 在首次连接时自动声明（任意 Queue Bean
 * 即会被 Boot 自动配置的 AmqpAdmin 声明到 broker，见 Spring Boot AMQP 参考文档）。
 *
 * <p>重试与死信取舍（迁移自 Spring AMQP 官方 Exception Handling / Recovering from
 * Broker Failures 章节的推荐）：消费端不配 listener retry、不做 TTL+DLX 回环——
 * 索引是分钟级重活，回环重试会与文件状态机（failed + reindex 端点手动恢复）打架；
 * DLQ 只做 parking lot，收编毒消息与未预期异常供人工排障。业务失败（ai-service
 * 不可达等）由 {@link IndexingConsumer} 兜底 {@code markFailed} 后正常 ack。</p>
 */
@Configuration
public class AmqpConfig {

    public static final String INDEXING_EXCHANGE = "rag.indexing";
    public static final String INDEXING_QUEUE = "rag.indexing.queue";
    public static final String INDEXING_ROUTING_KEY = "file.reindex";
    public static final String INDEXING_DLX = "rag.indexing.dlx";
    public static final String INDEXING_DLQ = "rag.indexing.dlq";

    @Bean
    DirectExchange indexingExchange() {
        return new DirectExchange(INDEXING_EXCHANGE, true, false);
    }

    /** 主队列：durable + 持久化消息，进程崩溃后未 ack 的消息由 broker requeue。 */
    @Bean
    Queue indexingQueue() {
        return QueueBuilder.durable(INDEXING_QUEUE)
                .deadLetterExchange(INDEXING_DLX)
                .deadLetterRoutingKey(INDEXING_ROUTING_KEY)
                .build();
    }

    @Bean
    Binding indexingBinding(Queue indexingQueue, DirectExchange indexingExchange) {
        return BindingBuilder.bind(indexingQueue).to(indexingExchange).with(INDEXING_ROUTING_KEY);
    }

    @Bean
    DirectExchange indexingDlx() {
        return new DirectExchange(INDEXING_DLX, true, false);
    }

    /** 死信停车场：不回环（无 TTL 重投），等待人工排障后经 reindex 端点重试。 */
    @Bean
    Queue indexingDlq() {
        return QueueBuilder.durable(INDEXING_DLQ).build();
    }

    @Bean
    Binding indexingDlqBinding(Queue indexingDlq, DirectExchange indexingDlx) {
        return BindingBuilder.bind(indexingDlq).to(indexingDlx).with(INDEXING_ROUTING_KEY);
    }

    /**
     * 批量消费工厂：攒批窗口 1 秒 / 每批最多 {@value #BATCH_SIZE} 条，消费者方法签名
     * 收 {@code List<String>}（转换后的消息体）。攒批后一次调 ai-service 的
     * /v1/index-batch——跨文件合并嵌入（长度排序池更大、padding 浪费更少），
     * 10 文件级的批量索引实测见 scripts/bench_upload.py。
     *
     * <p>Boot 3 的 {@code spring.rabbitmq.listener.simple.batch.*} 属性组在 Boot 4
     * 已不存在（RabbitProperties 无 Batch 内部类），故在代码装配；先经
     * {@link SimpleRabbitListenerContainerFactoryConfigurer} 应用 yml 属性
     * （并发 1 / 不 requeue），再叠批量项。并发必须保持 1：并发会拆散攒批，
     * 且嵌入在 ai-service 侧本就串行（其 INDEX_CAPACITY=1）。</p>
     */
    public static final int BATCH_SIZE = 10;

    @Bean
    SimpleRabbitListenerContainerFactory rabbitListenerContainerFactory(
            ConnectionFactory connectionFactory,
            SimpleRabbitListenerContainerFactoryConfigurer configurer) {
        SimpleRabbitListenerContainerFactory factory = new SimpleRabbitListenerContainerFactory();
        configurer.configure(factory, connectionFactory);
        factory.setBatchListener(true);
        factory.setConsumerBatchEnabled(true);
        factory.setBatchSize(BATCH_SIZE);
        factory.setReceiveTimeout(1000L);
        return factory;
    }

    /**
     * publisher confirm（correlated）失败仅告警：队列与消息均持久化，
     * broker 单机宕机的窗口内丢一条任务可由 StuckIndexingRecovery 兜底。
     * 模式迁移自 spring-projects/spring-amqp-samples 的 spring-rabbit-confirms-returns。
     */
    @Bean
    org.springframework.boot.amqp.autoconfigure.RabbitTemplateCustomizer confirmLoggingCustomizer() {
        Logger log = LoggerFactory.getLogger(AmqpConfig.class);
        return template -> template.setConfirmCallback((correlation, ack, cause) -> {
            if (!ack) {
                log.warn("reindex message not confirmed by broker: {}", cause);
            }
        });
    }
}
