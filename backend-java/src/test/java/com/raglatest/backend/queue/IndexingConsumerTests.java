package com.raglatest.backend.queue;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.argThat;
import static org.mockito.ArgumentMatchers.contains;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.service.IndexingService;
import com.raglatest.backend.store.DocumentFileRepository;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.JsonNodeFactory;

/**
 * 批量消费者的异常语义（对标旧内存 executor 时代 reindexAfterWrite 的兜底行为）：
 * 毒消息直投 DLQ 不抛（抛了整批陪葬）、整批调用失败逐文件 markFailed 后正常 ack、
 * 跨工作区分组、消失的目标跳过。
 */
class IndexingConsumerTests {

    private final DocumentFileRepository files = Mockito.mock(DocumentFileRepository.class);
    private final IndexingService indexing = Mockito.mock(IndexingService.class);
    private final AiServiceClient aiService = Mockito.mock(AiServiceClient.class);
    private final RabbitTemplate rabbit = Mockito.mock(RabbitTemplate.class);
    private final ObjectMapper mapper = new ObjectMapper();
    private static final JsonNodeFactory JSON = JsonNodeFactory.instance;

    private final IndexingConsumer consumer =
            new IndexingConsumer(files, indexing, aiService, rabbit, mapper);

    private static final FileRead FILE_A = new FileRead(
            "f1", "a.xlsx", "excel", 8L, "indexing", 0, null, null, Instant.now());
    private static final FileRead FILE_B = new FileRead(
            "f2", "b.xlsx", "excel", 8L, "indexing", 0, null, null, Instant.now());

    private String payload(String ws, String fileId, String relPath) {
        return mapper.writeValueAsString(new ReindexTask(ws, fileId, relPath));
    }

    @Test
    void groupsByWorkspaceAndCallsBatchIndexPerGroup() {
        when(files.findByRelPath("ws1", "a.xlsx")).thenReturn(Optional.of(FILE_A));
        when(files.findByRelPath("ws2", "b.xlsx")).thenReturn(Optional.of(FILE_B));
        when(aiService.indexBatch(eq("ws1"), anyList()))
                .thenReturn(List.of(JSON.objectNode().put("status", "indexed")));
        when(aiService.indexBatch(eq("ws2"), anyList()))
                .thenReturn(List.of(JSON.objectNode().put("status", "indexed")));

        consumer.onReindexTasks(List.of(
                payload("ws1", "f1", "a.xlsx"), payload("ws2", "f2", "b.xlsx")));

        // 两个工作区各一次批量调用，结果按序落到对应文件
        verify(aiService).indexBatch(eq("ws1"), argThat(l ->
                l.size() == 1 && "f1".equals(l.get(0)[0]) && "a.xlsx".equals(l.get(0)[1])));
        verify(aiService).indexBatch(eq("ws2"), argThat(l ->
                l.size() == 1 && "f2".equals(l.get(0)[0]) && "b.xlsx".equals(l.get(0)[1])));
        verify(indexing).applyIndexResult(eq("ws1"), eq("f1"), any());
        verify(indexing).applyIndexResult(eq("ws2"), eq("f2"), any());
    }

    @Test
    void missingTargetsAreSkippedWithoutFailure() {
        when(files.findByRelPath("ws", "gone.xlsx")).thenReturn(Optional.empty());

        consumer.onReindexTasks(List.of(payload("ws", "f9", "gone.xlsx")));

        verify(aiService, never()).indexBatch(anyString(), anyList());
        verify(files, never()).markFailed(anyString(), anyString());
    }

    @Test
    void batchFailureMarksEachFileFailedInsteadOfRequeueing() {
        when(files.findByRelPath("ws", "a.xlsx")).thenReturn(Optional.of(FILE_A));
        when(files.findByRelPath("ws", "b.xlsx")).thenReturn(Optional.of(FILE_B));
        when(aiService.indexBatch(eq("ws"), anyList()))
                .thenThrow(new IllegalStateException("ai-service exploded"));

        consumer.onReindexTasks(List.of(
                payload("ws", "f1", "a.xlsx"), payload("ws", "f2", "b.xlsx")));

        verify(files).markFailed(eq("f1"), contains("ai-service exploded"));
        verify(files).markFailed(eq("f2"), contains("ai-service exploded"));
    }

    @Test
    void poisonMessageIsDeadLetteredAndBatchContinues() {
        when(files.findByRelPath("ws", "a.xlsx")).thenReturn(Optional.of(FILE_A));
        when(aiService.indexBatch(eq("ws"), anyList()))
                .thenReturn(List.of(JSON.objectNode().put("status", "indexed")));

        consumer.onReindexTasks(List.of(
                "not-json{", payload("ws", "f1", "a.xlsx")));

        // 毒消息直投 DLQ（DLX 路由），同批的好文件照常处理
        verify(rabbit).convertAndSend(
                eq(AmqpConfig.INDEXING_DLX), eq(AmqpConfig.INDEXING_ROUTING_KEY), eq("not-json{"));
        verify(indexing).applyIndexResult(eq("ws"), eq("f1"), any());
        verify(files, never()).markFailed(anyString(), anyString());
    }

    @Test
    void resultCountMismatchIsLoggedAndGoodPrefixStillApplied() {
        when(files.findByRelPath("ws", "a.xlsx")).thenReturn(Optional.of(FILE_A));
        when(files.findByRelPath("ws", "b.xlsx")).thenReturn(Optional.of(FILE_B));
        // ai-service 只回了 1 个结果（异常场景）：前缀照常落地，缺的记 error
        when(aiService.indexBatch(eq("ws"), anyList()))
                .thenReturn(List.of(JSON.objectNode().put("status", "indexed")));

        consumer.onReindexTasks(List.of(
                payload("ws", "f1", "a.xlsx"), payload("ws", "f2", "b.xlsx")));

        verify(indexing).applyIndexResult(eq("ws"), eq("f1"), any());
        verify(indexing, never()).applyIndexResult(eq("ws"), eq("f2"), any());
    }
}
