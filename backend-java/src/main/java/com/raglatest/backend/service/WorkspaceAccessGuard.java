package com.raglatest.backend.service;

import com.raglatest.backend.api.ApiException;
import com.raglatest.backend.api.dto.Dtos.WorkspaceRead;
import com.raglatest.backend.store.WorkspaceRepository;
import org.springframework.stereotype.Service;

/**
 * 工作区归属校验的唯一入口。
 *
 * <p><b>为什么不用 MyBatis-Plus 的多租户插件</b>（{@code TenantLineInnerInterceptor}，
 * 位于 mybatis-plus-jsqlparser 模块）：它按表/联表自动追加租户条件，表达的是
 * "行属于哪个租户"，而这里要表达的是"这个工作区归当前用户所有"；更关键的是它只能
 * 产出 SQL 级过滤，拿不到我们想要的对外语义——别人的工作区与不存在的工作区一律 404，
 * 不泄露存在性。显式守卫还能被一条表驱动的鉴权测试覆盖。</p>
 *
 * <p>守卫只在 Web 层：Repository 不参与鉴权，因此后台任务（StuckIndexingRecovery、
 * IndexingConsumer）不需要伪造用户上下文。</p>
 */
@Service
public class WorkspaceAccessGuard {

    private static final String NOT_FOUND = "workspace not found";

    private final WorkspaceRepository workspaces;

    public WorkspaceAccessGuard(WorkspaceRepository workspaces) {
        this.workspaces = workspaces;
    }

    /**
     * 校验并返回工作区。不属于 {@code userId} 时抛 404 而不是 403：
     * 403 等于确认"这个 id 存在但不是你的"，对外就是一个枚举接口。
     */
    public WorkspaceRead requireOwned(String workspaceId, String userId) {
        return workspaces.findOwned(workspaceId, userId)
                .orElseThrow(() -> ApiException.notFound(NOT_FOUND));
    }
}
