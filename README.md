# naive-support-agent

A Telegram assistant for support staff. A staff member forwards a client's message or describes a case. The assistant checks the organization's data, code and interface, then says what happened, whose error it is, what to tell the client and who does what next. It only reads and advises. It changes no data, refunds nothing, files no tasks and never writes to clients. When it can't find the answer, it calls a responsible person and hands over what it has already checked.

Nothing about the organization or its product is in this repository. Each installation brings its own instructions, sources, chats and identity provider.

## What an installation needs

- A Telegram bot. In BotFather, set the bot up in this order:
  1. `/setprivacy` → Disable. Without it the bot never sees a mention of itself in a group.
  2. Add the bot to the organization's internal groups. Use only groups of staff, never a chat that has clients in it.
  3. `/setjoingroups` → Disable. Nobody can add the bot anywhere else after that. If it is added to an unlisted group anyway, it leaves right away.
- An OpenID Connect provider with the device authorization grant (RFC 8628) and `offline_access` on for the assistant's client. The client needs two roles: one that gives access to the assistant and one for the responsible people it calls. A staff member writes to the bot, signs in at the provider with the code the bot sends, and their Telegram account gets bound to their account. On every request the assistant asks the provider whether the account is still enabled and still holds the role. Disabling the account or taking the role away ends access on the next message.
- Sources. Two kinds are supported: MCP servers (a database, logs, an error tracker, a task tracker, a browser) and a git repository of the code. Every source is read-only, and the assistant calls only the tools the installation lists. Give each source access that can't write anyway. An MCP server is reached in one of three ways: `stdio` starts it inside the assistant's container, so it has to be in the image; `http` (streamable HTTP) and `sse` reach a server over the network, with `token_env` naming the variable that holds its bearer token if it asks for one. A server that isn't in the image can run as another container of the assistant's pod (`extraContainers` in the chart), reached at `localhost`.
- A test account of the product, optional. It must have no money and no saved way to pay. The browser source is started signed in to it, and the assistant walks the client's path there.
- PostgreSQL for the assistant's own data: bindings, cases, the journal.
- An Anthropic API key.

## Instructions

The installation mounts a directory:

```
instructions/
  10-cases.md     how to investigate, where the data is, known failures
  20-rules.md     who may do what, compensation rules
  30-tone.md      language and tone of the answers
  sources.toml    the sources and the fields with personal data
```

All `*.md` files are joined in name order and open the system prompt. `dev/instructions/` has an example, including the format of `sources.toml`. The assistant reads the directory at start, so restart it after changing the instructions. It won't start without them.

## Personal data

Emails and phone numbers are swapped for labels in every text before it reaches the model. Names and payment data are swapped too when they sit in the fields `sources.toml` lists. People see the real values in the bot's answers under a spoiler, which opens on a tap. Pictures and videos of clients are sent under a spoiler as well. One gap remains: names written inside a client's own text, and the client's files, go to the model as they are.

In the assistant's database, the conversation of a case, its labels and the messages waiting to be handled are encrypted with `ENCRYPTION_KEY`, as are the staff's tokens. A case nobody worked on for `CASE_RETENTION_DAYS` is deleted with all of that. The journal keeps who asked about which client and when. A scheduled check counts as work, so a case that waits for a fix stays. Changing the key makes the stored cases and bindings unreadable.

## Settings

All settings come from the environment. None has a default, and the service won't start with one missing.

| Variable | |
|---|---|
| `TELEGRAM_BOT_TOKEN` | secret |
| `TELEGRAM_BOT_USERNAME` | without the `@` |
| `TELEGRAM_EGRESS` | `direct` or `proxy` |
| `TELEGRAM_PROXY_URL` | with `proxy`; secret, may carry a password |
| `TELEGRAM_GROUPS` | numbers of the internal groups, comma separated; empty means private chats only |
| `LOCALE` | `ru` or `en`: the assistant's own messages (answers follow the instructions) |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID` | |
| `OIDC_CLIENT_SECRET` | secret |
| `OIDC_ROLES_CLAIM` | dotted path to the roles in an access token, e.g. `resource_access.support-assistant.roles` |
| `ROLE_SUPPORT`, `ROLE_OWNER` | the role that gives access; the role of responsible people |
| `ROLE_ANALYTICS`, `ANALYTICS_ROLES_CLAIM` | the role of people who may ask for aggregate figures (totals, breakdowns), and where the provider lists it; it may be a role of another application, e.g. `resource_access.admin-panel.roles` |
| `ANTHROPIC_API_KEY` | secret |
| `MODEL`, `MODEL_EFFORT` | e.g. `claude-opus-5-5`, `high` |
| `CASE_TOKEN_BUDGET` | tokens one request about a case may spend |
| `INSTRUCTIONS_DIR` | the instructions directory |
| `ENCRYPTION_KEY` | secret: a Fernet key that encrypts the staff's offline tokens and the data of cases |
| `CASE_RETENTION_DAYS` | after how many days without a turn a case is deleted |
| `FOLLOWUP_INTERVAL_MINUTES` | how often cases waiting for a fix are checked again |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `DB_PASSWORD` is secret |

The variables named in `sources.toml` (`env`, `token_env`) are passed to their source and to nothing else.

## Running locally

```bash
docker compose up -d
```

This starts PostgreSQL and a Keycloak with the realm `support`. The realm has a client `support-assistant` with the device grant and three users: `anna` (support), `oleg` (support, owner) and `eve` (no role). Each password is the same as the user name.

```bash
cd server
uv sync
uv run pytest
```

The tests use the `assistant_test` database of that PostgreSQL and fake every outside service. No test reaches Telegram, the provider or the model. To run the assistant itself with a bot of your own, put its settings in the environment and run `uv run alembic upgrade head`, then `uv run python cmd/bot/main.py`. Telegram hands a bot's updates to one reader only, so don't run it locally while the same bot runs elsewhere.

## Deploying

The Helm chart is in `deploy/helm/naive-support-agent`, and `ci/example-values.yaml` there is a complete example. A tag `vX.Y.Z` publishes the image `ghcr.io/adalekin/naive-support-agent` and the chart to `oci://ghcr.io/adalekin/charts`. The chart runs one pod (a second reader of the bot's updates would conflict) and a migrations Job; the assistant waits for the schema before it reads anything.

## License

MIT
