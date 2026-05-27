# Feishu Listener

Independent Feishu event listener for this n8n compose stack.

## Runtime

The service uses the Feishu/Lark Python SDK long connection client. It does not
need a public HTTP callback URL. It connects outbound to Feishu, normalizes
events, matches routes in `config.yaml`, and writes event/dispatch logs to
SQLite.

The first invoice route is intentionally configured with:

```yaml
dispatch_enabled: false
```

That means matching events are logged but do not call n8n yet.

## Required env

Set these in the project `.env` before starting the service:

```env
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=
```

`FEISHU_VERIFICATION_TOKEN` and `FEISHU_ENCRYPT_KEY` can stay empty if the
Feishu event subscription does not use them.

## Start

```bash
docker compose up -d feishu-listener
```

## Debug endpoints

- `GET http://localhost:8010/health`
- `GET http://localhost:8010/routes`
- `GET http://localhost:8010/events?limit=50`
- `POST http://localhost:8010/debug/normalize`

Event logs are stored in `feishu-listener/data/events.sqlite`.
