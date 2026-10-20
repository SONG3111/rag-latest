# Attribution

This package is a small, local adaptation rather than a vendored copy of a
larger Google Workspace integration.

- Gmail search, thread/reply semantics, and Calendar event/free-busy handling
  were informed by `taylorwilsdon/google_workspace_mcp` (MIT). The upstream
  implementation was used as a reference for MIME parsing, reply headers, tool
  annotations, and timezone-aware Calendar payloads.
- The durable approval semantics used by the host application were informed by
  `Anil-matcha/open-agent-email` (Apache-2.0), in particular the idea of an
  atomic pending -> resolved transition and explicit expiry.
- Feishu long-connection and card-callback usage follows the official
  `larksuite/oapi-sdk-python` samples (MIT).

No GPL code is included. The package intentionally exposes only the operations
needed by this project's assistant.
