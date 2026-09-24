package com.raglatest.backend.api;

import org.springframework.http.HttpStatus;

/** 业务异常：携带 HTTP 状态码与 FastAPI 风格 detail 文本。 */
public class ApiException extends RuntimeException {

    private final HttpStatus status;

    public ApiException(HttpStatus status, String detail) {
        super(detail);
        this.status = status;
    }

    public static ApiException notFound(String detail) {
        return new ApiException(HttpStatus.NOT_FOUND, detail);
    }

    public static ApiException badRequest(String detail) {
        return new ApiException(HttpStatus.BAD_REQUEST, detail);
    }

    /** 下游不可用 / 被韧性策略拒绝：统一 503。 */
    public static ApiException serviceUnavailable(String detail) {
        return new ApiException(HttpStatus.SERVICE_UNAVAILABLE, detail);
    }

    public HttpStatus status() {
        return status;
    }
}
