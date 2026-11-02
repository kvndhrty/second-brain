# brain web UI

React + TypeScript + Vite. The production build is written into
`src/brain/web/static/` and shipped inside the Python wheel, so Node is needed
only to build it.

```sh
npm ci
npm run dev        # http://localhost:5173, forwarding /api and /mcp to brain start on :8765
npm run typecheck
npm test
npm run build      # into ../src/brain/web/static
```

Run the backend for development with `brain start` in another terminal.
`BRAIN_FAKE_LLM=1 brain start` works without a model endpoint. Point the dev
server at a different backend with `BRAIN_URL=http://127.0.0.1:9000 npm run dev`.

Node 24 is pinned in `.node-version` (read by mise, fnm, and the CI setup).
