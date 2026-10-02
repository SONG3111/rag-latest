package com.raglatest.backend.queue;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.service.IndexingService;
import com.raglatest.backend.store.DocumentFileRepository;
import java.time.Instant;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;
import tools.jackson.databind.ObjectMapper;

/**
 * 索引消费者的异常语义（对标旧内存 executor 时代 reindexAfterWrite 的兜底行为）：
 * 业务失败 markFailed 后正常 ack（毒不了队列），毒消息（无法反序列化）抛出
 * 交给 DLQ——@RabbitListener 配 default-requeue-rejected=false，不会无限 requeue。
 */
class IndexingConsumerTests {

    private final DocumentFileRepository files = Mockito.mock(DocumentFileRepository.class);
    private final IndexingService indexing = Mockito.mock(IndexingService.class);
    private final ObjectMapper mapper = new ObjectMapper();

    private final IndexingConsumer consumer = new IndexingConsumer(files, indexing, mapper);

    private static final FileRead FILE = new FileRead(
            "f1", "a.xlsx", "excel", 8L, "indexing", 0, null, null, Instant.now());

    private String payload(String relPath) {
        return mapper.writeValueAsString(new ReindexTask("ws", "f1", relPath));
    }

    @Test
    void relooksUpFileByRelPathAndIndexes() {
        when(files.findByRelPath("ws", "a.xlsx")).thenReturn(Optional.of(FILE));

        consumer.onReindexTask(payload("a.xlsx"));

        verify(indexing).index("ws", "f1");
    }

    @Test
    void missingTargetIsSkippedWithoutFailure() {
        when(files.findByRelPath("ws", "gone.xlsx")).thenReturn(Optional.empty());

        consumer.onReindexTask(payload("gone.xlsx"));

        verify(indexing, never()).index(any(), any());
        verify(files, never()).markFailed(any(), any());
    }

    @Test
    void unexpectedFailureMarksFileFailedInsteadOfRequeueing() {
        when(files.findByRelPath("ws", "a.xlsx")).thenReturn(Optional.of(FILE));
        doThrow(new IllegalStateException("db exploded")).when(indexing).index("ws", "f1");

        consumer.onReindexTask(payload("a.xlsx"));

        verify(files).markFailed(eq("f1"), org.mockito.ArgumentMatchers.contains("db exploded"));
    }

    @Test
    void poisonMessagePropagatesForDeadLettering() {
        assertThatThrownBy(() -> consumer.onReindexTask("not-json{"))
                .isInstanceOf(tools.jackson.core.JacksonException.class);
        verify(indexing, never()).index(any(), any());
        verify(files, never()).markFailed(any(), any());
    }
}
