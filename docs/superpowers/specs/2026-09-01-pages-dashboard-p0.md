# GitHub Pages Data Dashboard P0 Decision

## Decision

Issue [#21](https://github.com/trionnemesis/healthcare-opendata-mcp/issues/21)
adds a bounded, read-only data projection to GitHub Pages. It incrementally
changes the 2026-07-03 Pages design decision that listed a dynamic dashboard as
a non-goal; it does not replace that document or turn Pages into an application
runtime.

The architecture is:

```text
official PCC/NHI sources
  → hcmcp-sync
  → SQLite source of truth
  → strict, sanitized snapshot exporter
  → docs/data/current.json + pre-rendered dashboard summary
  → GitHub Pages
```

## First-principles boundary

1. SQLite and official sources remain authoritative. Pages is a rebuildable
   projection and never executes MCP, SQLite, or arbitrary SQL.
2. `generated_at` is the UTC snapshot build time. `status.source_max_date` is
   the newest matching PCC record date. Freshness is derived from the latest
   ingestion result/time, not from record recency: a narrowly scoped dataset
   can legitimately have no new matching tender. These facts are never merged.
3. Only the allowlisted PCC fields in schema v1 are published. SQLite, raw
   payloads, ingestion exceptions, credentials, internal paths, NHI addresses,
   and NHI telephone numbers are excluded.
4. Missing or invalid amounts are JSON `null`, not zero. Aggregates expose a
   `known_count` so readers can see the coverage boundary.
5. The complete projection is measured before publication. Above 5 MiB, the
   exporter keeps full aggregates and filters but bounds detail rows to the
   newest records. The chosen strategy and byte counts are explicit metadata.
6. The core status and KPI summary is pre-rendered into HTML. JavaScript only
   enhances the page with PCC search, filters, sorting, and fixed-size paging.
7. Upstream strings are inserted with `textContent` and DOM construction. The
   dashboard does not use HTML string injection.
8. Snapshot and generated HTML files are written through same-directory temp
   files and atomic replacement. A build failure does not replace the prior
   valid output with an empty snapshot.

## Snapshot contract v1

The machine-readable contract is `docs/data/schema-v1.json`. Top-level fields:

| Field | Meaning |
|---|---|
| `schema_version` | Consumer compatibility boundary; P0 is `1.0` |
| `generated_at` | Snapshot generation time in UTC |
| `status` | `fresh`, `stale`, `degraded`, or `empty`, plus reason and PCC source date |
| `datasets` | PCC/NHI row counts and fetch metadata |
| `summary` | Full PCC aggregates with null-aware amount coverage |
| `filters` | Deterministically ordered filter values |
| `export` | Full-size measurement, publication strategy, and byte limit |
| `rows` | Bounded, allowlisted PCC detail projection |

For a fixed database and explicit `--generated-at`, serialization and row order
are deterministic.

## P0 UI behavior

- The existing landing page remains the project introduction and links to
  `/dashboard/`.
- The dashboard pre-renders status, timestamps, PCC totals/date range/type and
  amount aggregates, and NHI row/fetch metadata.
- The JavaScript enhancement supports keyword search across title, agency,
  case number, and vendor; type/agency/date filters; date/budget/award sorting;
  and 20-row pages.
- Malformed or unavailable JSON changes the runtime banner to an error state,
  preserves the pre-rendered summary, and does not present a successful empty
  table.
- The table is keyboard-focusable and horizontally scrollable at 360px; focus,
  reduced-motion, and print styles remain explicit.

## Validation and deployment

`scripts/verify_dashboard.py` is a standard-library deployment gate. It checks
the snapshot shape, schema version, deterministic date ordering, allowlisted row
fields, size, required UI elements, safe DOM implementation, local links, and
absence of SQLite files. The Pages workflow runs this gate before uploading the
`docs/` artifact.

The committed P0 snapshot is rebuilt manually from a successful real `hcmcp-sync`
database. Automated sync, atomic last-known-good publication, and source failure
recovery belong to PR B/P1.

## Explicit non-goals

- Automated official-source sync in the Pages workflow (P1).
- NHI directory rows, geography/type aggregations, or PR #15 schema (P2).
- Changes to SQLite schema, MCP tool contracts, Docker, Kubernetes, or GKE.
- Medical advice, provider rankings, procurement recommendations, analytics,
  frontend frameworks, chart libraries, or CDN runtime dependencies.
