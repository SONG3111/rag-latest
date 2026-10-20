# mcp-google-server

Minimal stdio MCP server for Gmail and Google Calendar. It exposes five read
tools and four approval-gated write tools:

- `search_emails`, `read_email`, `get_email_thread`
- `list_calendar_events`, `find_free_slots`
- `save_reply_draft`, `send_reply`, `send_draft`, `create_calendar_event`

The server is stateless. In particular, it does not implement approval: the
host application records a proposal and replays the exact tool call only after
the user approves it.

## OAuth

Create a Google Cloud OAuth client of type **Desktop app**, then run:

```powershell
$env:GOOGLE_CLIENT_SECRETS_PATH = "C:\path\to\client_secret.json"
$env:GOOGLE_TOKEN_PATH = "C:\path\to\data\secrets\google_token.json"
python -m mcp_google_server.auth
```

The token is written locally and should never be committed. The requested
scopes cover Gmail read/compose/send and Calendar read/events.

## Run

```powershell
$env:GOOGLE_TOKEN_PATH = "C:\path\to\data\secrets\google_token.json"
python -m mcp_google_server.server
```

The host backend launches this process over stdio and reads approval metadata
from each tool's `readOnlyHint` annotation.
