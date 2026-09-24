package com.raglatest.backend.store;

import com.raglatest.backend.api.dto.Dtos.WorkspaceRead;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

@Repository
public class WorkspaceRepository {

    /** 与 python uuid4().hex 同形态的 32 位十六进制 id。 */
    public static String newId() {
        return UUID.randomUUID().toString().replace("-", "");
    }

    private final JdbcClient jdbc;

    public WorkspaceRepository(JdbcClient jdbc) {
        this.jdbc = jdbc;
    }

    private static final String SELECT_WITH_COUNT = """
            SELECT w.id, w.name, w.description, w.created_at, w.updated_at,
                   (SELECT COUNT(*) FROM document_files f WHERE f.workspace_id = w.id) AS file_count
            FROM workspaces w
            """;

    public List<WorkspaceRead> list() {
        return jdbc.sql(SELECT_WITH_COUNT + " ORDER BY w.created_at DESC")
                .query((rs, i) -> new WorkspaceRead(
                        rs.getString("id"), rs.getString("name"), rs.getString("description"),
                        toInstant(rs.getTimestamp("created_at")),
                        toInstant(rs.getTimestamp("updated_at")),
                        rs.getInt("file_count")))
                .list();
    }

    public Optional<WorkspaceRead> find(String workspaceId) {
        return jdbc.sql(SELECT_WITH_COUNT + " WHERE w.id = :id")
                .param("id", workspaceId)
                .query((rs, i) -> new WorkspaceRead(
                        rs.getString("id"), rs.getString("name"), rs.getString("description"),
                        toInstant(rs.getTimestamp("created_at")),
                        toInstant(rs.getTimestamp("updated_at")),
                        rs.getInt("file_count")))
                .optional();
    }

    public boolean exists(String workspaceId) {
        Long count = jdbc.sql("SELECT COUNT(*) FROM workspaces WHERE id = :id")
                .param("id", workspaceId)
                .query(Long.class)
                .single();
        return count != null && count > 0;
    }

    public WorkspaceRead create(String name, String description) {
        String id = newId();
        Instant now = Instant.now();
        jdbc.sql("""
                INSERT INTO workspaces (id, name, description, created_at, updated_at)
                VALUES (:id, :name, :description, :created, :updated)
                """)
                .param("id", id)
                .param("name", name)
                .param("description", description)
                .param("created", Timestamp.from(now))
                .param("updated", Timestamp.from(now))
                .update();
        return new WorkspaceRead(id, name, description, now, now, 0);
    }

    /** 级联删除依赖连接级 PRAGMA foreign_keys=ON（见 application.yml 数据源属性）。 */
    public void delete(String workspaceId) {
        jdbc.sql("DELETE FROM workspaces WHERE id = :id").param("id", workspaceId).update();
    }

    static Instant toInstant(Timestamp timestamp) {
        return timestamp == null ? null : timestamp.toInstant();
    }
}
