package com.raglatest.backend.service;

import com.raglatest.backend.config.RagProperties;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.ZoneOffset;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.util.Comparator;
import java.util.List;
import org.springframework.stereotype.Service;

/**
 * 写入前的文件备份。语义移植自 Python services/backup.py：
 * 每次获批写入前先复制原文件，使变更可一键还原；按工作区保留最近 N 份。
 */
@Service
public class BackupService {

    private static final DateTimeFormatter STAMP =
            DateTimeFormatter.ofPattern("yyyyMMdd'T'HHmmssSSSSSS").withZone(ZoneOffset.UTC);

    private final Path backupsRoot;

    public BackupService(RagProperties properties) {
        this.backupsRoot = Path.of(properties.dataDir()).resolve("backups");
    }

    /** 把 source 复制进备份树，返回新备份路径。 */
    public Path backup(String workspaceId, Path source) throws IOException {
        Path dir = backupsRoot.resolve(workspaceId);
        Files.createDirectories(dir);
        String name = ZonedDateTime.now().format(STAMP) + "__" + source.getFileName();
        Path target = dir.resolve(name);
        Files.copy(source, target, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.COPY_ATTRIBUTES);
        return target;
    }

    /** 把备份复制回目标路径。 */
    public void restore(Path backupPath, Path destination) throws IOException {
        if (!Files.exists(backupPath)) {
            throw new IOException("backup not found: " + backupPath);
        }
        Files.createDirectories(destination.getParent());
        Files.copy(backupPath, destination,
                StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.COPY_ATTRIBUTES);
    }

    /** 只保留每个工作区最近 keep 份备份，返回删除数量。 */
    public int prune(String workspaceId, int keep) {
        Path dir = backupsRoot.resolve(workspaceId);
        if (!Files.isDirectory(dir)) {
            return 0;
        }
        List<Path> files;
        try (var stream = Files.list(dir)) {
            files = stream.filter(Files::isRegularFile)
                    .sorted(Comparator.comparingLong(BackupService::mtime).reversed())
                    .toList();
        } catch (IOException ex) {
            return 0;
        }
        int removed = 0;
        for (Path path : files.subList(Math.min(keep, files.size()), files.size())) {
            try {
                Files.deleteIfExists(path);
                removed++;
            } catch (IOException ignored) {
                // 尽力删
            }
        }
        return removed;
    }

    private static long mtime(Path path) {
        try {
            return Files.getLastModifiedTime(path).toMillis();
        } catch (IOException ex) {
            return 0L;
        }
    }
}
