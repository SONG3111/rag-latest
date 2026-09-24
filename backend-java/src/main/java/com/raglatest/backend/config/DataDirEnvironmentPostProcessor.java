package com.raglatest.backend.config;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import org.springframework.boot.EnvironmentPostProcessor;
import org.springframework.boot.SpringApplication;
import org.springframework.core.Ordered;
import org.springframework.core.env.ConfigurableEnvironment;

/**
 * 在 DataSource 初始化（连接池 fail-fast）之前确保数据目录存在：
 * SQLite 会创建库文件，但不会创建父目录。
 */
public class DataDirEnvironmentPostProcessor implements EnvironmentPostProcessor, Ordered {

    @Override
    public void postProcessEnvironment(ConfigurableEnvironment environment, SpringApplication application) {
        String dataDir = environment.getProperty("rag.data-dir",
                environment.getProperty("DATA_DIR", "./data"));
        try {
            Files.createDirectories(Path.of(dataDir).resolve("workspaces"));
        } catch (IOException ignored) {
            // 留给 DataSource 以明确的错误失败
        }
    }

    @Override
    public int getOrder() {
        // 必须晚于配置文件加载（ConfigDataEnvironmentPostProcessor），才能读到 rag.data-dir。
        return Ordered.LOWEST_PRECEDENCE;
    }
}
