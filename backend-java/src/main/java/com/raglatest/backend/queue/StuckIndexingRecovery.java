package com.raglatest.backend.queue;

import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.DocumentFileRepository.FileStuck;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

/**
 * 启动兜底：把卡在 pending/indexing 的文件重新投递进队列。
 *
 * <p>消息未 ack 时 broker 会自行 requeue，本 runner 补的是消息之外的状态残留：
 * 标了 indexing 但投递前进程退出、旧内存 executor 时代的遗留、以及 broker 数据
 * 丢失的极端情形。重复投递无害（消费端幂等，见 {@link IndexingConsumer}）。</p>
 */
@Component
@Order(2)
public class StuckIndexingRecovery implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(StuckIndexingRecovery.class);

    private final DocumentFileRepository files;
    private final ReindexProducer reindexQueue;

    public StuckIndexingRecovery(DocumentFileRepository files, ReindexProducer reindexQueue) {
        this.files = files;
        this.reindexQueue = reindexQueue;
    }

    @Override
    public void run(ApplicationArguments args) {
        List<FileStuck> stuck = files.findByStatuses("pending", "indexing");
        for (FileStuck file : stuck) {
            reindexQueue.enqueue(file.workspaceId(), file.id(), file.relPath());
        }
        if (!stuck.isEmpty()) {
            log.info("requeued {} stuck indexing file(s) on startup", stuck.size());
        }
    }
}
