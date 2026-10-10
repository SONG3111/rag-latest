package com.raglatest.backend.store.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.raglatest.backend.store.entity.UserEntity;
import java.util.List;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

/**
 * users 的 CRUD（{@link BaseMapper} 提供 selectById/insert/updateById/deleteById
 * 与条件构造器），外加两条需要 SQL 表达力的查询。
 */
@Mapper
public interface UserMapper extends BaseMapper<UserEntity> {

    /** 登录与唯一性校验一律按小写邮箱匹配（users 上有 lower(email) 唯一索引）。 */
    @Select("SELECT * FROM users WHERE lower(email) = lower(#{email})")
    UserEntity findByEmail(@Param("email") String email);

    /** 管理员列表：新号在前。 */
    @Select("SELECT * FROM users ORDER BY created_at DESC")
    List<UserEntity> listAll();

    /** 空库判定（首启引导管理员用）。 */
    @Select("SELECT COUNT(*) FROM users")
    long countAll();

    /**
     * 最后一个管理员不可停用/降级的判据：除自己以外还有几个 active 的 admin。
     * 放在 SQL 里而不是"先查再判断"，是为了让判定与写入落在同一条语句的读视图上。
     */
    @Select("""
            SELECT COUNT(*) FROM users
            WHERE role = 'admin' AND status = 'active' AND id <> #{excludeUserId}
            """)
    long countOtherActiveAdmins(@Param("excludeUserId") String excludeUserId);

    @Update("UPDATE users SET last_login_at = NOW() WHERE id = #{id}")
    void touchLastLogin(@Param("id") String id);
}
