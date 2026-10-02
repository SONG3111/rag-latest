package com.raglatest.backend;

import com.raglatest.backend.queue.ReindexProducer;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

/** 上下文能起：配置绑定、WebClient 装配不炸（不触发对外请求）。 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.MOCK)
class BackendApplicationTests {

    /** 冒烟上下文不连 broker：启动恢复 runner（StuckIndexingRecovery）也走 mock。 */
    @MockitoBean
    ReindexProducer reindexQueue;

    @Test
    void contextLoads() {
    }
}
