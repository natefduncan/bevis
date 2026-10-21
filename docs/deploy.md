# Deploying Bevis

Bevis is one container plus one SQLite file (SPEC §2). This guide runs it on
a small always-on server behind Caddy, which handles HTTPS.

## What you need

- A Linux server with Docker and the Compose plugin. 1 CPU and 1 GB of
  memory are enough for a team; PDF rendering is the heaviest work.
- A DNS name pointing at the server, such as `bevis.example.com`.
- Ports 80 and 443 open to the internet. Caddy uses port 80 to obtain the
  certificate.

## First start

```sh
git clone https://github.com/natefduncan/bevis.git
cd bevis/deploy
export BEVIS_DOMAIN=bevis.example.com
docker compose up -d --build
docker compose ps          # bevis should become "healthy"
curl https://$BEVIS_DOMAIN/healthz
```

Make an agent key and give it to the agent's runtime as a secret:

```sh
docker compose exec bevis bevis keys create claude-analyst
```

The key is printed once. The agent connects its MCP client to
`https://bevis.example.com/mcp/`, with the header
`Authorization: Bearer <key>`, or uses the REST API at `/api`.

When the agent creates a report, the response holds a one-time **admin link**
for the report's owner. The owner opens `https://bevis.example.com/view#key=…`
and uses **Share** to make links for readers and commenters.

## Configuration

Environment variables on the `bevis` service:

| Variable | Default | Meaning |
|---|---|---|
| `BEVIS_DATA_DIR` | `/data` | Where the database and the PDF/HTML cache live |
| `BEVIS_MAX_ROWS` | `10000` | Row cap per evidence record |
| `BEVIS_MAX_BYTES` | `5242880` | Size cap per evidence record, in bytes |

On the Compose project: `BEVIS_DOMAIN` (required), and `BEVIS_HTTP_PORT` and
`BEVIS_HTTPS_PORT` (default 80 and 443) for the ports Caddy publishes.

Secrets are never kept in the database in the clear. Agent keys and links are
stored as SHA-256 hashes. A lost key cannot be recovered; revoke it and make
a new one.

## Backups

All state is one file. `bevis backup` copies it consistently while the host
runs:

```sh
docker compose exec bevis bevis backup /data/backups/bevis-$(date +%F).db
docker compose cp bevis:/data/backups/bevis-$(date +%F).db ./
```

Run this daily from cron and keep copies off the server. The `exports/`
folder in the data volume is a cache of rendered PDFs and HTML; it does not
need backing up.

To restore, stop the host, put the backup in place as `/data/bevis.db`, and
start it again:

```sh
docker compose stop bevis
docker compose cp ./bevis-2026-10-21.db bevis:/data/bevis.db
docker compose start bevis
```

## Upgrades

```sh
git pull
docker compose exec bevis bevis backup /data/backups/before-upgrade.db
docker compose up -d --build
```

The host migrates the database on start when the schema has changed. A
database from a newer version of Bevis is refused rather than modified.

## Agent keys

```sh
docker compose exec bevis bevis keys list
docker compose exec bevis bevis keys revoke key_…
```

An agent key is an editor on the reports it created and a suggester on all
others, including their drafts. Give each agent its own key, and revoke a key
when its agent is retired.

## Security notes

- Serve HTTPS only. Agent keys travel in the `Authorization` header and links
  in URL fragments; both are bearer secrets.
- The web view loads its chart libraries from cdn.jsdelivr.net. Its Content
  Security Policy allows only that origin for scripts, and only this host for
  everything else.
- Request bodies over about three times `BEVIS_MAX_BYTES` are refused unread.
  Caddy caps them at 20 MB as well.
- Logs never contain secrets. API calls carry them in headers, which neither
  uvicorn nor Caddy logs by default, and browsers never send URL fragments.
