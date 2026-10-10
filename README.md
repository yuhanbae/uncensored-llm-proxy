# uncensored-llm-proxy

Stable-URL proxy for the rotating `trycloudflare` tunnel of [yuhanbae/llm-compute](https://github.com/yuhanbae/llm-compute) (GitHub Actions llama.cpp serving `Qwen3-4B-Instruct-2507-abliterated`, 64k ctx).

- Deployed on Railway → **permanent public URL**, never rotates.
- On every request it resolves the *current* tunnel URL from the GitHub Gist (`url.txt`), so runner rotations (~6h) are invisible to clients.
- Streams SSE chat completions through unchanged; passes through `/v1/models`, `/v1/models/:id`, `/props`, `/health`.
- On upstream 5xx (tunnel mid-rotation) it force-refreshes the gist and retries once.

## Env vars

- `GIST_TOKEN` — GitHub PAT with gist read access
- `GIST_ID` — default `7ec01cb438fa7f501847b725ed44063a`
- `PORT` — set by Railway

## Deploy

```bash
docker build -t uncensored-llm-proxy . && docker run -p 8080:8080 -e GIST_TOKEN=ghp_xxx uncensored-llm-proxy
```

Then point pi's `models.json` → `providers.github-llm-compute.baseUrl` at `https://<service>.up.railway.app/v1` and never touch it again.
