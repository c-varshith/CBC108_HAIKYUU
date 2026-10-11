# Calibrex

<div align="center">
  <p><strong>Image-based inventory intelligence for retail shelves</strong><br>
  Turn shelf photos into package-count estimates, match detections to your catalog, and review every count before approval.</p>
  <p>
    <a href="#features">Features</a> ·
    <a href="#workflow">Workflow</a> ·
    <a href="#screenshots">Screenshots</a> ·
    <a href="#setup">Setup</a> ·
    <a href="#stack">Stack</a>
  </p>
</div>

> **Human review stays in the loop:** Calibrex counts visible package detections. Staff review and correct estimates before a manager approves a scan.

## At a glance

| Area | Details |
| --- | --- |
| Product | Shelf-image inventory counting and variance review |
| Detection | Fine-tuned Calibrex YOLO checkpoint |
| Catalog matching | DINOv2 image embeddings with Qdrant search |
| Workflow | Staff review, manager approval, reports, and audit history |
| Stack | React + TypeScript · FastAPI · PostgreSQL · Qdrant |

## Features

- Detect visible packages in shelf photos and estimate quantities by matched SKU.
- Keep unmatched detections under **Unknown** until staff resolves them.
- Review scans, compare counts with expected stock, and approve submitted counts.
- Use live camera detection where browser permissions and a secure origin are available.
- Track scan decisions and inventory changes through reports and the audit trail.

## Workflow

1. Add products and reference photos to the catalog.
2. Capture or upload a shelf image. The detector finds package boxes; catalog photos help match boxes to SKUs.
3. Staff review detections, correct uncertain matches, and submit the count.
4. A manager approves or returns the scan. Approved counts update inventory variance and reports.

Catalog photos improve SKU matching; they do not retrain the detector. See the [catalog setup and import guide](docs/catalog-import.md).

## Screenshots

### Landing page

![Calibrex landing page](docs/screenshots/landing.png)

### Workspace dashboard

![Calibrex workspace dashboard](docs/screenshots/dashboard.png)

## Setup

### Run with Docker Compose

The standard stack starts the web app, API, PostgreSQL, Qdrant, and the real CPU inference worker:

```bash
docker compose up --build
```

Open [http://localhost:8080](http://localhost:8080) and create the first Owner account. The default setup does not seed demo users or catalog data. Add product reference photos in **Catalog** before scanning to enable automatic SKU matching. The first worker startup downloads DINOv2 weights.

The worker loads the fine-tuned checkpoint at `ai-worker/runs/detect/runs/detect/calibrex_s960/weights/best.pt`. Docker copies it into the worker image; a missing or invalid checkpoint causes startup to fail clearly. The optional H5 model is excluded from GitHub because of its size. To use it locally, place `iou_resnet50_csv_06.h5` in the project root and start with the H5 overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.h5.yml up --build
```

### Production

Copy `.env.production.example` to `.env.production`, set deployment secrets and public URLs, then start the production overlay:

```bash
docker compose --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

Keep production secrets out of source control. See [backup and restore](ops/restore-local.md) for local data recovery guidance.

## Stack

| Component | Role |
| --- | --- |
| React, TypeScript, Vite | Web application |
| FastAPI, SQLAlchemy, Alembic | Authenticated API and database migrations |
| PostgreSQL | Organizations, catalog, scans, inventory, and audit data |
| Calibrex YOLO | Visible package detection |
| DINOv2 and Qdrant | Product-reference image matching |

## Guides

- [Catalog setup and Open Food Facts imports](docs/catalog-import.md)
- [AI worker configuration and evaluation](ai-worker/README.md)
- [Model training](docs/TRAINING.md)
- [Local test guide](TEST_GUIDE.md)
- [Backup and restore](ops/restore-local.md)
