package com.raglatest.backend.queue;

/**
 * 索引任务的消息体：JSON 字符串经 AMQP 传输（序列化见 {@link ReindexProducer}）。
 *
 * <p>relPath 与 fileId 同时携带：消费端仍按 relPath 重新定位文件（apply 期间
 * 行可能被并发修改，与旧内存 executor 时代的 {@code findByRelPath} 语义一致），
 * fileId 供失败兜底直接 {@code markFailed}。</p>
 */
public record ReindexTask(String workspaceId, String fileId, String relPath) {}
