package com.raglatest.backend.api;

import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.store.WorkspaceRepository;
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
    private final WorkspaceRepository workspaces;

    public ToolsController(AiServiceClient aiService, WorkspaceRepository workspaces) {
        this.aiService = aiService;
        this.workspaces = workspaces;
    }

    /** 与 python 版一致：不校验 workspace 是否存在（工具清单与工作区无关）。 */
    @GetMapping("/workspaces/{workspaceId}/tools")
    public JsonNode listTools(@PathVariable String workspaceId) {
        return aiService.listTools();
    }

    @GetMapping("/workspaces/{workspaceId}/preview")
    public JsonNode previewCitation(
            @PathVariable String workspaceId,
            @RequestParam("file") String file,
            @RequestParam("location") String location) {
        workspaces.find(workspaceId)
                .orElseThrow(() -> ApiException.notFound("workspace not found"));
        return aiService.preview(workspaceId, file, location);
    }
}
