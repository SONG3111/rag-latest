package com.raglatest.backend.security;

import com.raglatest.backend.config.RagProperties;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.springframework.stereotype.Component;

/**
 * Java ↔ ai-service 的内部共享密钥。
 *
 * <p>多用户改造前，{@code /internal/workspaces/{id}/retrieval-corpus} 与
 * ai-service 的整个 {@code /v1} 契约都没有任何鉴权——而 8000 端口是对外发布的，
 * 于是任何人 curl 一下就能读到任意工作区的语料。账号体系建立后这些端点仍不能
 * 挂在会话上（服务间调用没有会话），因此用共享密钥把这条边界关上。</p>
 *
 * <p><b>未配置即启动失败</b>：空密钥不是"关闭校验"，把这层做成可选开关等于
 * 把漏洞留成默认值。</p>
 */
@Component
public class InternalToken {

    /** 与 ai-service 约定的头名（Python 侧同名常量）。 */
    public static final String HEADER = "X-Internal-Token";

    private final byte[] expected;
    private final String value;

    public InternalToken(RagProperties properties) {
        String configured = properties.internalToken();
        if (configured == null || configured.isBlank()) {
            throw new IllegalStateException(
                    "INTERNAL_TOKEN 未配置：Java 后端与 ai-service 之间的内部调用需要共享密钥，"
                            + "请在两边的 .env（或 compose 的 environment）里设置同一个值，"
                            + "参见 .env.example。");
        }
        this.value = configured;
        this.expected = configured.getBytes(StandardCharsets.UTF_8);
    }

    /** 出站调用（AiServiceClient）回填请求头用。 */
    public String value() {
        return value;
    }

    /** 常量时间比较：避免按字节早退把密钥长度/前缀泄露成可测量差异。 */
    public boolean matches(String candidate) {
        if (candidate == null) {
            return false;
        }
        return MessageDigest.isEqual(expected, candidate.getBytes(StandardCharsets.UTF_8));
    }
}
