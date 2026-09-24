package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.api.dto.Dtos.IndexingResponse;
import com.raglatest.backend.api.dto.Dtos.WorkspaceCreate;
import com.raglatest.backend.api.dto.Dtos.WorkspaceRead;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.service.FileStorage;
import com.raglatest.backend.service.IndexingService;
import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.WorkspaceRepository;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ContentDisposition;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.util.MimeTypeUtils;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.core.io.FileSystemResource;
import jakarta.validation.Valid;

/**
 * 工作区与文件的 CRUD。路径、状态码、错误 detail 语义对齐原 Python 版
 * ai-service/app/api/routes.py 的同名端点。
 */
@RestController
@RequestMapping("/api")
public class WorkspaceController {

    private static final Logger log = LoggerFactory.getLogger(WorkspaceController.class);

    private final WorkspaceRepository workspaces;
    private final DocumentFileRepository files;
    private final FileStorage storage;
    private final IndexingService indexing;
    private final AiServiceClient aiService;

    public WorkspaceController(WorkspaceRepository workspaces, DocumentFileRepository files,
                               FileStorage storage, IndexingService indexing,
                               AiServiceClient aiService) {
        this.workspaces = workspaces;
        this.files = files;
        this.storage = storage;
        this.indexing = indexing;
        this.aiService = aiService;
    }

    // ------------------------------------------------------------------ //
    // workspaces
    // ------------------------------------------------------------------ //
    @GetMapping("/workspaces")
    public List<WorkspaceRead> listWorkspaces() {
        return workspaces.list();
    }

