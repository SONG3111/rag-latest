package com.raglatest.backend.api;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import io.github.resilience4j.bulkhead.BulkheadFullException;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.multipart.MaxUploadSizeExceededException;
import org.springframework.web.servlet.resource.NoResourceFoundException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 错误响应保持 FastAPI HTTPException 的形状 {"detail": "..."}，
 * 前端按非 2xx + detail 文本提示。
 */
@RestControllerAdvice
public class ApiExceptionHandler {

    private static final Logger log = LoggerFactory.getLogger(ApiExceptionHandler.class);
    private static final JsonNodeFactory JSON = JsonNodeFactory.instance;

    @ExceptionHandler(ApiException.class)
    public ResponseEntity<ObjectNode> apiException(ApiException ex) {
        return ResponseEntity.status(ex.status()).body(detail(ex.getMessage()));
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<ObjectNode> validation(MethodArgumentNotValidException ex) {
        String message = ex.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(error -> error.getField() + ": " + error.getDefaultMessage())
                .orElse("invalid request");
        return ResponseEntity.status(HttpStatus.BAD_REQUEST).body(detail(message));
    }

    @ExceptionHandler(MaxUploadSizeExceededException.class)
    public ResponseEntity<ObjectNode> uploadTooLarge(MaxUploadSizeExceededException ex) {
        return ResponseEntity.status(HttpStatus.BAD_REQUEST)
                .body(detail("file exceeds the upload limit"));
    }

    /** Spring Framework 7 原生 @ConcurrencyLimit(REJECT) 超限拒绝。 */
    @ExceptionHandler(org.springframework.resilience.InvocationRejectedException.class)
    public ResponseEntity<ObjectNode> concurrencyRejected(
            org.springframework.resilience.InvocationRejectedException ex) {
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(detail("当前请求过多，请稍后重试"));
    }

    /** Resilience4j 熔断打开：调用被快速拒绝。 */
    @ExceptionHandler(CallNotPermittedException.class)
    public ResponseEntity<ObjectNode> circuitOpen(CallNotPermittedException ex) {
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(detail("AI 服务暂时不可用，请稍后重试"));
    }

    /** Resilience4j 舱壁已满：并发回合达上限。 */
    @ExceptionHandler(BulkheadFullException.class)
    public ResponseEntity<ObjectNode> bulkheadFull(BulkheadFullException ex) {
        return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                .body(detail("当前对话请求过多，请稍后重试"));
    }

    /**
     * 未匹配到任何路由/静态资源：返回 404，而不是被下方兜底 Exception 处理器
     * 吞成 500（形状与原 Python/FastAPI 的未知路径一致）。
     */
    @ExceptionHandler(NoResourceFoundException.class)
    public ResponseEntity<ObjectNode> noResource(NoResourceFoundException ex) {
        return ResponseEntity.status(HttpStatus.NOT_FOUND).body(detail("not found"));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<ObjectNode> unexpected(Exception ex) {
        log.error("unhandled error", ex);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(detail("internal error: " + ex.getMessage()));
    }

    private ObjectNode detail(String message) {
        ObjectNode body = JSON.objectNode();
        body.put("detail", message);
        return body;
    }

    /** ai-service 错误体的 detail 提取辅助。 */
    public static String extractDetail(JsonNode errorBody, String fallback) {
        if (errorBody != null && errorBody.hasNonNull("detail")) {
            return errorBody.get("detail").asString();
        }
        return fallback;
    }
}
