package com.raglatest.backend.db;

import java.time.LocalDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeFormatterBuilder;
import java.time.format.DateTimeParseException;
import java.time.temporal.ChronoField;
import java.util.List;
import java.util.Map;
import javax.sql.DataSource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.core.annotation.Order;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * 建表 + 增量迁移，语义移植自 Python 版 app/migrations.py（保持“只加不改”规则：
 * 不删列、不改列、不动数据，因此可以每次启动自动执行）。
 *
 * 兼容旧库：原 Python 后端创建过的 data/app.db 直接可用 —— CREATE TABLE
 * IF NOT EXISTS 只补缺失的表，PRAGMA 探测到缺列/缺索引时追加（对应原版
 * create_all 只建缺表、不 alter 既有表的坑，见原模块 docstring）。
 */
@Component
@Order(1)
public class DbInitializer implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(DbInitializer.class);

    /** 表结构对齐 Python SQLAlchemy models.py；JSON 列在 SQLite 里按 TEXT 存。 */
    private static final List<String> TABLES = List.of(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                name VARCHAR(200) NOT NULL,
                description TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS document_files (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                rel_path VARCHAR(500) NOT NULL,
                kind VARCHAR(20) NOT NULL,
                size_bytes INTEGER DEFAULT 0 NOT NULL,
                checksum VARCHAR(64) DEFAULT '' NOT NULL,
                status VARCHAR(8) DEFAULT 'pending' NOT NULL,
                chunk_count INTEGER DEFAULT 0 NOT NULL,
                error TEXT,
                indexed_at DATETIME,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_file_per_ws UNIQUE (workspace_id, rel_path)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                workspace_id VARCHAR(32) NOT NULL,
                file_id VARCHAR(32) NOT NULL REFERENCES document_files(id) ON DELETE CASCADE,
                parent_id VARCHAR(32),
                level VARCHAR(10) DEFAULT 'child' NOT NULL,
                ordinal INTEGER DEFAULT 0 NOT NULL,
                text TEXT NOT NULL,
                location VARCHAR(300) DEFAULT '' NOT NULL,
                meta TEXT NOT NULL,
                token_counts TEXT NOT NULL,
                token_length INTEGER DEFAULT 0 NOT NULL,
                created_at DATETIME NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                role VARCHAR(9) NOT NULL,
                content TEXT NOT NULL,
                tool_calls TEXT,
                citations TEXT,
                feedback VARCHAR(10),
                created_at DATETIME NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS conversation_summaries (
                workspace_id VARCHAR(32) NOT NULL PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                covered_count INTEGER DEFAULT 0 NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS run_traces (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                run_id VARCHAR(32) NOT NULL,
                node VARCHAR(50) NOT NULL,
                duration_ms INTEGER DEFAULT 0 NOT NULL,
                input TEXT NOT NULL,
                output TEXT NOT NULL,
                error TEXT,
                created_at DATETIME NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS operations (
                id VARCHAR(32) NOT NULL PRIMARY KEY,
                workspace_id VARCHAR(32) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
                message_id VARCHAR(32),
                tool_name VARCHAR(100) NOT NULL,
                rel_path VARCHAR(500) NOT NULL,
                arguments TEXT NOT NULL,
                diff TEXT NOT NULL,
                summary TEXT NOT NULL,
                status VARCHAR(9) DEFAULT 'proposed' NOT NULL,
                backup_path VARCHAR(1000),
                result TEXT,
                error TEXT,
                created_at DATETIME NOT NULL,
                resolved_at DATETIME
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mcp_tools (
                name VARCHAR(100) NOT NULL PRIMARY KEY,
                description TEXT NOT NULL,
                read_only BOOLEAN DEFAULT 0 NOT NULL,
                destructive BOOLEAN DEFAULT 0 NOT NULL,
                schema_json TEXT NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """);

    /** 与 SQLAlchemy index=True 在旧库里生成的索引名保持一致。 */
    private static final List<String> INDEXES = List.of(
            "CREATE INDEX IF NOT EXISTS ix_document_files_workspace_id ON document_files (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_chunks_workspace_id ON chunks (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_chunks_file_id ON chunks (file_id)",
            "CREATE INDEX IF NOT EXISTS ix_chunks_parent_id ON chunks (parent_id)",
            "CREATE INDEX IF NOT EXISTS ix_chunk_workspace_file ON chunks (workspace_id, file_id)",
            "CREATE INDEX IF NOT EXISTS ix_chunk_level_workspace ON chunks (level, workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_messages_workspace_id ON messages (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_messages_created_at ON messages (created_at)",
            "CREATE INDEX IF NOT EXISTS ix_run_traces_workspace_id ON run_traces (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_run_trace_run ON run_traces (workspace_id, run_id)",
            "CREATE INDEX IF NOT EXISTS ix_operations_workspace_id ON operations (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_operations_created_at ON operations (created_at)");

    /** table -> (column, DDL 类型, 默认字面量或 null)；与 Python 版 _ADDITIVE_COLUMNS 对齐。 */
    private record AdditiveColumn(String table, String column, String ddlType, String defaultLiteral) {}

    private static final List<AdditiveColumn> ADDITIVE_COLUMNS = List.of(
            // 层级化之前的旧行都是可检索单元，回填 "child" 保持其行为不变。
            new AdditiveColumn("chunks", "level", "VARCHAR(10)", "'child'"),
            new AdditiveColumn("chunks", "parent_id", "VARCHAR(32)", null),
            // 点赞/点踩；NULL 让旧消息行保持合法。
            new AdditiveColumn("messages", "feedback", "VARCHAR(10)", null));

    /** 需要归一化的时间戳列（旧 Python 版写成文本，Java 版写成整数）。 */
    private record TimestampColumn(String table, String column) {}

    private static final List<TimestampColumn> TIMESTAMP_COLUMNS = List.of(
            new TimestampColumn("workspaces", "created_at"),
            new TimestampColumn("workspaces", "updated_at"),
            new TimestampColumn("document_files", "created_at"),
            new TimestampColumn("document_files", "indexed_at"),
            new TimestampColumn("messages", "created_at"),
            new TimestampColumn("conversation_summaries", "updated_at"),
            new TimestampColumn("run_traces", "created_at"),
            new TimestampColumn("operations", "created_at"),
            new TimestampColumn("operations", "resolved_at"));

    /** Python/SQLAlchemy 的写法：'YYYY-MM-DD HH:MM:SS[.ffffff]'（UTC，无时区后缀）。 */
    private static final DateTimeFormatter LEGACY_TIMESTAMP = new DateTimeFormatterBuilder()
            .appendPattern("yyyy-MM-dd HH:mm:ss")
            .optionalStart()
            .appendFraction(ChronoField.NANO_OF_SECOND, 0, 9, true)
            .optionalEnd()
            .toFormatter();

    private final JdbcTemplate jdbc;

    public DbInitializer(DataSource dataSource) {
        this.jdbc = new JdbcTemplate(dataSource);
    }

    @Override
    public void run(ApplicationArguments args) {
        for (String ddl : TABLES) {
            jdbc.execute(ddl);
        }
        // 补列必须先于索引：旧库里 ix_chunks_parent_id 引用的列此刻才存在。
        applyAdditiveMigrations();
        for (String ddl : INDEXES) {
            jdbc.execute(ddl);
        }
        normalizeLegacyTimestamps();
    }

    private void applyAdditiveMigrations() {
        for (AdditiveColumn migration : ADDITIVE_COLUMNS) {
            if (!tableExists(migration.table()) || columnExists(migration.table(), migration.column())) {
                continue;
            }
            String clause = "ALTER TABLE " + migration.table()
                    + " ADD COLUMN " + migration.column() + " " + migration.ddlType();
            if (migration.defaultLiteral() != null) {
                clause += " DEFAULT " + migration.defaultLiteral();
            }
            jdbc.execute(clause);
            log.info("migration: added column {}.{}", migration.table(), migration.column());
        }
    }

    /**
     * 把旧库里的**文本**时间戳归一为 Java 侧使用的整数 epoch-ms。
     *
     * <p>原因：Python 版经 SQLAlchemy 把时间写成 'YYYY-MM-DD HH:MM:SS.ffffff'（UTC）文本，
     * 而 Java 侧经 sqlite-jdbc 存/读的是整数毫秒。同一列混用两种类型时，SQLite 会按**类型**
     * 而非时间排序（NULL &lt; INTEGER &lt; TEXT），导致 {@code ORDER BY created_at} 取不到最新、
     * {@code MAX(created_at)} 算错；且 sqlite-jdbc 解析 6 位小数文本会得到错误的时刻。</p>
     *
     * <p>以 {@code typeof(col) = 'text'} 为守卫：转换后不再命中，因此每次启动可安全重复执行
     * （等价于一次性的数据归一，不做其它数据改动）。</p>
     */
    private void normalizeLegacyTimestamps() {
        for (TimestampColumn column : TIMESTAMP_COLUMNS) {
            if (!tableExists(column.table()) || !columnExists(column.table(), column.column())) {
                continue;
            }
            List<Map<String, Object>> rows = jdbc.queryForList(
                    "SELECT rowid AS rid, " + column.column() + " AS ts FROM " + column.table()
                            + " WHERE typeof(" + column.column() + ") = 'text'");
            for (Map<String, Object> row : rows) {
                Object raw = row.get("ts");
                if (!(raw instanceof String text) || text.isBlank()) {
                    continue;
                }
                try {
                    long millis = LocalDateTime.parse(text.trim(), LEGACY_TIMESTAMP)
                            .toInstant(ZoneOffset.UTC)
                            .toEpochMilli();
                    jdbc.update("UPDATE " + column.table() + " SET " + column.column()
                            + " = ? WHERE rowid = ?", millis, row.get("rid"));
                } catch (DateTimeParseException ex) {
                    log.warn("skip unparseable legacy timestamp {}.{} (rowid={}): {}",
                            column.table(), column.column(), row.get("rid"), raw);
                }
            }
        }
    }

    private boolean tableExists(String table) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
                Integer.class, table);
        return count != null && count > 0;
    }

    private boolean columnExists(String table, String column) {
        return Boolean.TRUE.equals(jdbc.query(
                "PRAGMA table_info(" + table + ")",
                (rs, i) -> column.equalsIgnoreCase(rs.getString("name"))).stream().anyMatch(b -> b));
    }
}
