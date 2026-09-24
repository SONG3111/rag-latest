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

    public HttpStatus status() {
        return status;
    }
}
