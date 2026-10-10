package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.Dtos.MessageFeedback;
import com.raglatest.backend.api.dto.Dtos.MessageRead;
import com.raglatest.backend.api.dto.Dtos.TraceRunDetail;
import com.raglatest.backend.api.dto.Dtos.TraceRunRead;
import com.raglatest.backend.security.CurrentUser;
import com.raglatest.backend.service.WorkspaceAccessGuard;
import com.raglatest.backend.store.MessageRepository;
import com.raglatest.backend.store.TraceRepository;
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

    private final WorkspaceAccessGuard access;
    private final MessageRepository messages;
    private final TraceRepository traces;

    public MessageController(WorkspaceAccessGuard access, MessageRepository messages,
                             TraceRepository traces) {
        this.access = access;
        this.messages = messages;
        this.traces = traces;
    }

    @GetMapping("/workspaces/{workspaceId}/messages")
    public List<MessageRead> listMessages(
            @PathVariable String workspaceId,
            @RequestParam(defaultValue = "200") int limit) {
        access.requireOwned(workspaceId, CurrentUser.id());
        return messages.recent(workspaceId, limit);
    }

    @PostMapping("/workspaces/{workspaceId}/messages/{messageId}/feedback")
    public MessageRead setMessageFeedback(
            @PathVariable String workspaceId,
            @PathVariable String messageId,
            @Valid @RequestBody MessageFeedback payload) {
        access.requireOwned(workspaceId, CurrentUser.id());
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
        access.requireOwned(workspaceId, CurrentUser.id());
        return traces.listRuns(workspaceId, limit);
    }

    @GetMapping("/workspaces/{workspaceId}/traces/{runId}")
    public TraceRunDetail getTraceRun(@PathVariable String workspaceId, @PathVariable String runId) {
        access.requireOwned(workspaceId, CurrentUser.id());
        List<com.raglatest.backend.api.dto.Dtos.TraceNodeRead> nodes =
                traces.runDetail(workspaceId, runId)
                        .orElseThrow(() -> ApiException.notFound("trace run not found"));
        return new TraceRunDetail(runId, nodes);
    }

}
