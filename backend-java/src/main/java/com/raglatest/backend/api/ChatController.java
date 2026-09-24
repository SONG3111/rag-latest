package com.raglatest.backend.api;

import com.raglatest.backend.api.dto.Dtos.ChatRequest;
import com.raglatest.backend.api.dto.Dtos.FileRead;
import com.raglatest.backend.api.dto.Dtos.MessageRead;
import com.raglatest.backend.api.dto.Dtos.SummaryRow;
import com.raglatest.backend.config.RagProperties;
import com.raglatest.backend.internal.AiServiceClient;
import com.raglatest.backend.service.Answers;
import com.raglatest.backend.service.StaleFileDetector;
import com.raglatest.backend.service.StaleFileDetector.StaleFile;
import com.raglatest.backend.store.ConversationSummaryRepository;
import com.raglatest.backend.store.DocumentFileRepository;
import com.raglatest.backend.store.MessageRepository;
import com.raglatest.backend.store.TraceRepository;
import com.raglatest.backend.store.TraceRepository.TraceNodeInput;
import com.raglatest.backend.store.WorkspaceRepository;
import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import jakarta.validation.Valid;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.codec.ServerSentEvent;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;
import reactor.core.Disposable;
import reactor.core.publisher.Flux;
import reactor.core.scheduler.Schedulers;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * POST /api/workspaces/{id}/chat/stream 的 SSE 中继（M2 无状态聊天）。
 *
 * 编排移植自 python api/routes.py 的 chat_stream，落库职责移到本侧：
 * Java 预组装全量消息请求（裁剪留在 ai-service——压缩需要全量，见交接设计
 * §3.6 偏差 1）；ai-service 流回两类帧——可转发帧原样给前端，最后一帧恒为
 * persist，由本侧落库（assistant 行 + trace 节点 + 滚动摘要书签）后合成权威
 * done（带 message_id/run_id）。proposal 帧在转发前被拦截：生成 operation id、
 * 建 proposed 行、替换帧里的 null id 再转发（审批全生命周期在 M3）。
 *
 * 中断路径（浏览器断开 / 上游断流 / 超时）都用转发过的 token 快照 +
 * Answers.compose 持久化部分答案，注记与旧版逐字一致。
 */
@RestController
public class ChatController {

    private static final Logger log = LoggerFactory.getLogger(ChatController.class);

    /** 发给 ai-service 的历史消息封顶（交接设计 §3.4：Java 封顶 500 条）。 */
    private static final int MESSAGE_CAP = 500;
    /** 给 Python 的 persist 帧留出的余量：正常路径 Python 先超时并回 persist(timeout)。 */
    private static final Duration UPSTREAM_MARGIN = Duration.ofSeconds(30);

    private final WorkspaceRepository workspaces;
    private final MessageRepository messages;
    private final ConversationSummaryRepository summaries;
    private final DocumentFileRepository documentFiles;
    private final TraceRepository traces;
    private final com.raglatest.backend.store.OperationRepository operations;
    private final StaleFileDetector staleFiles;
    private final AiServiceClient aiService;
    private final Bulkhead chatTurnBulkhead;
    private final ObjectMapper mapper;
    private final Duration chatTurnTimeout;

    public ChatController(WorkspaceRepository workspaces,
                           MessageRepository messages,
                           ConversationSummaryRepository summaries,
                           DocumentFileRepository documentFiles,
                           TraceRepository traces,
                           com.raglatest.backend.store.OperationRepository operations,
                           StaleFileDetector staleFiles,
                           AiServiceClient aiService,
                           Bulkhead chatTurnBulkhead,
                           ObjectMapper mapper,
                           RagProperties properties) {
        this.workspaces = workspaces;
        this.messages = messages;
        this.summaries = summaries;
        this.documentFiles = documentFiles;
        this.traces = traces;
        this.operations = operations;
        this.staleFiles = staleFiles;
        this.aiService = aiService;
        this.chatTurnBulkhead = chatTurnBulkhead;
        this.mapper = mapper;
        this.chatTurnTimeout = properties.aiService().chatTurnTimeout();
    }

