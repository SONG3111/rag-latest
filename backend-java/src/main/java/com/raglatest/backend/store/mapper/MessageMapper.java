package com.raglatest.backend.store.mapper;

import java.time.OffsetDateTime;
import java.util.List;
import org.apache.ibatis.annotations.Insert;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/** messages 的 SQL：历史窗读、计数、书签时间与两条写入。 */
@Mapper
public interface MessageMapper {

    record MessageRow(
            String id,
            String role,
            String content,
            String userId,
            String toolCalls,
            String citations,
            String feedback,
            OffsetDateTime createdAt) {}

    String COLUMNS = """
            SELECT id, role, content, user_id, tool_calls, citations, feedback, created_at
            FROM messages
            """;

    @Select(COLUMNS + " WHERE workspace_id = #{workspaceId}"
            + " ORDER BY created_at DESC, id DESC LIMIT #{limit}")
    List<MessageRow> recentNewestFirst(@Param("workspaceId") String workspaceId,
                                       @Param("limit") int limit);

    @Select("SELECT COUNT(*) FROM messages WHERE workspace_id = #{workspaceId}")
    long countAll(@Param("workspaceId") String workspaceId);

    @Select("SELECT MAX(created_at) FROM messages WHERE workspace_id = #{workspaceId}")
    OffsetDateTime latestCreatedAt(@Param("workspaceId") String workspaceId);

    @Select(COLUMNS + " WHERE id = #{messageId} AND workspace_id = #{workspaceId}")
    MessageRow findInWorkspace(@Param("workspaceId") String workspaceId,
                               @Param("messageId") String messageId);

    @Insert("""
            INSERT INTO messages (id, workspace_id, user_id, role, content, tool_calls, citations, created_at)
            VALUES (#{id}, #{workspaceId}, #{userId}, #{role}, #{content}, #{toolCalls},
                    #{citations}, #{now})
            """)
    int insert(@Param("id") String id,
               @Param("workspaceId") String workspaceId,
               @Param("userId") String userId,
               @Param("role") String role,
               @Param("content") String content,
               @Param("toolCalls") String toolCalls,
               @Param("citations") String citations,
               @Param("now") OffsetDateTime now);

    @Update("UPDATE messages SET feedback = #{feedback} WHERE id = #{messageId}")
    int updateFeedback(@Param("messageId") String messageId, @Param("feedback") String feedback);
}
