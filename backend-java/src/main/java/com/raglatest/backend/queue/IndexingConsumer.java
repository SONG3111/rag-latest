package com.raglatest.backend.queue;

import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.service.IndexingService;
import com.raglatest.backend.store.DocumentFileRepository;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.amqp.AmqpException;
import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.ObjectMapper;

/**
 * 索引任务的批量消费端：容器层攒批（1 秒 / 最多 {@link AmqpConfig#BATCH_SIZE} 条，
 * 见 AmqpConfig 的工厂装配），按工作区分组后一次调 ai-service 的
 * /v1/index-batch（跨文件合并嵌入）。
 *
 * <p>异常语义：反序列化失败（毒消息）不抛——抛了会把整批一起 requeue/reject，
 * 好文件陪葬；毒消息单独直投 DLQ 停车场后继续。整批调用失败（网络/超时）逐文件
 * {@code markFailed} 后正常 ack，绝不 requeue（与文件状态机打架的死循环）。
 * at-least-once 投递 + {@code replaceFileChunks} 的删旧插新幂等，重复消费无害。</p>
 */
@Component
public class IndexingConsumer {

    private static final Logger log = LoggerFactory.getLogger(IndexingConsumer.class);

    private final DocumentFileRepository files;
    private final IndexingService indexing;
    private final AiServiceClient aiService;
    private final RabbitTemplate rabbit;
    private final ObjectMapper mapper;

    public IndexingConsumer(DocumentFileRepository files, IndexingService indexing,
                            AiServiceClient aiService, RabbitTemplate rabbit, ObjectMapper mapper) {
        this.files = files;
        this.indexing = indexing;
        this.aiService = aiService;
        this.rabbit = rabbit;
        this.mapper = mapper;
    }

    @RabbitListener(queues = AmqpConfig.INDEXING_QUEUE)
    public void onReindexTasks(List<String> payloads) {
        Map<String, List<ReindexTask>> byWorkspace = new LinkedHashMap<>();
        for (String payload : payloads) {
            ReindexTask task;
            try {
                task = mapper.readValue(payload, ReindexTask.class);
            } catch (JacksonException ex) {
                // 毒消息直投 DLQ（不抛：抛了整批陪葬），消费继续
                log.error("poison reindex message dead-lettered: {}", ex.getMessage());
                try {
                    rabbit.convertAndSend(AmqpConfig.INDEXING_DLX,
                            AmqpConfig.INDEXING_ROUTING_KEY, payload);
                } catch (AmqpException dlqEx) {
                    log.error("could not dead-letter poison message: {}", dlqEx.getMessage());
                }
                continue;
            }
            byWorkspace.computeIfAbsent(task.workspaceId(), key -> new ArrayList<>()).add(task);
        }

        for (Map.Entry<String, List<ReindexTask>> entry : byWorkspace.entrySet()) {
            indexGroup(entry.getKey(), entry.getValue());
        }
    }

    /** 同一工作区的一批任务：按 relPath 重新定位（apply 期间可能被并发修改）后批量索引。 */
    private void indexGroup(String workspaceId, List<ReindexTask> tasks) {
        List<String[]> resolved = new ArrayList<>();
        for (ReindexTask task : tasks) {
            files.findByRelPath(workspaceId, task.relPath()).ifPresent(
                    file -> resolved.add(new String[] {file.id(), file.relPath()}));
        }
        int missing = tasks.size() - resolved.size();
        if (missing > 0) {
            log.warn("{} reindex target(s) no longer exist in workspace {}", missing, workspaceId);
        }
        if (resolved.isEmpty()) {
            return;
        }

        try {
            List<tools.jackson.databind.JsonNode> results = aiService.indexBatch(workspaceId, resolved);
            for (int i = 0; i < resolved.size() && i < results.size(); i++) {
                indexing.applyIndexResult(workspaceId, resolved.get(i)[0], results.get(i));
            }
            if (results.size() != resolved.size()) {
                log.error("index-batch returned {} results for {} files in workspace {}",
                        results.size(), resolved.size(), workspaceId);
            }
        } catch (Exception ex) {
            log.warn("batch reindex failed for {} file(s) in {}: {}",
                    resolved.size(), workspaceId, ex.getMessage());
            for (String[] file : resolved) {
                files.markFailed(file[0], "reindex failed: " + ex.getMessage());
            }
        }
    }
}
