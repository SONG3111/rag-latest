package com.raglatest.backend.service;

import com.raglatest.backend.config.CacheConfig;
import com.raglatest.backend.store.ChunkRepository;
import org.springframework.cache.annotation.Cacheable;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 检索语料的缓存读：ai-service 每轮聊天都回读一次全量分块（其 pipeline 内做
 * memoize，但跨轮仍逐次打到本进程），这是 Redis 化收益最大的读路径。
 *
 * <p>失效纪律：重建索引（IndexingService.index 的 @CacheEvict）、删文件/删工作区
 * 都显式 evict，60s TTL 只是兜底——检索宁可重读也不得用旧语料。</p>
 */
@Service
public class CorpusService {

    private final ChunkRepository chunks;
    private final ObjectMapper mapper;

    public CorpusService(ChunkRepository chunks, ObjectMapper mapper) {
        this.chunks = chunks;
        this.mapper = mapper;
    }

    /** 树化后返回：缓存值与响应同构（JsonNode），命中即原样透传给 ai-service。 */
    @Cacheable(cacheNames = CacheConfig.RETRIEVAL_CORPUS, key = "#workspaceId")
    public JsonNode load(String workspaceId) {
        return mapper.valueToTree(chunks.loadCorpus(workspaceId));
    }
}
