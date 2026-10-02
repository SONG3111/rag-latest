package com.raglatest.backend.api;

import com.raglatest.backend.service.CorpusService;
import com.raglatest.backend.store.WorkspaceRepository;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

/**
 * 内部读端点：仅 ai-service（内网）调用，不对前端暴露。
 *
 * 无状态聊天部署里 app.db 只被本进程打开，ai-service 的检索语料与
 * list_files 过滤清单从这里回读（对应其 httpx corpus_loader，每次 search 一次）。
 * 语料经 {@link CorpusService} 的 Redis 缓存（写路径显式失效 + 短 TTL 兜底），
 * 返回类型从 CorpusRecord 改为 JsonNode——响应形状不变（snake_case 已固化在树里）。
 */
@RestController
public class InternalController {

    private final WorkspaceRepository workspaces;
    private final CorpusService corpus;

    public InternalController(WorkspaceRepository workspaces, CorpusService corpus) {
        this.workspaces = workspaces;
        this.corpus = corpus;
    }

    @GetMapping("/internal/workspaces/{workspaceId}/retrieval-corpus")
    public JsonNode retrievalCorpus(@PathVariable String workspaceId) {
        if (!workspaces.exists(workspaceId)) {
            throw new ApiException(HttpStatus.NOT_FOUND, "workspace not found");
        }
        return corpus.load(workspaceId);
    }
}
