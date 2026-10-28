# Attribution

This package uses the official `larksuite/oapi-sdk-python` (MIT) for Feishu
Mail v1, Calendar v4, and OAuth Device Flow.

The narrow tool surface follows this project's existing MCP architecture:
read-only tools execute directly, while draft/send/calendar-create tools are
approval-gated by the host application. No GPL code is included.

The IMAP/SMTP-oriented projects `Wh1isper/mcp-email-server` (BSD-3-Clause) and
`ikvk/imap_tools` (Apache-2.0) were reviewed as references for generic mail
semantics, but this implementation uses Feishu's official HTTP API rather than
copying their code.
