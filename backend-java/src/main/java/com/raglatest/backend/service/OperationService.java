package com.raglatest.backend.service;

import com.raglatest.backend.api.ApiException;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.OperationRepository;
import com.raglatest.backend.store.OperationRepository.OperationRow;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 审批闭环的编排：加锁备份 → 真正执行写工具 → 标记结果 → 重排同文件后续提案 →
 * 刷新它们的乐观锁 → 后台重建该文件的索引。
 *
 * <p>MCP Server 无状态、调用即写；审批门在本层——apply 才真正写文件，reject 不碰文件，
 * revert 用备份还原。语义移植自 Python services/operations.py，其中坐标重排
 * （_plan_rebase / _rebase_arguments / _refresh_diff_before / _apply_rebase_plan）
 * 见 {@link OperationRebase} 与本类的 planRebase/refreshDiffBefore。</p>
 */
@Service
public class OperationService {

    private static final Logger log = LoggerFactory.getLogger(OperationService.class);

    /** 工具参数里可能携带目标路径的键（与 MCP Schema 一致）。 */
    private static final List<String> PATH_ARGUMENTS = List.of("path", "filepath", "file_path");

    /** 目标行已被删掉的提案自动驳回时的提示（与 Python 版逐字一致）。 */
    private static final String REBASE_REJECT_ERROR =
            "提案的目标行已随先前应用的删除操作一起消失，无法自动重定位，已自动驳回；"
                    + "请让模型基于最新文件重新提交提案";

    private final OperationRepository operations;
    private final AiServiceClient aiService;
    private final FileStorage storage;
    private final BackupService backups;
    private final DocumentFileRepository files;
    private final IndexingService indexing;
    private final ObjectMapper mapper;
    /** 写后重建索引的后台执行器：本地嵌入耗时数秒，确认按钮不该等它。 */
    private final ExecutorService reindexExecutor;

    public OperationService(OperationRepository operations, AiServiceClient aiService,
                            FileStorage storage, BackupService backups,
                            DocumentFileRepository files, IndexingService indexing,
                            ObjectMapper mapper) {
        this.operations = operations;
        this.aiService = aiService;
        this.storage = storage;
        this.backups = backups;
        this.files = files;
        this.indexing = indexing;
        this.mapper = mapper;
        this.reindexExecutor = Executors.newSingleThreadExecutor(runnable -> {
            Thread thread = new Thread(runnable, "reindex-after-write");
            thread.setDaemon(true);
            return thread;
        });
    }

    /** 备份目标文件后真正执行写操作；失败时登记原因并抛出可读错误。 */
    public OperationRow apply(String workspaceId, String operationId) {
        OperationRow op = operations.find(workspaceId, operationId)
                .orElseThrow(() -> ApiException.notFound("operation not found"));
        if (!"proposed".equals(op.status()) && !"failed".equals(op.status())) {
            throw new ApiException(HttpStatus.CONFLICT,
                    "operation is already " + op.status() + "; nothing to apply");
        }
        // 失败的 apply 允许重试——常见原因（文件被 Excel 锁住、冲突已被别的编辑化解）
        // 都是瞬时的，重试仍受 digest 校验保护。

        Path source = storage.resolveWorkspacePath(workspaceId, op.relPath());
        if (!Files.exists(source)) {
            operations.markFailed(operationId, "the target file no longer exists");
            throw new ApiException(HttpStatus.CONFLICT, "the target file no longer exists");
        }

        String backupPath;
        try {
            backupPath = backups.backup(workspaceId, source).toString();
        } catch (IOException ex) {
            operations.markFailed(operationId, "backup failed: " + ex.getMessage());
            throw new ApiException(HttpStatus.INTERNAL_SERVER_ERROR,
                    "could not back up the file: " + ex.getMessage());
        }

        JsonNode payload;
        try {
            payload = aiService.callTool(op.toolName(), buildToolArguments(workspaceId, op.arguments()));
        } catch (ApiException ex) {
            operations.markFailed(operationId, "tool invocation failed: " + ex.getMessage());
            throw ex;
        }

        if (!payload.path("ok").asBoolean(false)) {
            JsonNode error = payload.get("error");
            String message = error != null && error.isObject()
                    ? error.path("message").asString(error.toString())
                    : (error == null || error.isNull() ? "unknown tool error" : error.asString());
            operations.markFailed(operationId, message);
            throw new ApiException(HttpStatus.CONFLICT, message);
        }

        JsonNode data = payload.path("data");
        JsonNode changes = data.path("changes");
        JsonNode diff = changes.isArray() && !changes.isEmpty() ? changes : op.diff();
        operations.markApplied(operationId, data, diff, backupPath);
        // 先落重排再刷新链式 digest：refresh 必须落在重排后的参数上，
        // digest 永远是参数里最新的那个值，而不是被重排恢复的旧值。
        applyRebasePlan(planRebase(workspaceId, op));
        refreshChainedDigests(workspaceId, operationId, op.relPath(), data.path("digest"));
        backups.prune(workspaceId, 50);
        reindexAfterWrite(workspaceId, op.relPath());
        log.info("applied operation {} ({})", operationId, op.toolName());
        return operations.find(workspaceId, operationId).orElseThrow();
    }

