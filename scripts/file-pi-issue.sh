#!/bin/bash
# One-shot: file ANTHROPIC_BASE_URL issue on badlogic/pi-mono, then self-destruct.
set -euo pipefail

gh issue create --repo badlogic/pi-mono \
  --title "pi-ai: ANTHROPIC_BASE_URL env var not respected in createClient" \
  --body "$(cat <<'EOF'
## Problem

The `createClient` function in `packages/ai/src/providers/anthropic.ts` always passes `model.baseUrl` (hardcoded per-model in `models.generated.ts`) to the Anthropic SDK constructor:

```ts
const client = new Anthropic({
    apiKey,
    baseURL: model.baseUrl,  // always "https://api.anthropic.com"
    ...
});
```

This overrides the Anthropic SDK's native support for the `ANTHROPIC_BASE_URL` environment variable, making it impossible to route requests through a proxy or custom endpoint.

## Expected behavior

`ANTHROPIC_BASE_URL` should take precedence when set, falling back to `model.baseUrl`:

```ts
baseURL: process.env.ANTHROPIC_BASE_URL || model.baseUrl,
```

Both occurrences in `createClient` (OAuth path line ~391 and API key path line ~404) need this change.

## Use case

Running pi-mcp-server in a Docker container that routes API calls through a credential-injecting reverse proxy on an isolated Docker network. The container has no credentials — a sidecar proxy injects auth headers. This pattern requires `ANTHROPIC_BASE_URL` to redirect SDK requests to the proxy.

## Workaround

```bash
sed -i 's#baseURL: model\.baseUrl,#baseURL: process.env.ANTHROPIC_BASE_URL || model.baseUrl,#g' \
  .../pi-ai/dist/providers/anthropic.js
```

## Version

pi-mcp-server 0.1.2 / @mariozechner/pi-ai (bundled)
EOF
)"

echo "Issue filed. Removing cron entry and this script."
# Remove self from crontab
crontab -l 2>/dev/null | grep -v 'file-pi-issue' | crontab - 2>/dev/null || true
rm -f "$0"
