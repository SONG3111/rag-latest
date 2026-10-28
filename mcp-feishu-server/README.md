# mcp-feishu-server

Minimal stdio MCP server for Feishu Mail and Feishu Calendar.

Tools:

- `search_emails`, `read_email`, `get_email_thread`
- `list_calendar_events`, `find_free_slots`
- `save_reply_draft`, `send_reply`, `send_draft`, `create_calendar_event`

The server is stateless and does not implement approval. The host stores an
exact tool proposal and calls it only after the user approves.

## Configuration

```ini
FEISHU_APP_ID=cli_xxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxx
FEISHU_MAIL_USER_MAILBOX_ID=you@example.com
FEISHU_CALENDAR_ID=
FEISHU_USER_TOKEN_PATH=...\data\secrets\feishu_user_token.json
FEISHU_OAUTH_SCOPES=
```

`FEISHU_CALENDAR_ID` may be empty; the server then resolves the user's primary
calendar.

## User authorization

Mail search/write and user calendar APIs require a Feishu user access token.
Run:

```powershell
python -m mcp_feishu_server.auth
```

The command starts Feishu's OAuth Device Flow, prints a verification URL, and
stores the access/refresh token locally. No public callback address is needed.
Grant the app the mail and calendar scopes shown in the Feishu developer
console; if your tenant requires explicit scope IDs, put them in
`FEISHU_OAUTH_SCOPES` as a space-separated list.
