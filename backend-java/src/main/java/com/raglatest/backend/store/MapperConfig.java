package com.raglatest.backend.store;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.context.annotation.Configuration;

/**
 * MyBatis mapper 的扫描入口。
 *
 * <p>刻意不把 {@code @MapperScan} 挂在 {@code BackendApplication} 上：注解写在主类上，
 * 任何以主类为起点的测试切片都会跟着注册 mapper，而 {@code @WebMvcTest} 这类切片
 * 没有 SqlSessionFactory，于是启动直接报 "Property 'sqlSessionFactory' or
 * 'sqlSessionTemplate' are required"。放在普通的 {@code @Configuration} 里之后，
 * 切片（会排除 @Configuration）不再被牵连，全量上下文照样扫描。</p>
 */
@Configuration
@MapperScan("com.raglatest.backend.store.mapper")
public class MapperConfig {
}
