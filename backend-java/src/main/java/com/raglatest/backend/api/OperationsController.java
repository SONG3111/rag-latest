package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.Dtos.OperationRead;
import com.raglatest.backend.security.CurrentUser;
import com.raglatest.backend.service.OperationService;
import com.raglatest.backend.service.WorkspaceAccessGuard;
import com.raglatest.backend.store.OperationRepository;
import com.raglatest.backend.store.OperationRepository.OperationRow;
import java.util.List;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.JsonNodeFactory;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 审批闭环的对外端点（对齐原 Python routes.py）：列表（可按 status 过滤，查询参数
 * 同时认 status 与 Python 版的 status_filter）、apply（备份、写入并重排同文件后续
 * 提案）、reject（不动文件）、revert（还原备份）。响应形状对齐
 * python schemas.OperationRead（不含 arguments，created_at/resolved_at 在列）。
 */
@RestController
@RequestMapping("/api")
public class OperationsController {

    private final WorkspaceAccessGuard access;
    private final OperationRepository operations;
    private final OperationService service;

    public OperationsController(WorkspaceAccessGuard access, OperationRepository operations,
                                OperationService service) {
        this.access = access;
        this.operations = operations;
        this.service = service;
    }

    /** status=proposed（或 Python 版的 status_filter=proposed）时只列待确认；其余返回全部历史。 */
    @GetMapping("/workspaces/{workspaceId}/operations")
    public List<OperationRead> listOperations(@PathVariable String workspaceId,
                                              @RequestParam(defaultValue = "") String status,
                                              @RequestParam(name = "status_filter", defaultValue = "")
                                              String statusFilter) {
        access.requireOwned(workspaceId, CurrentUser.id());
        String filter = !statusFilter.isBlank() ? statusFilter : status;
        List<OperationRow> rows = "proposed".equals(filter)
                ? operations.listPending(workspaceId)
                : operations.listByWorkspace(workspaceId);
        return rows.stream().map(OperationsController::toRead).toList();
    }

    @PostMapping("/workspaces/{workspaceId}/operations/{operationId}/apply")
    public OperationRead apply(@PathVariable String workspaceId, @PathVariable String operationId) {
        access.requireOwned(workspaceId, CurrentUser.id());
        return toRead(service.apply(workspaceId, operationId, CurrentUser.id()));
    }

    @PostMapping("/workspaces/{workspaceId}/operations/{operationId}/reject")
    public OperationRead reject(@PathVariable String workspaceId, @PathVariable String operationId) {
        access.requireOwned(workspaceId, CurrentUser.id());
        return toRead(service.reject(workspaceId, operationId, CurrentUser.id()));
    }

    @PostMapping("/workspaces/{workspaceId}/operations/{operationId}/revert")
    public OperationRead revert(@PathVariable String workspaceId, @PathVariable String operationId) {
        access.requireOwned(workspaceId, CurrentUser.id());
        return toRead(service.revert(workspaceId, operationId, CurrentUser.id()));
    }

    private static OperationRead toRead(OperationRow row) {
        JsonNode diff = row.diff() != null && row.diff().isArray() ? row.diff()
                : JsonNodeFactory.instance.arrayNode();
        return new OperationRead(row.id(), row.toolName(), row.relPath(), row.summary(),
                row.status(), diff, row.result(), row.error(), row.backupPath(),
                row.createdAt(), row.resolvedAt());
    }
}
