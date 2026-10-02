# BUG-008：Git Bash 下 rm 带反斜杠路径被 MSYS 转换，误删整个 src/test/java

- 日期：2026-10-02（清理 Mimosa hook 状态目录时发生）
- 模块：无代码缺陷——工具链路径语义事故，留档防再犯
- 状态：已恢复（git checkout + 重放未提交修改，verify 全绿后确认）

## 现象

Mimosa hook 在源码树留下带反斜杠的怪目录（`src/test/\java\com\...\hook-state\...`）。
执行 `rm -rf 'backend-java/src/test/\java'` 想删除该怪目录时，**整个
`src/test/java` 目录（16 个测试文件）被删除**，git status 全线标 D。

## 根因

Git Bash（MSYS2）的路径转换：单引号里的字面量 `src/test/\java` 传给底层 Windows
程序（rm 是 MSYS 程序但路径启发式仍在）时，`\` 被当作分隔符规范化为 `/`——
"删除名为 `\java` 的目录"变成了"删除 `src/test/java`"。反斜杠开头的路径段在
MSYS 与 Windows 两套语义之间没有安全解释。

## 恢复过程

1. `git checkout -- backend-java/src/test/` 恢复全部 16 个文件到 HEAD；
2. 重放当日未提交的修改（批量消费适配：IndexingConsumerTests 重写 + 3 个文件的
   /v1/index-batch 桩与 onReindexTasks 调用）；
3. `mvnw verify` 99 单测 + 4 容器 IT 全绿确认无丢失。

## 预防 / 教训

- Git Bash 里**永远不要对含反斜杠的路径执行 rm**；先 `ls -la` 或 `find` 确认目标，
  或者用 `find . -name 'hook-state' -type d -prune` 精确定位后再删其父目录。
- 删除类命令前先跑一遍"预演"（`echo` 目标列表）；配合 git 的话先确认目标在
  git 管辖内（有 HEAD 兜底）——本次能完整恢复正因所有文件都已提交过。