    public OperationRow reject(String workspaceId, String operationId) {
        OperationRow op = operations.find(workspaceId, operationId)
                .orElseThrow(() -> ApiException.notFound("operation not found"));
        if (!"proposed".equals(op.status())) {
            throw new ApiException(HttpStatus.CONFLICT,
                    "operation is already " + op.status() + "; nothing to reject");
        }
        operations.markRejected(operationId, null);
        return operations.find(workspaceId, operationId).orElseThrow();
    }

    public OperationRow revert(String workspaceId, String operationId) {
        OperationRow op = operations.find(workspaceId, operationId)
                .orElseThrow(() -> ApiException.notFound("operation not found"));
        if (!"applied".equals(op.status())) {
            throw new ApiException(HttpStatus.CONFLICT, "only applied operations can be reverted");
        }
        if (op.backupPath() == null || op.backupPath().isBlank()) {
            throw new ApiException(HttpStatus.CONFLICT, "no backup was recorded for this operation");
        }
        Path destination = storage.resolveWorkspacePath(workspaceId, op.relPath());
        try {
            backups.restore(Path.of(op.backupPath()), destination);
        } catch (IOException ex) {
            throw new ApiException(HttpStatus.INTERNAL_SERVER_ERROR,
                    "could not restore the backup: " + ex.getMessage());
        }
        operations.markRejected(operationId, "reverted to the pre-change backup");
        reindexAfterWrite(workspaceId, op.relPath());
        log.info("reverted operation {}", operationId);
        return operations.find(workspaceId, operationId).orElseThrow();
    }

    // ------------------------------------------------------------------ //
    // 坐标重排（_plan_rebase / _refresh_diff_before / _apply_rebase_plan）
    // ------------------------------------------------------------------ //

    /** 重排计划的一项：newArguments 为 null 表示该提案必须自动驳回。 */
    private record RebasePlanEntry(OperationRow operation, ObjectNode newArguments, JsonNode diff) {}

