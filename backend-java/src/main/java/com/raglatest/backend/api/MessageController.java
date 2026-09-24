package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.Dtos.MessageFeedback;
import com.raglatest.backend.api.dto.Dtos.MessageRead;
import com.raglatest.backend.api.dto.Dtos.TraceRunDetail;
import com.raglatest.backend.api.dto.Dtos.TraceRunRead;
import com.raglatest.backend.store.MessageRepository;
import com.raglatest.backend.store.TraceRepository;
import com.raglatest.backend.store.WorkspaceRepository;
import jakarta.validation.Valid;
import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/** 消息历史、点踩/点赞反馈、run 级 trace 读侧。 */
@RestController
@RequestMapping("/api")
public class MessageController {

    private final WorkspaceRepository workspaces;
    private final MessageRepository messages;
    private final TraceRepository traces;

    public MessageController(WorkspaceRepository workspaces, MessageRepository messages,
                             TraceRepository traces) {
        this.workspaces = workspaces;
        this.messages = messages;
        this.traces = traces;
    }

    @GetMapping("/workspaces/{workspaceId}/messages")
    public List<MessageRead> listMessages(
            @PathVariable String workspaceId,
            @RequestParam(defaultValue = "200") int limit) {
        requireWorkspace(workspaceId);
        return messages.recent(workspaceId, limit);
    }

    @PostMapping("/workspaces/{workspaceId}/messages/{messageId}/feedback")
    public MessageRead setMessageFeedback(
            @PathVariable String workspaceId,
            @PathVariable String messageId,
            @Valid @RequestBody MessageFeedback payload) {
        requireWorkspace(workspaceId);
        messages.findInWorkspace(workspaceId, messageId)
                .orElseThrow(() -> ApiException.notFound("message not found"));
        messages.updateFeedback(messageId,
                "none".equals(payload.feedback()) ? null : payload.feedback());
        return messages.findInWorkspace(workspaceId, messageId).orElseThrow();
    }

    @GetMapping("/workspaces/{workspaceId}/traces")
    public List<TraceRunRead> listTraceRuns(
            @PathVariable String workspaceId,
            @RequestParam(defaultValue = "20") int limit) {
        requireWorkspace(workspaceId);
        return traces.listRuns(workspaceId, limit);
    }

    @GetMapping("/workspaces/{workspaceId}/traces/{runId}")
    public TraceRunDetail getTraceRun(@PathVariable String workspaceId, @PathVariable String runId) {
        requireWorkspace(workspaceId);
        List<com.raglatest.backend.api.dto.Dtos.TraceNodeRead> nodes =
                traces.runDetail(workspaceId, runId)
                        .orElseThrow(() -> ApiException.notFound("trace run not found"));
        return new TraceRunDetail(runId, nodes);
    }

    private void requireWorkspace(String workspaceId) {
        workspaces.find(workspaceId)
                .orElseThrow(() -> ApiException.notFound("workspace not found"));
    }
}
