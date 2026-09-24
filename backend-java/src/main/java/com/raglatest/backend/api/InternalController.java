package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.Dtos.CorpusResponse;
import com.raglatest.backend.store.ChunkRepository;
import com.raglatest.backend.store.WorkspaceRepository;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

/**
 * 内部读端点：仅 ai-service（内网）调用，不对前端暴露。
 *
 * 无状态聊天部署里 app.db 只被本进程打开，ai-service 的检索语料与
 * list_files 过滤清单从这里回读（对应其 httpx corpus_loader，每次 search 一次）。
 */
@RestController
public class InternalController {

    private final WorkspaceRepository workspaces;
    private final ChunkRepository chunks;

    public InternalController(WorkspaceRepository workspaces, ChunkRepository chunks) {
        this.workspaces = workspaces;
        this.chunks = chunks;
    }

    @GetMapping("/internal/workspaces/{workspaceId}/retrieval-corpus")
    public CorpusResponse retrievalCorpus(@PathVariable String workspaceId) {
        if (!workspaces.exists(workspaceId)) {
            throw new ApiException(HttpStatus.NOT_FOUND, "workspace not found");
        }
        return chunks.loadCorpus(workspaceId);
    }
}