    /**
     * 在任何数据库写入前算出每个待确认提案应用后的新形态。
     *
     * <p>网络读（read_range 重建 diff）全部发生在本方法里；{@link #applyRebasePlan}
     * 只做属性写入。仅行/列增删会改变布局，其余写操作只刷新链式 digest。</p>
     */
    private List<RebasePlanEntry> planRebase(String workspaceId, OperationRow applied) {
        String tool = applied.toolName();
        if (!tool.equals("delete_rows") && !tool.equals("insert_rows")
                && !tool.equals("delete_columns") && !tool.equals("insert_columns")) {
            return List.of();
        }
        JsonNode appliedArgs = applied.arguments() != null ? applied.arguments()
                : mapper.createObjectNode();
        boolean columnAxis = tool.equals("delete_columns") || tool.equals("insert_columns");
        Integer start = OperationRebase.parseIntArg(appliedArgs.get(columnAxis ? "start_col" : "start_row"));
        if (start == null) {
            return List.of();
        }
        Integer parsedCount = OperationRebase.parseIntArg(appliedArgs.get("count"));
        int count = parsedCount == null || parsedCount == 0 ? 1 : parsedCount;
        boolean delete = tool.startsWith("delete");
        String sheetName = OperationRebase.sheetText(appliedArgs.get("sheet_name"));

        List<RebasePlanEntry> plan = new ArrayList<>();
        for (OperationRow other : operations.listPending(workspaceId)) {
            if (other.id().equals(applied.id()) || !other.relPath().equals(applied.relPath())) {
                continue;
            }
            ObjectNode args = other.arguments() != null && other.arguments().isObject()
                    ? (ObjectNode) other.arguments() : mapper.createObjectNode();
            if (other.toolName().equals("copy_range")) {
                // copy_range 的源和目的地分属两张表：任一侧在被平移的表上就跟随移动
                if (!Objects.equals(sheetName, OperationRebase.sheetText(args.get("src_sheet")))
                        && !Objects.equals(sheetName, OperationRebase.sheetText(args.get("dst_sheet")))) {
                    continue;
                }
            } else if (!Objects.equals(sheetName, OperationRebase.sheetText(args.get("sheet_name")))) {
                continue;
            }
            OperationRebase.Rebased rebased = OperationRebase.rebaseArguments(
                    other.toolName(), args, delete, start, count, columnAxis, sheetName);
            if (rebased == null) {
                plan.add(new RebasePlanEntry(other, null, null));
                continue;
            }
            if (!rebased.moved()) {
                continue;
            }
            JsonNode diff = other.toolName().equals("update_cells")
                    ? refreshDiffBefore(workspaceId, other, rebased.arguments()) : null;
            plan.add(new RebasePlanEntry(other, rebased.arguments(), diff));
        }
        return plan;
    }

    /**
     * 用当前文件重建重排后提案的 diff：提议时的 diff 描述的是已经搬移的行，
     * 审批卡片应展示目标单元格里现在的值，坐标用重排后的。
     */
    private JsonNode refreshDiffBefore(String workspaceId, OperationRow operation, ObjectNode newArgs) {
        JsonNode updates = newArgs.get("updates");
        if (updates == null || !updates.isArray() || updates.isEmpty()) {
            return operation.diff();
        }
        String sheet = OperationRebase.sheetText(newArgs.get("sheet_name"));
        String path = workspaceRelativePath(workspaceId, operation.relPath());
        ArrayNode refreshed = mapper.createArrayNode();
        for (JsonNode update : updates) {
            String cell = update.isObject() && update.hasNonNull("cell")
                    ? update.get("cell").asString().strip() : "";
            JsonNode after = update.isObject() ? update.get("value") : null;
            JsonNode before = null;
            if (!cell.isEmpty()) {
                try {
                    ObjectNode readArgs = mapper.createObjectNode();
                    readArgs.put("path", path);
                    if (sheet != null) {
                        readArgs.put("sheet_name", sheet);
                    }
                    readArgs.put("start_cell", cell);
                    JsonNode payload = aiService.callTool("read_range", readArgs);
                    JsonNode values = payload.path("data").path("values");
                    if (values.isArray() && !values.isEmpty()
                            && values.get(0).isArray() && !values.get(0).isEmpty()) {
                        JsonNode value = values.get(0).get(0);
                        before = value.isObject() ? value.get("cached_value") : value;
                    }
                } catch (Exception ex) {
                    log.warn("diff re-read failed for {}!{}: {}", path, cell, ex.getMessage());
                }
            }
            ObjectNode entry = refreshed.addObject();
            entry.put("cell", cell);
            entry.putNull("before");
            if (before != null) {
                entry.set("before", before);
            }
            if (after != null) {
                entry.set("after", after);
            } else {
                entry.putNull("after");
            }
        }
        return refreshed;
    }

