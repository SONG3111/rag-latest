package com.raglatest.backend.internal;

import com.raglatest.backend.api.ApiException;
import org.springframework.http.HttpStatus;

/**
 * 可重试的下游瞬时故障：连接重置 / 连接超时 / 上游 502、503、504。
 *
 * 与 {@link ApiException} 的区别在语义而非状态码——只有本类型会被
 * {@code @Retryable(includes = ...)} 纳入重试；4xx 与解析类错误一律不重试。
 * 继承 {@link ApiException} 以复用 503 语义与 {@code {"detail": "..."}} 响应形状。
 */
public class TransientAiServiceException extends ApiException {

    public TransientAiServiceException(String detail) {
        super(HttpStatus.SERVICE_UNAVAILABLE, detail);
    }
}
