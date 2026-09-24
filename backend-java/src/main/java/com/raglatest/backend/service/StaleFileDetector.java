package com.raglatest.backend.service;

import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.config.RagProperties;
import com.raglatest.backend.store.DocumentFileRepository;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.FileTime;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Service;

/**
 * 「上一轮之后哪些文件变了」的判定，移植自 python api/routes.py 的
 * _files_changed_since_last_turn：indexed_at/created_at/磁盘 mtime 三者取最大，
 * 晚于最后一条消息即视为已变更（外部编辑同样作废此前的回答）。时间统一用 UTC
 * 比较——存储与 SQLite 侧都是 UTC 时间戳，语义同 Python 的 naive 比较版。
 */
@Service
public class StaleFileDetector {

    /** (rel_path, changed_at 文本) —— 与 python 版的元组形状一致。 */
    public record StaleFile(String relPath, String changedAt) {}

    private static final DateTimeFormatter FORMAT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss").withZone(ZoneOffset.UTC);

    private final DocumentFileRepository files;
    private final Path workspacesRoot;

    public StaleFileDetector(DocumentFileRepository files, RagProperties properties) {
        this.files = files;
        this.workspacesRoot = Path.of(properties.dataDir(), "workspaces");
    }

    public List<StaleFile> changedSince(String workspaceId, Instant cutoff) {
        List<StaleFile> changed = new ArrayList<>();
        for (FileRead record : files.listByWorkspace(workspaceId)) {
            Instant latest = latestMoment(workspaceId, record);
            if (latest == null) {
                continue;
            }
            if (cutoff == null || latest.isAfter(cutoff)) {
                changed.add(new StaleFile(record.relPath(), FORMAT.format(latest)));
            }
        }
        return changed;
    }

    private Instant latestMoment(String workspaceId, FileRead record) {
        Instant latest = null;
        for (Instant moment : new Instant[] {record.indexedAt(), record.createdAt()}) {
            if (moment != null && (latest == null || moment.isAfter(latest))) {
                latest = moment;
            }
        }
        // An edit made outside the app still invalidates earlier answers.
        Path path = workspacesRoot.resolve(workspaceId).resolve(record.relPath());
        if (Files.exists(path)) {
            try {
                FileTime mtime = Files.getLastModifiedTime(path);
                if (latest == null || mtime.toInstant().isAfter(latest)) {
                    latest = mtime.toInstant();
                }
            } catch (Exception ignored) {
                // 磁盘不可读：退回已知的数据库时间戳。
            }
        }
        return latest;
    }
}