    /** 落地重排计划：只写属性，无网络、无查询。 */
    private void applyRebasePlan(List<RebasePlanEntry> plan) {
        for (RebasePlanEntry entry : plan) {
            if (entry.newArguments() == null) {
                operations.markRejected(entry.operation().id(), REBASE_REJECT_ERROR);
                continue;
            }
            String summary = OperationSummaries.summarize(
                    entry.operation().toolName(), entry.operation().relPath(), entry.newArguments());
            operations.updateArguments(entry.operation().id(), entry.newArguments(), summary,
                    entry.diff() != null ? entry.diff() : entry.operation().diff());
        }
    }

    // ------------------------------------------------------------------ //
    // 链式 digest 与索引刷新
    // ------------------------------------------------------------------ //

    /**
     * 同一轮里提交的提案共享提议时的 digest；应用其中一个会让同文件后续提案的乐观锁失效。
     * 把它们的 expected_digest 指向刚写入文件的新 digest（外部编辑仍会冲突，这正是锁的目的）。
     */
    private void refreshChainedDigests(String workspaceId, String appliedId, String relPath,
                                       JsonNode newDigest) {
        if (newDigest == null || !newDigest.isString() || newDigest.asString().isEmpty()) {
            return;
        }
        for (OperationRow other : operations.listPending(workspaceId)) {
            if (other.id().equals(appliedId) || !other.relPath().equals(relPath)) {
                continue;
            }
            JsonNode args = other.arguments();
            if (args != null && args.isObject() && args.hasNonNull("expected_digest")) {
                ObjectNode updated = (ObjectNode) args.deepCopy();
                updated.put("expected_digest", newDigest.asString());
                operations.updateArguments(other.id(), updated, other.summary(), other.diff());
            }
        }
    }

    /**
     * 文件刚被审批写入/还原，分块已过期：后台重建。仅在网络读（read_range）全部
     * 完成后调度；失败降级为告警，下一次 reindex 端点可手动重试。
     */
    private void reindexAfterWrite(String workspaceId, String relPath) {
        reindexExecutor.execute(() -> {
            try {
                files.findByRelPath(workspaceId, relPath)
                        .ifPresent(file -> indexing.index(workspaceId, file.id()));
            } catch (Exception ex) {
                log.warn("reindex after write failed for {}: {}", relPath, ex.getMessage());
            }
        });
    }

    @PreDestroy
    void stopReindexExecutor() {
        reindexExecutor.shutdownNow();
    }

    // ------------------------------------------------------------------ //
    // 工具参数改写
    // ------------------------------------------------------------------ //

    /** 把路径类参数改写为相对沙箱根（&lt;workspace_id&gt;/&lt;file&gt;），MCP 挂载在 workspaces 目录。 */
    private JsonNode buildToolArguments(String workspaceId, JsonNode arguments) {
        if (arguments == null || !arguments.isObject()) {
            return arguments;
        }
        ObjectNode rewritten = (ObjectNode) arguments.deepCopy();
        for (String key : PATH_ARGUMENTS) {
            JsonNode value = rewritten.get(key);
            if (value != null && value.isString()) {
                rewritten.put(key, workspaceRelativePath(workspaceId, value.asString()));
            }
        }
        return rewritten;
    }

    static String workspaceRelativePath(String workspaceId, String relPath) {
        String normalised = relPath.replace('\\', '/');
        while (normalised.startsWith("/")) {
            normalised = normalised.substring(1);
        }
        String prefix = workspaceId + "/";
        while (normalised.startsWith(prefix)) {
            normalised = normalised.substring(prefix.length());
        }
        return prefix + normalised;
    }
}
