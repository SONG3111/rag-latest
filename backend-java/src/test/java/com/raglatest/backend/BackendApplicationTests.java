package com.raglatest.backend;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

/** 上下文能起：配置绑定、WebClient 装配不炸（不触发对外请求）。 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.MOCK)
class BackendApplicationTests {

    @Test
    void contextLoads() {
    }
}
