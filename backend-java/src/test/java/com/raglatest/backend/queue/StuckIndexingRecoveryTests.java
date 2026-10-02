package com.raglatest.backend.queue;

import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.DocumentFileRepository.FileStuck;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.mockito.Mockito;

/** 启动兜底：卡在 pending/indexing 的文件重新入队（消息之外的残留状态靠它恢复）。 */
class StuckIndexingRecoveryTests {

    private final DocumentFileRepository files = Mockito.mock(DocumentFileRepository.class);
    private final ReindexProducer reindexQueue = Mockito.mock(ReindexProducer.class);

    private final StuckIndexingRecovery recovery = new StuckIndexingRecovery(files, reindexQueue);

    @Test
    void requeuesPendingAndIndexingFiles() {
        when(files.findByStatuses("pending", "indexing")).thenReturn(List.of(
                new FileStuck("ws1", "f1", "a.xlsx"),
                new FileStuck("ws2", "f2", "b.xlsx")));

        recovery.run(null);

        verify(reindexQueue).enqueue("ws1", "f1", "a.xlsx");
        verify(reindexQueue).enqueue("ws2", "f2", "b.xlsx");
    }

    @Test
    void nothingStuckMeansNoEnqueue() {
        when(files.findByStatuses("pending", "indexing")).thenReturn(List.of());

        recovery.run(null);

        verify(reindexQueue, Mockito.never()).enqueue(Mockito.any(), Mockito.any(), Mockito.any());
    }
}
