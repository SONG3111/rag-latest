package com.raglatest.backend.service;

/**
 * 助手消息文本的组装规则，移植自 python api/routes.py 的 compose_answer
 * （M2 交接设计 §3.5：取消/超时路径也要用它持久化部分答案）。
 *
 * 空回答兜底与提案提示都必须与旧版逐字一致——前端直接展示这些文本。
 */
public final class Answers {

    private Answers() {}

    public static final String EMPTY_FALLBACK = "模型这次没有返回内容，请再说一次。";

    /** 与 python app/agent/prompts.py 的 PROPOSAL_NOTICE 逐字一致（{count} 占位）。 */
    public static final String PROPOSAL_NOTICE =
            "⚠️ 已生成 {count} 条待确认的修改提案，请在右侧确认后才会写入文件。";

    /** 客户端断开/上游中断时附加在部分答案后的注记（旧版逐字一致）。 */
    public static final String STOPPED_NOTE = "\n\n（已停止生成，以上为已生成的部分。）";

    /**
     * 组装一条助手消息的持久化文本：已流出的 token 快照 + 提案提示 + 空回答兜底。
     *
     * @param collected      已转发给前端的 token 快照拼接结果（可为空串）
     * @param proposalCount  本轮提案数
     */
    public static String compose(String collected, int proposalCount) {
        String answer = collected == null ? "" : collected.strip();
        StringBuilder text = new StringBuilder(answer);
        if (proposalCount > 0) {
            if (!text.isEmpty()) {
                text.append("\n\n");
            }
            text.append(PROPOSAL_NOTICE.replace("{count}", String.valueOf(proposalCount)));
        }
        return text.isEmpty() ? EMPTY_FALLBACK : text.toString();
    }
}
