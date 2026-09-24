package com.raglatest.backend.internal;

import static org.assertj.core.api.Assertions.assertThat;

import java.net.SocketException;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.util.concurrent.TimeoutException;
import javax.net.ssl.SSLHandshakeException;
import org.junit.jupiter.api.Test;

/**
 * {@link AiServiceClient#isTransient} 的分类回归：连接建立/重置类与服务端超时可重试，
 * DNS 解析失败与 TLS 握手失败不重试（重试无意义），非 IO 异常同样不重试。
 */
class AiServiceClientTransientTest {

    @Test
    void connectionAndTimeoutAreTransient() {
        assertThat(AiServiceClient.isTransient(new SocketException("Connection reset"))).isTrue();
        assertThat(AiServiceClient.isTransient(new SocketTimeoutException("read timed out"))).isTrue();
        assertThat(AiServiceClient.isTransient(new TimeoutException("block timeout"))).isTrue();
        // 对齐 WebClient 的异常包装：瞬时故障常被包在外层异常里。
        assertThat(AiServiceClient.isTransient(
                new RuntimeException("wrapped", new SocketException("reset")))).isTrue();
    }

    @Test
    void dnsAndTlsFailuresAreNotTransient() {
        assertThat(AiServiceClient.isTransient(new UnknownHostException("no such host"))).isFalse();
        assertThat(AiServiceClient.isTransient(new SSLHandshakeException("handshake failed"))).isFalse();
        assertThat(AiServiceClient.isTransient(new IllegalStateException("boom"))).isFalse();
    }
}
