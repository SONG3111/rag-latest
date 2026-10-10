package com.raglatest.backend.api;

import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.security.CurrentUser;
import com.raglatest.backend.service.WorkspaceAccessGuard;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

/**
 * MCP 工具清单与引用出处预览：两者都透传 ai-service 的 JSON（工具注解与
 * location 解析逻辑都在 Python 侧，Java 不重复实现）。
 */
@RestController
@RequestMapping("/api")
public class ToolsController {

    private final AiServiceClient aiService;
    private final WorkspaceAccessGuard access;

    public ToolsController(AiServiceClient aiService, WorkspaceAccessGuard access) {
        this.aiService = aiService;
        this.access = access;
    }

    /**
     * 工具清单与工作区无关，但路由仍带 workspaceId：为了不让"存在一条不需要归属
     * 校验的 workspace 子路由"成为先例（那正是越权最容易钻的缝），这里照样校验。
     */
    @GetMapping("/workspaces/{workspaceId}/tools")
    public JsonNode listTools(@PathVariable String workspaceId) {
        access.requireOwned(workspaceId, CurrentUser.id());
        return aiService.listTools();
    }

    @GetMapping("/workspaces/{workspaceId}/preview")
    public JsonNode previewCitation(
            @PathVariable String workspaceId,
            @RequestParam("file") String file,
            @RequestParam("location") String location) {
        access.requireOwned(workspaceId, CurrentUser.id());
        return aiService.preview(workspaceId, file, location);
    }
}
