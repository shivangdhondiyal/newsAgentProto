# Frontend boundary

The dashboard is a static, dependency-free client served by FastAPI at
`/dashboard`. It consumes only the versioned `/api/v1` API and never imports
backend modules. Source files:

- `index.html` — shell, six view panels, and stable element IDs.
- `styles.css` — design tokens and component styles.
- `app.js` — hash router, API wrapper, rendering, polling, and review actions.

## Routes

| Hash | Workspace |
| --- | --- |
| `#/overview` | Command center: next action, production activity, metrics, pipeline, attention, services, recent runs |
| `#/production` | Video studio: story and script, style/format, assets, narration, render progress, scenes, variants, logs |
| `#/review` | Review queue: overview, evidence, script, scenes, and decision tabs |
| `#/runs` | Agent runs and manual stage triggers |
| `#/publishing` | Publishing records and queue processing |
| `#/articles` | Collected source library |

Unknown hashes fall back to `#/review`.

## Design tokens

All color, type, spacing, radius, shadow, control-height, and motion values live
as custom properties in `:root` at the top of `styles.css`. The palette is one
low-saturation charcoal family with a restrained slate-blue accent and muted
status colors. Status is never conveyed by color alone: badges and attention
items include text, and stage cards include status labels.

Only a dark theme is implemented. `color-scheme: dark` and a matching
`theme-color` are declared; there is no theme switcher.

## Contracts to preserve

- Element IDs and `data-*` hooks are used by `app.js` and dashboard tests.
- The global pipeline action is `#pipeline-button` / `#pipeline-label`, now in
  the Overview next-action band rather than the topbar.
- Overview activity uses `/api/v1/system/overview` and
  `/api/v1/production/jobs?limit=6`.
- Production list summaries do not include stage data, so the UI shows honest
  status text instead of invented progress. Stage-based progress appears only
  when stage data is present.
- Human approval is mandatory before publishing. The UI never presents
  publishing as automatic.

## Local checks

```powershell
.tools/node/node.exe --check frontend/app.js
.venv\Scripts\python.exe -m pytest tests/test_dashboard.py -q
```

The dashboard is for local or single-operator use behind a trusted network
boundary; it has no built-in authentication.
