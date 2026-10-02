package com.raglatest.backend.config;

import java.time.Duration;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.cache.autoconfigure.RedisCacheManagerBuilderCustomizer;
import org.springframework.cache.Cache;
import org.springframework.cache.annotation.CachingConfigurer;
import org.springframework.cache.annotation.EnableCaching;
import org.springframework.cache.interceptor.CacheErrorHandler;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.redis.cache.RedisCacheConfiguration;
import org.springframework.data.redis.serializer.RedisSerializationContext;
import org.springframework.data.redis.serializer.RedisSerializer;
import org.springframework.data.redis.serializer.SerializationException;
import tools.jackson.databind.ObjectMapper;

/**
 * 读路径缓存（Redis）：按缓存名区分 TTL 的标准做法是
 * {@link RedisCacheManagerBuilderCustomizer}（Spring Boot 官方 Caching 文档明确
 * 无属性写法，此处迁移其示例；key 前缀默认保留，避免同名 key 串值）。
 *
 * <p>缓存值统一 JSON 字节：{@link tools.jackson.databind.JsonNode} 是全部缓存点的
 * 载体（ai-service 响应原文 + corpus 的树化结果），反序列化一律 readTree，
 * 规避 JDK 序列化对 record/JsonNode 的版本脆弱，也绕开 Spring Data Redis 尚无的
 * Jackson 3 官方序列化器。</p>
 *
 * <p>降级语义：Redis 读写失败经 {@link CacheErrorHandler} 记告警后吞掉——
 * 缓存 miss 照常穿透到 ai-service/SQLite，业务端点不因缓存抖动失败。</p>
 */
@Configuration
@EnableCaching
public class CacheConfig implements CachingConfigurer {

    private static final Logger log = LoggerFactory.getLogger(CacheConfig.class);

    public static final String AI_TOOLS = "aiTools";
    public static final String AI_PREVIEW = "aiPreview";
    public static final String AI_HEALTH = "aiHealth";
    public static final String RETRIEVAL_CORPUS = "retrievalCorpus";

    /** MCP 工具清单：变更频率低，5 分钟足够。 */
    public static final Duration TOOLS_TTL = Duration.ofMinutes(5);
    /** 引用预览：内容随写操作变化，apply/revert 会显式清空，TTL 兜底。 */
    public static final Duration PREVIEW_TTL = Duration.ofMinutes(5);
    /** 健康检查：短 TTL 保住“要快”的语义，只削峰不改结果。 */
    public static final Duration HEALTH_TTL = Duration.ofSeconds(10);
    /** 检索语料：写路径（重建索引/删文件）显式失效，60s 是兜底上限。 */
    public static final Duration CORPUS_TTL = Duration.ofSeconds(60);

    @Bean
    RedisCacheManagerBuilderCustomizer cacheTtlCustomizer(ObjectMapper mapper) {
        RedisSerializer<Object> json = new JsonNodeRedisSerializer(mapper);
        return builder -> builder
                .withCacheConfiguration(AI_TOOLS, config(json, TOOLS_TTL))
                .withCacheConfiguration(AI_PREVIEW, config(json, PREVIEW_TTL))
                .withCacheConfiguration(AI_HEALTH, config(json, HEALTH_TTL))
                .withCacheConfiguration(RETRIEVAL_CORPUS, config(json, CORPUS_TTL));
    }

    private static RedisCacheConfiguration config(RedisSerializer<Object> values, Duration ttl) {
        return RedisCacheConfiguration.defaultCacheConfig()
                .entryTtl(ttl)
                .serializeValuesWith(RedisSerializationContext.SerializationPair.fromSerializer(values));
    }

    /** Redis 故障降级：告警 + 穿透，缓存层永远不让业务调用失败。 */
    @Override
    public CacheErrorHandler errorHandler() {
        return new CacheErrorHandler() {
            @Override
            public void handleCacheGetError(RuntimeException ex, Cache cache, Object key) {
                log.warn("cache get failed on {}::{}: {}", cache.getName(), key, ex.getMessage());
            }

            @Override
            public void handleCachePutError(RuntimeException ex, Cache cache, Object key, Object value) {
                log.warn("cache put failed on {}::{}: {}", cache.getName(), key, ex.getMessage());
            }

            @Override
            public void handleCacheEvictError(RuntimeException ex, Cache cache, Object key) {
                log.warn("cache evict failed on {}::{}: {}", cache.getName(), key, ex.getMessage());
            }

            @Override
            public void handleCacheClearError(RuntimeException ex, Cache cache) {
                log.warn("cache clear failed on {}: {}", cache.getName(), ex.getMessage());
            }
        };
    }

    /** JSON 字节 ↔ JsonNode：全部缓存点的值载体（见类注释）。 */
    static final class JsonNodeRedisSerializer implements RedisSerializer<Object> {

        private final ObjectMapper mapper;

        JsonNodeRedisSerializer(ObjectMapper mapper) {
            this.mapper = mapper;
        }

        @Override
        public byte[] serialize(Object value) throws SerializationException {
            if (value == null) {
                return null;
            }
            try {
                return mapper.writeValueAsBytes(value);
            } catch (tools.jackson.core.JacksonException ex) {
                throw new SerializationException("could not serialize cache value", ex);
            }
        }

        @Override
        public Object deserialize(byte[] bytes) throws SerializationException {
            if (bytes == null) {
                return null;
            }
            try {
                return mapper.readTree(bytes);
            } catch (tools.jackson.core.JacksonException ex) {
                throw new SerializationException("could not deserialize cache value", ex);
            }
        }
    }
}
