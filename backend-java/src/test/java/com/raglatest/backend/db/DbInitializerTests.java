package com.raglatest.backend.db;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.DriverManagerDataSource;

/**
 * 增量迁移回归：模拟 Python 版层级化之前的旧库（chunks 无 level/parent_id、
 * messages 无 feedback），初始化后必须补列并回填默认值 —— 对应原
 * migrations.py 记录过的“create_all 不 alter 既有表”的坑。
 * 测试库固定放在 target/ 下（surefire 工作目录）；建表走与主代码相同的
 * JdbcTemplate.execute(DDL 字面量)，数据行一律参数化写入。
 */
class DbInitializerTests {

    private static final String LEGACY_URL = "jdbc:sqlite:target/legacy-migration-test.db";
    private static final String FRESH_URL = "jdbc:sqlite:target/fresh-migration-test.db";

    private final JdbcTemplate legacy = new JdbcTemplate(new DriverManagerDataSource(LEGACY_URL));
    private final JdbcTemplate fresh = new JdbcTemplate(new DriverManagerDataSource(FRESH_URL));

    @BeforeEach
    void resetTestDatabases() throws IOException {
        Files.createDirectories(Path.of("target"));
        Files.deleteIfExists(Path.of("target/legacy-migration-test.db"));
        Files.deleteIfExists(Path.of("target/fresh-migration-test.db"));
    }

    @Test
    void addsMissingColumnsAndIndexesToLegacyDatabases() {
        legacy.execute("""
                CREATE TABLE chunks (
                    id VARCHAR(32) NOT NULL PRIMARY KEY,
                    workspace_id VARCHAR(32) NOT NULL,
                    file_id VARCHAR(32) NOT NULL,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    location VARCHAR(300),
                    meta JSON,
                    token_counts JSON,
                    token_length INTEGER NOT NULL,
                    created_at DATETIME
                )
                """);
        legacy.execute("""
                CREATE TABLE messages (
                    id VARCHAR(32) NOT NULL PRIMARY KEY,
                    workspace_id VARCHAR(32) NOT NULL,
                    role VARCHAR(9) NOT NULL,
                    content TEXT,
                    tool_calls JSON,
                    citations JSON,
                    created_at DATETIME
                )
                """);
        legacy.update("""
                INSERT INTO chunks (id, workspace_id, file_id, ordinal, text, token_length)
                VALUES (?, ?, ?, ?, ?, ?)
                """, "c1", "w1", "f1", 0, "旧行", 3);
        legacy.update(
                "INSERT INTO messages (id, workspace_id, role) VALUES (?, ?, ?)",
                "m1", "w1", "user");

        new DbInitializer(new DriverManagerDataSource(LEGACY_URL)).run(null);

        assertThat(columnNames(legacy, "chunks")).contains("level", "parent_id");
        assertThat(columnNames(legacy, "messages")).contains("feedback");

        // 旧 chunk 行回填 child，保持可检索行为不变。
        String level = legacy.queryForObject(
                "SELECT level FROM chunks WHERE id = ?", String.class, "c1");
        assertThat(level).isEqualTo("child");
        String feedback = legacy.queryForObject(
                "SELECT feedback FROM messages WHERE id = ?", String.class, "m1");
        assertThat(feedback).isNull();

        List<String> indexes = legacy.queryForList(
                "SELECT name FROM sqlite_master WHERE type = 'index'", String.class);
        assertThat(indexes).contains("ix_chunks_parent_id", "ix_chunk_level_workspace");
    }

    @Test
    void createsEveryTableOnAFreshDatabase() {
        new DbInitializer(new DriverManagerDataSource(FRESH_URL)).run(null);

        List<String> tables = fresh.queryForList(
                "SELECT name FROM sqlite_master WHERE type = 'table'", String.class);
        assertThat(tables).containsExactlyInAnyOrder(
                "workspaces", "document_files", "chunks", "messages",
                "conversation_summaries", "run_traces", "operations", "mcp_tools");
    }

    private static List<String> columnNames(JdbcTemplate jdbc, String table) {
        return jdbc.queryForList("PRAGMA table_info(" + table + ")").stream()
                .map(column -> String.valueOf(column.get("name")))
                .toList();
    }
}
