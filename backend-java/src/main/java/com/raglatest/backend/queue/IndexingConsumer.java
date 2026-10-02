package com.raglatest.backend.queue;

import com.raglatest.backend.service.IndexingService;
import com.raglatest.backend.store.DocumentFileRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;
import tools.jackson.databind.ObjectMapper;

/**
 * 索引任务的消费端：并发 1（对齐原单线程 reindex executor，嵌入是重活且 SQLite 写锁
 * 只想被一个索引流程占用）。任务体与旧 {@code OperationService.reindexAfterWrite} 一致：
 * 按 relPath 重新定位文件（apply 期间可能被并发修改）后交给 {@link IndexingService}。
 *
 * <p>异常语义：反序列化失败（毒消息）直接抛出——listener 配了
 * {@code default-requeue-rejected=false}，消息进 DLQ 停车场；业务失败被 catch 兜底
 * {@code markFailed} 后正常 ack，绝不 requeue（避免与文件状态机打架的死循环）。
 * at-least-once 投递 + {@code replaceFileChunks} 的删旧插新幂等，重复消费无害。</p>
 */
@Component
public class IndexingConsumer {

    private static final Logger log = LoggerFactory.getLogger(IndexingConsumer.class);

    private final DocumentFileRepository files;
    private final IndexingService indexing;
    private final ObjectMapper mapper;

    public IndexingConsumer(DocumentFileRepository files, IndexingService indexing,
                            ObjectMapper mapper) {
        this.files = files;
        this.indexing = indexing;
        this.mapper = mapper;
    }

    @RabbitListener(queues = AmqpConfig.INDEXING_QUEUE)
    public void onReindexTask(String payload) {
        ReindexTask task = mapper.readValue(payload, ReindexTask.class);
        try {
            files.findByRelPath(task.workspaceId(), task.relPath())
                    .ifPresentOrElse(
                            file -> indexing.index(task.workspaceId(), file.id()),
                            () -> log.warn("reindex target no longer exists: {}/{}",
                                    task.workspaceId(), task.relPath()));
        } catch (Exception ex) {
            log.warn("reindex task failed for {} ({}): {}",
                    task.relPath(), task.fileId(), ex.getMessage());
            files.markFailed(task.fileId(), "reindex failed: " + ex.getMessage());
        }
    }
}
