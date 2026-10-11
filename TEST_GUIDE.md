# Test guide

Use an isolated development database and explicitly enabled fixtures for browser workflows. Never enable fixture seeding in production. Commands below assume a project-root `.venv` and installed frontend dependencies.

## Backend

```bash
cd backend
../.venv/bin/python -m pytest -q
```

## AI worker

```bash
cd ai-worker
../.venv/bin/python -m pytest -q
```

## Frontend

```bash
cd frontend
npx tsc --noEmit
npm run test:unit
npm run build
```

## Browser workflows

Start an isolated seeded API in one terminal:

```bash
cd backend
DATABASE_URL=sqlite+aiosqlite:////tmp/calibrex-tests.db \
AUTO_CREATE_SCHEMA=true SEED_ON_START=true ENV=development \
EXPOSE_INVITE_TOKEN=true LOGIN_MAX_ATTEMPTS=20 \
../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In another terminal, start the frontend and run Playwright:

```bash
cd frontend
npm run dev
```

```bash
cd frontend
PLAYWRIGHT_BASE_URL=http://127.0.0.1:5173 npm run test:e2e
```

The browser workflow uses development fixtures created by the API. It does not require a real AI worker. For a normal scan with real detections, use the standard Docker stack and its Calibrex worker as described in the [README](README.md).