    @PostMapping("/workspaces")
    public ResponseEntity<WorkspaceRead> createWorkspace(@Valid @RequestBody WorkspaceCreate payload) {
        WorkspaceRead workspace = workspaces.create(payload.name().strip(), payload.description());
        try {
            Files.createDirectories(storage.workspaceDir(workspace.id()));
        } catch (IOException ex) {
            throw new ApiException(HttpStatus.INTERNAL_SERVER_ERROR,
                    "could not create workspace directory: " + ex.getMessage());
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(workspace);
    }

    @GetMapping("/workspaces/{workspaceId}")
    public WorkspaceRead getWorkspace(@PathVariable String workspaceId) {
        return requireWorkspace(workspaceId);
    }

    @DeleteMapping("/workspaces/{workspaceId}")
    public ResponseEntity<Void> removeWorkspace(@PathVariable String workspaceId) {
        requireWorkspace(workspaceId);
        try {
            aiService.dropCollection(workspaceId);
        } catch (Exception ex) {
            // 索引是派生数据，删除工作区不被它阻塞（与 python 版一致）。
            log.warn("could not drop vector collection for {}: {}", workspaceId, ex.getMessage());
        }
        deleteDirectoryTree(storage.workspaceDir(workspaceId));
        workspaces.delete(workspaceId);
        return ResponseEntity.noContent().build();
    }

    // ------------------------------------------------------------------ //
    // files
    // ------------------------------------------------------------------ //
    @GetMapping("/workspaces/{workspaceId}/files")
    public List<FileRead> listFiles(@PathVariable String workspaceId) {
        requireWorkspace(workspaceId);
        return files.listByWorkspace(workspaceId);
    }

    /**
     * 上传即建索引：落盘登记后立即经 ai-service 的 /v1/index 解析、嵌入并落库分块。
     * 索引失败不影响上传本身（文件已保存，状态为 failed，可经 reindex 重试）。
     */
    @PostMapping("/workspaces/{workspaceId}/files")
    public ResponseEntity<List<IndexingResponse>> uploadFiles(
            @PathVariable String workspaceId,
            @RequestParam("files") List<MultipartFile> uploads) {
        requireWorkspace(workspaceId);
        List<IndexingResponse> results = new ArrayList<>();
        for (MultipartFile upload : uploads) {
            byte[] content;
            try {
                content = upload.getBytes();
            } catch (IOException ex) {
                throw ApiException.badRequest("could not read upload: " + ex.getMessage());
            }
            FileStorage.StoredUpload stored;
            try {
                stored = storage.save(workspaceId, upload.getOriginalFilename(), content);
            } catch (FileStorage.IngestionException ex) {
                throw ApiException.badRequest(ex.getMessage());
            }
            FileRead record = files.insertPending(
                    workspaceId, stored.relPath(), stored.kind(),
                    stored.sizeBytes(), stored.checksum());
            results.add(indexing.index(workspaceId, record.id()));
        }
        return ResponseEntity.status(HttpStatus.CREATED).body(results);
    }

    /** 重建单个文件的索引（分块策略或模型变更后使用）。 */
    @PostMapping("/workspaces/{workspaceId}/files/{fileId}/reindex")
    public IndexingResponse reindexFile(@PathVariable String workspaceId,
                                        @PathVariable String fileId) {
        requireWorkspace(workspaceId);
        return indexing.index(workspaceId, fileId);
    }

    /** 重建整个工作区的索引：分块/嵌入变更后，从源文件重建（派生数据不就地修补）。 */
    @PostMapping("/workspaces/{workspaceId}/reindex")
    public List<IndexingResponse> reindexWorkspace(@PathVariable String workspaceId) {
        requireWorkspace(workspaceId);
        List<IndexingResponse> results = new ArrayList<>();
        for (FileRead file : files.listByWorkspace(workspaceId)) {
            results.add(indexing.index(workspaceId, file.id()));
        }
        return results;
    }

    @DeleteMapping("/workspaces/{workspaceId}/files/{fileId}")
    public ResponseEntity<Void> removeFile(@PathVariable String workspaceId, @PathVariable String fileId) {
        requireWorkspace(workspaceId);
        FileRead record = files.findInWorkspace(workspaceId, fileId)
                .orElseThrow(() -> ApiException.notFound("file not found"));
        try {
            aiService.deleteFileVectors(workspaceId, fileId);
        } catch (Exception ex) {
            log.warn("could not remove {} from the vector index: {}", fileId, ex.getMessage());
        }
        storage.deleteQuietly(workspaceId, record.relPath());
        files.delete(fileId);
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/workspaces/{workspaceId}/files/{fileId}/download")
    public ResponseEntity<FileSystemResource> downloadFile(
            @PathVariable String workspaceId, @PathVariable String fileId) {
        requireWorkspace(workspaceId);
        FileRead record = files.findInWorkspace(workspaceId, fileId)
                .orElseThrow(() -> ApiException.notFound("file not found"));
        Path path = storage.resolveWorkspacePath(workspaceId, record.relPath());
        if (!Files.exists(path)) {
            throw ApiException.notFound("file is missing from disk");
        }
        HttpHeaders headers = new HttpHeaders();
        headers.setContentType(MediaType.parseMediaType(
                MediaTypeFactoryProxy.guess(record.relPath())));
        headers.setContentDisposition(ContentDisposition.attachment()
                .filename(record.relPath(), java.nio.charset.StandardCharsets.UTF_8)
                .build());
        return ResponseEntity.ok().headers(headers).body(new FileSystemResource(path));
    }

    WorkspaceRead requireWorkspace(String workspaceId) {
        return workspaces.find(workspaceId)
                .orElseThrow(() -> ApiException.notFound("workspace not found"));
    }

    static void deleteDirectoryTree(Path directory) {
        if (!Files.exists(directory)) {
            return;
        }
        try (var paths = Files.walk(directory)) {
            paths.sorted(Comparator.reverseOrder()).forEach(p -> {
                try {
                    Files.delete(p);
                } catch (IOException ignored) {
                    // rmtree(ignore_errors=True) 语义：尽力删
                }
            });
        } catch (IOException ex) {
            log.warn("could not remove directory {}: {}", directory, ex.getMessage());
        }
    }

    /** 仅为 download 的 Content-Type 猜测，避免引入完整 mimetypes 表。 */
    private static final class MediaTypeFactoryProxy {
        static String guess(String relPath) {
            String lower = relPath.toLowerCase();
            if (lower.endsWith(".xlsx") || lower.endsWith(".xlsm")) {
                return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
            }
            if (lower.endsWith(".docx")) {
                return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
            }
            return MimeTypeUtils.APPLICATION_OCTET_STREAM_VALUE;
        }
    }
}
