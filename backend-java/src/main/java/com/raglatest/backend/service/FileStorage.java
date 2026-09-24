package com.raglatest.backend.service;

import com.raglatest.backend.config.RagProperties;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.text.Normalizer;
import java.util.HexFormat;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;
import org.springframework.stereotype.Service;

/**
 * 工作区文件落盘，语义移植自 Python services/files.py 的存储半边
 * （sanitize / 唯一命名 / sha256 / 沙箱路径解析）。索引半边在 ai-service。
 */
@Service
public class FileStorage {

    private static final Set<String> SUPPORTED_SUFFIXES = Set.of(".xlsx", ".xlsm", ".docx");
    private static final long MAX_UPLOAD_BYTES = 200L * 1024 * 1024;
    private static final Pattern UNSAFE_CHARS = Pattern.compile("[<>:\"/\\\\|?*\\x00-\\x1f]");

    /** 与 python IngestionError 对应，映射为 400。 */
    public static class IngestionException extends RuntimeException {
        public IngestionException(String message) {
            super(message);
        }
    }

    public record StoredUpload(String relPath, String kind, long sizeBytes, String checksum) {}

    private final Path workspacesDir;

    public FileStorage(RagProperties properties) {
        this.workspacesDir = Path.of(properties.dataDir()).resolve("workspaces");
    }

    public Path workspaceDir(String workspaceId) {
        return workspacesDir.resolve(workspaceId);
    }

    /** 存上传内容并返回登记所需元数据；文件名永不信任为路径（浏览器送什么都有）。 */
    public StoredUpload save(String workspaceId, String filename, byte[] content) {
        if (content.length > MAX_UPLOAD_BYTES) {
            throw new IngestionException(
                    "file exceeds the " + (MAX_UPLOAD_BYTES / (1024 * 1024)) + "MB upload limit");
        }
        if (content.length == 0) {
            throw new IngestionException("uploaded file is empty");
        }
        String safeName = sanitizeFilename(filename);
        String suffix = suffixOf(safeName);
        if (!SUPPORTED_SUFFIXES.contains(suffix)) {
            throw new IngestionException("unsupported file type '"
                    + (suffix.isEmpty() ? "<none>" : suffix) + "'; supported: .docx .xlsm .xlsx");
        }
        try {
            Path dir = workspaceDir(workspaceId);
            Files.createDirectories(dir);
            String relPath = uniqueRelPath(dir, safeName);
            Path target = dir.resolve(relPath);
            Files.write(target, content);
            return new StoredUpload(relPath,
                    suffix.equals(".docx") ? "word" : "excel",
                    content.length, sha256Hex(content));
        } catch (IOException ex) {
            throw new IngestionException("could not store upload: " + ex.getMessage());
        }
    }

    /** 解析工作区相对路径，拒绝任何逃逸根目录的输入。 */
    public Path resolveWorkspacePath(String workspaceId, String relPath) {
        Path root = workspaceDir(workspaceId).toAbsolutePath().normalize();
        Path candidate = root.resolve(relPath).toAbsolutePath().normalize();
        if (!candidate.startsWith(root)) {
            throw new IngestionException("path escapes the workspace: " + relPath);
        }
        return candidate;
    }

    public void deleteQuietly(String workspaceId, String relPath) {
        try {
            Files.deleteIfExists(resolveWorkspacePath(workspaceId, relPath));
        } catch (IOException ex) {
            // 磁盘缺失不阻塞删除登记（与 python unlink(missing_ok=True) 一致）
        }
    }

    static String sanitizeFilename(String name) {
        String base = basename(name);
        base = Normalizer.normalize(base, Normalizer.Form.NFKC).strip();
        base = UNSAFE_CHARS.matcher(base).replaceAll("_");
        base = stripLeadingTrailing(base, ". ".toCharArray(), ". ".toCharArray());
        if (base.isEmpty()) {
            throw new IngestionException("filename is empty after sanitization");
        }
        return base.length() > 200 ? base.substring(0, 200) : base;
    }

    private static String basename(String name) {
        if (name == null) {
            return "";
        }
        String normalized = name.replace('\\', '/');
        int slash = normalized.lastIndexOf('/');
        return slash >= 0 ? normalized.substring(slash + 1) : normalized;
    }

    /** python base.strip(". ")：两端同时剥离点与空格。 */
    private static String stripLeadingTrailing(String value, char[] leading, char[] trailing) {
        int start = 0;
        while (start < value.length() && matchesAny(value.charAt(start), leading)) {
            start++;
        }
        int end = value.length();
        while (end > start && matchesAny(value.charAt(end - 1), trailing)) {
            end--;
        }
        return value.substring(start, end);
    }

    private static boolean matchesAny(char c, char[] chars) {
        for (char candidate : chars) {
            if (candidate == c) {
                return true;
            }
        }
        return false;
    }

    private static String suffixOf(String name) {
        int dot = name.lastIndexOf('.');
        return dot < 0 ? "" : name.substring(dot).toLowerCase(Locale.ROOT);
    }

    /** 撞名时追加 "(1)"、"(2)"…，避免覆盖已有上传。 */
    private static String uniqueRelPath(Path dir, String filename) {
        int dot = filename.lastIndexOf('.');
        String stem = dot < 0 ? filename : filename.substring(0, dot);
        String suffix = dot < 0 ? "" : filename.substring(dot);
        String candidate = filename;
        int counter = 1;
        while (Files.exists(dir.resolve(candidate))) {
            candidate = stem + "(" + counter + ")" + suffix;
            counter++;
        }
        return candidate;
    }

    static String sha256Hex(byte[] content) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(content));
        } catch (NoSuchAlgorithmException ex) {
            throw new IllegalStateException(ex);
        }
    }
}
