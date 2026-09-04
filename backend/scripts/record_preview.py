"""Record the API responses the shareable preview renders from.

    python -m scripts.record_preview

The preview (`frontend/npm run build:demo`) uses the real pages, components
and stylesheet — only the transport is swapped for a lookup against the file
this writes. That is the point: it cannot drift into being a mock-up of the
app, because it *is* the app, rendering responses a running backend actually
returned.

Recording them by hand, which is how this started, meant the preview silently
fell behind every time an endpoint gained a field. This makes it one command.

Run it against a seeded development database:

    AQM_DATABASE_URL=sqlite:///./demo.db python -m scripts.seed --example
    AQM_DATABASE_URL=sqlite:///./demo.db python -m scripts.record_preview
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import Customer, Enquiry

OUT = Path(__file__).resolve().parents[2] / "frontend" / "demo" / "fixtures.json"

#: Paths that do not depend on which rows exist.
STATIC_PATHS = [
    "/queue?sort=flags&include_closed=false",
    "/queue?sort=age&include_closed=false",
    "/queue?sort=value&include_closed=false",
    "/queue?sort=confidence&include_closed=false",
    "/queue?sort=flags&include_closed=true",
    "/admin/rates",
    "/admin/rules",
    "/admin/rules/promotion-candidates",
    "/admin/market",
    "/admin/market/sources",
    "/admin/stock",
    "/reports/turnaround",
    "/reports/extraction-accuracy",
    "/reports/win-rate",
    "/reports/time-source-mix",
    "/reports/estimate-vs-actual",
]


def main() -> int:
    settings = get_settings()
    if settings.environment not in ("development", "test"):
        print(f"Refusing to record from a '{settings.environment}' database.")
        return 1

    db = SessionLocal()
    try:
        enquiry_ids = [row.id for row in db.query(Enquiry).all()]
        customer_ids = [row.id for row in db.query(Customer).all()]
        part_ids = [part.id for enquiry in db.query(Enquiry).all() for part in enquiry.parts]
    finally:
        db.close()

    if not enquiry_ids:
        print("Nothing to record — seed the database first (scripts.seed --example).")
        return 1

    paths = [
        *STATIC_PATHS,
        *(f"/enquiries/{i}" for i in enquiry_ids),
        *(f"/customers/{i}" for i in customer_ids),
        *(f"/search/similar?part_id={i}" for i in part_ids),
    ]

    client = TestClient(app)
    client.headers["X-User-Email"] = "estimator@edmzone.example"

    recorded: dict[str, object] = {}
    missing: list[str] = []
    for path in paths:
        response = client.get(path)
        if response.status_code != 200:
            missing.append(f"{path} -> HTTP {response.status_code}")
            continue
        recorded[path] = response.json()

    OUT.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
    size_kb = OUT.stat().st_size / 1024

    print(f"\nRecorded {len(recorded)} response(s) to {OUT} ({size_kb:.0f} kB)")
    for failure in missing:
        print(f"  skipped: {failure}")
    print("\nNow run:  cd frontend && npm run build:demo\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