    @PostMapping(value = "/api/workspaces/{workspaceId}/chat/stream",
            produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter chatStream(@PathVariable String workspaceId,
                                 @Valid @RequestBody ChatRequest payload) {
        if (!workspaces.exists(workspaceId)) {
            throw ApiException.notFound("workspace not found");
        }
        // 韧性前置：熔断开闸或舱壁已满时快速失败，不建 SSE、不写库（避免等满超时）。
        if (aiService.chatCircuitState() == CircuitBreaker.State.OPEN) {
            throw ApiException.serviceUnavailable("AI 服务暂时不可用，请稍后重试");
        }

        String runId = WorkspaceRepository.newId();
        ChatTurnState state = new ChatTurnState(workspaceId, runId, chatTurnBulkhead);
        if (!state.tryAcquirePermit()) {
            throw ApiException.serviceUnavailable("当前对话请求过多，请稍后重试");
        }

        try {
            // cutoff 必须在插入本轮 user 行之前取（语义同 python：以上一轮为界）。
            Instant cutoff = messages.latestCreatedAt(workspaceId);
            List<MessageRead> history = messages.recent(workspaceId, MESSAGE_CAP);
            long total = messages.countAll(workspaceId);
            SummaryRow summary = summaries.find(workspaceId).orElse(null);
            messages.insert(workspaceId, "user", payload.message(), null, null);
            List<StaleFile> stale = staleFiles.changedSince(workspaceId, cutoff);
            ObjectNode request = buildRequest(
                    workspaceId, runId, payload.message(), history, total, summary, stale);

            SseEmitter emitter =
                    new SseEmitter(chatTurnTimeout.plus(UPSTREAM_MARGIN).toMillis());

            Disposable subscription = aiService.streamChat(request)
                    .timeout(chatTurnTimeout.plus(UPSTREAM_MARGIN))
                    .publishOn(Schedulers.boundedElastic())
                    .subscribe(
                            frame -> handleFrame(frame, emitter, state),
                            error -> interrupted(emitter, state, "upstream failed: " + error),
                            // 协议要求 persist 收尾；没等到也按中断持久化部分答案。
                            () -> interrupted(emitter, state, "upstream ended without persist"));
            state.bind(subscription);

            // 浏览器断开 / SseEmitter 到期：停上游、释放舱壁许可，部分答案仍然落库（旧版语义）。
            emitter.onError(throwable -> {
                subscription.dispose();
                state.releasePermitOnce();
                interruptedQuietly(state, "client disconnected");
            });
            emitter.onTimeout(() -> {
                subscription.dispose();
                state.releasePermitOnce();
                interruptedQuietly(state, "emitter timed out");
            });
            emitter.onCompletion(() -> {
                subscription.dispose();
                state.releasePermitOnce();
            });
            return emitter;
        } catch (RuntimeException ex) {
            // 编排开始前失败：确保舱壁许可不泄漏。
            state.releasePermitOnce();
            throw ex;
        }
    }

    // ------------------------------------------------------------------ //
    // 请求组装
    // ------------------------------------------------------------------ //
    private ObjectNode buildRequest(String workspaceId, String runId, String message,
                                    List<MessageRead> history, long total,
                                    SummaryRow summary, List<StaleFile> stale) {
        ObjectNode request = mapper.createObjectNode();
        request.put("workspace_id", workspaceId);
        request.put("run_id", runId);
        request.put("message", message);
        request.put("timeout_seconds", chatTurnTimeout.toSeconds());

        ArrayNode messagesNode = mapper.createArrayNode();
        for (MessageRead row : history) {
            ObjectNode item = mapper.createObjectNode();
            item.put("role", row.role());
            item.put("content", row.content());
            if (row.toolCalls() != null && !row.toolCalls().isNull()) {
                item.set("tool_calls", row.toolCalls());
            }
            messagesNode.add(item);
        }
        request.set("messages", messagesNode);
        request.put("total_messages", total);

        if (summary != null) {
            request.put("summary", summary.summary());
            request.put("covered_count", summary.coveredCount());
        } else {
            request.putNull("summary");
            request.put("covered_count", 0);
        }

        ArrayNode staleNode = mapper.createArrayNode();
        for (StaleFile file : stale) {
            ObjectNode item = mapper.createObjectNode();
            item.put("rel_path", file.relPath());
            item.put("changed_at", file.changedAt());
            staleNode.add(item);
        }
        request.set("stale_files", staleNode);

        ArrayNode filesNode = mapper.createArrayNode();
        for (FileRead file : documentFiles.listByWorkspace(workspaceId)) {
            filesNode.add(file.relPath());
        }
        request.set("files", filesNode);
        return request;
    }

    // ------------------------------------------------------------------ //
    // 帧处理
    // ------------------------------------------------------------------ //
    private void handleFrame(ServerSentEvent<String> frame, SseEmitter emitter,
                             ChatTurnState state) {
        String data = frame.data();
        if (data == null || data.isBlank()) {
            return; // keep-alive 注释行
        }
        String event = frame.event() == null ? "message" : frame.event();
        switch (event) {
            case "persist" -> handlePersist(data, emitter, state);
            case "proposal" -> handleProposal(data, emitter, state);
            default -> {
                state.recordForwardable(event, data, mapper);
                forward(emitter, event, data);
            }
        }
    }

    /** 拦截提案帧：建 proposed 行、回填真实 operation id、转发。 */
    private void handleProposal(String data, SseEmitter emitter, ChatTurnState state) {
        ObjectNode payload = (ObjectNode) mapper.readTree(data);
        JsonNode arguments = payload.get("arguments");
        JsonNode diff = payload.get("diff");
        String operationId = operations.insertProposed(
                state.workspaceId,
                payload.path("tool").asText(""),
                payload.path("path").asText(""),
                arguments != null ? arguments : mapper.createObjectNode(),
                diff != null ? diff : mapper.createArrayNode(),
                payload.path("summary").asText(""));
        payload.put("operation_id", operationId);
        state.addProposal(operationId);

        // 转发帧保持 ai-service 的原样（含 arguments）；前端契约不变。
        forward(emitter, "proposal", payload.toString());

        // 消息行的 tool_calls 沿用旧版形状：无 arguments（全参数在 operations 行上）。
        ObjectNode toolCall = payload.deepCopy();
        toolCall.remove("arguments");
        toolCall.remove("type");
        state.addToolCall(toolCall);
    }

    /** persist：权威落库（assistant 行 + trace + 摘要书签）后合成对外 done。 */
    private void handlePersist(String data, SseEmitter emitter, ChatTurnState state) {
        JsonNode persist = mapper.readTree(data);
        String content = persist.path("content").asText("");
        JsonNode citations = persist.get("citations");
        ArrayNode toolCalls = state.buildToolCalls(mapper);

        // 判定 + 落库 + 置位收进 persistOnce 的同一临界区：容器线程的中断路径
        // 与弹性线程的 persist 路径可能并发，不能各写一行（会重复 assistant 行）。
        String assistantId = state.persistOnce(() -> {
            String id = messages.insert(
                    state.workspaceId, "assistant", content, toolCalls, citations);
            for (JsonNode node : persist.path("trace_nodes")) {
                JsonNode error = node.get("error");
                traces.insertNode(state.workspaceId, state.runId, new TraceNodeInput(
                        node.path("node").asText(),
                        node.path("duration_ms").asLong(0),
                        node.get("input"),
                        node.get("output"),
                        error == null || error.isNull() ? null : error.asText()));
            }
            JsonNode summary = persist.get("summary");
            if (summary != null && !summary.isNull()) {
                summaries.upsert(
                        state.workspaceId,
                        summary.path("text").asText(),
                        summary.path("covered_count").asInt(0));
            }
            return id;
        });
        if (assistantId == null) {
            return; // 已被中断路径落库：不重复写、也不再发 done。
        }

        ObjectNode done = mapper.createObjectNode();
        done.put("type", "done");
        done.put("content", content);
        done.put("run_id", state.runId);
        done.put("message_id", assistantId);
        try {
            forward(emitter, "done", done.toString());
        } finally {
            state.releasePermitOnce();
            emitter.complete();
        }
    }

    // ------------------------------------------------------------------ //
    // 中断路径（超时 / 断开 / 上游故障）
    // ------------------------------------------------------------------ //
    private void interrupted(SseEmitter emitter, ChatTurnState state, String reason) {
        if (state.persisted()) {
            return;
        }
        log.warn("chat turn {} interrupted: {}", state.runId, reason);
        String content = Answers.compose(state.streamedText(), state.proposalCount())
                + Answers.STOPPED_NOTE;
        persistPartial(state, content);
        ObjectNode done = mapper.createObjectNode();
        done.put("type", "done");
        done.put("content", content);
        done.put("run_id", state.runId);
        try {
            forward(emitter, "done", done.toString());
        } catch (Exception ignored) {
            // 客户端已不在：落库已完成，发送失败无所谓。
        }
        state.releasePermitOnce();
        emitter.complete();
    }

    private void interruptedQuietly(ChatTurnState state, String reason) {
        if (state.persisted()) {
            return;
        }
        log.info("chat turn {} interrupted: {}", state.runId, reason);
        String content = Answers.compose(state.streamedText(), state.proposalCount())
                + Answers.STOPPED_NOTE;
        persistPartial(state, content);
        state.releasePermitOnce();
    }

    private void persistPartial(ChatTurnState state, String content) {
        ArrayNode toolCalls = state.buildToolCalls(mapper);
        state.persistOnce(() -> messages.insert(
                state.workspaceId, "assistant", content, toolCalls, state.citations()));
    }

    private void forward(SseEmitter emitter, String event, String data) {
        try {
            emitter.send(SseEmitter.event().name(event).data(data));
        } catch (Exception exc) {
            // 客户端断开：向上抛给 Flux 的错误通道，走中断持久化路径。
            if (exc instanceof RuntimeException runtime) {
                throw runtime;
            }
            throw new IllegalStateException("SSE forward failed", exc);
        }
    }

    // ------------------------------------------------------------------ //
    // 单轮状态
    // ------------------------------------------------------------------ //
    /** One turn's relay bookkeeping; guarded because SSE callbacks race the relay. */
    static final class ChatTurnState {

        private final String workspaceId;
        private final String runId;
        private final Bulkhead bulkhead;
        private final java.util.concurrent.atomic.AtomicBoolean permitReleased =
                new java.util.concurrent.atomic.AtomicBoolean();
        private final StringBuilder streamed = new StringBuilder();
        private final List<String> proposalIds = new ArrayList<>();
        private final List<ObjectNode> toolCalls = new ArrayList<>();
        private volatile Disposable subscription;
        private JsonNode citations;
        private boolean persisted;

        ChatTurnState(String workspaceId, String runId, Bulkhead bulkhead) {
            this.workspaceId = workspaceId;
            this.runId = runId;
            this.bulkhead = bulkhead;
        }

        /** 申请一个舱壁许可；满则 false（调用方据此快速拒绝）。 */
        boolean tryAcquirePermit() {
            return bulkhead.tryAcquirePermission();
        }

        /** 释放舱壁许可；幂等，所有终态路径都应调用。 */
        void releasePermitOnce() {
            if (permitReleased.compareAndSet(false, true)) {
                bulkhead.releasePermission();
            }
        }

        synchronized void bind(Disposable subscription) {
            this.subscription = subscription;
        }

        synchronized void recordForwardable(String event, String data, ObjectMapper mapper) {
            switch (event) {
                case "token" -> streamed.append(tokenText(data, mapper));
                case "citations" -> citations = mapper.readTree(data).get("items");
                default -> { /* notice/tool_call/tool_result/followups 无需快照 */ }
            }
        }

        private static String tokenText(String data, ObjectMapper mapper) {
            return mapper.readTree(data).path("text").asText("");
        }

        synchronized void addProposal(String operationId) {
            proposalIds.add(operationId);
        }

        synchronized void addToolCall(ObjectNode toolCall) {
            toolCalls.add(toolCall);
        }

        synchronized ArrayNode buildToolCalls(ObjectMapper mapper) {
            ArrayNode array = mapper.createArrayNode();
            toolCalls.forEach(array::add);
            return array;
        }

        synchronized JsonNode citations() {
            return citations;
        }

        synchronized String streamedText() {
            return streamed.toString();
        }

        synchronized int proposalCount() {
            return proposalIds.size();
        }

        synchronized boolean persisted() {
            return persisted;
        }

        /**
         * 一轮对话至多落库一次：把「判定 + 写库 + 置位（并停订阅）」收进同一临界区。
         * 传入的写操作抛异常时不置位，交由中断路径兜底重试；已落库则返回 null。
         */
        synchronized String persistOnce(java.util.function.Supplier<String> writes) {
            if (persisted) {
                return null;
            }
            String id = writes.get();
            persisted = true;
            if (subscription != null) {
                subscription.dispose();
            }
            return id;
        }
    }
}
