# Working in this repository

Home Assistant custom integration for **Swiss Post** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

API mechanics (both endpoints, the handshake, the payload map, the status
vocabulary, the traps) live in `carrier-research/swiss-post/api/` in the private
research repo — **not** here and not in a local `docs/api/`. What follows is
integration-side only.

**Two hosts, each with half the data.** `service.post.ch/ekp-web` has status,
ETA, weight, dimensions and the delivery booleans but its `events` array is
always empty; `eosapi.postlogistics.ch` has the event timeline and no usable
status vocabulary. `api.py` merges the second into the first's `events` key, so
`parcels.py` only ever sees one payload shape.

**The history option controls the call count, not just an attribute.** Two
requests per parcel per poll with history off, three with it on. That is why
`include_history` is passed down into `async_get_parcel` instead of being
applied in `normalize_parcel` — do not "simplify" it back.

**The anonymous session is the only stateful code here** and the only place the
integration can silently rot:

- It needs a **dedicated `aiohttp` session** (`async_create_clientsession`, not
  `async_get_clientsession`) — the ekp-web cookie must not land in HA's shared
  cookie jar.
- A lookup is POST-then-GET. The GET's hash is just `sha256(tracking_number)`,
  so caching it and skipping the POST looks free. **It is not:** without the
  POST the GET answers `200 []`, which is indistinguishable from an unknown
  parcel, and every parcel silently reports as missing forever. The POST is
  what registers the number in the session.
- A `403` means the session died; it is re-established once and the lookup
  retried. `test_api.py` covers both 403 paths — keep them.

**`[]` is "not found", never "gone".** It returns `None`, which the coordinator
turns into the cached payload or a pending placeholder, so a parcel never
silently disappears or flips to delivered.

**Deliberate `None`s in `normalize_parcel`:** `sender` (the payload has a
`sender` field but it has never been populated; `senderCountry` is a country,
not a sender) and `pickup_point` on anything but a pickup parcel (Swiss Post
gives the office's postcode, never its name).

**`delivered` comes from the payload's own boolean**, not from a status match —
it keeps working when an unmapped status token shows up. The status enum is
mapped from `globalStatus` only; the per-event `Status` on the timeline is
near-constant (`PST` on almost everything, including the delivery itself) and
mapping it would mis-file delivered parcels, so history entries keep
`status: null` on purpose.

**Pre-1.0 unknowns**, each with a one-shot WARNING and an issue link
(`parcels.py`): no pickup-point `globalStatus` token is known (pickup is
inferred from `deliveryPostOfficeZip` / `avis` instead), `deliveryRange` /
`deliveryTimeWindow` have never been seen populated, and the
`dimension1/2/3` → length/width/height order is assumed. The warnings log field
*names*, never values — a pickup point or a delivery window is location data.

## Options and reloads

The options flow is one sectioned form (`data_entry_flow.section`); changes apply
without a restart. Two models, **do not mix them**:
- **Account-less carriers** (the default, and what this repo is) apply changes
  live: an update listener calls `async_request_refresh()`, so added/removed
  parcel sensors appear immediately. This is also the resume path after
  dynamic polling has fully suspended (see below) — adding a parcel back
  triggers the same refresh, which re-arms scheduling.
- **Account-based carriers** call `async_schedule_reload` on submit and register
  **no** update listener. Combining a listener with a reload-on-update flow is
  deprecated, an error in HA 2026.12+.

## Polling

Polling is dynamic and status-driven, unconditionally — there is no
user-facing interval option. The coordinator recomputes its own cadence at the
end of every refresh: a quiet window (00:00–06:00 local, with catch-up anchors
at each end), a 15-minute hot tier when a tracked parcel is
`out_for_delivery` (immediately, or from an hour before `planned_from`), a
45-minute mid tier otherwise, and a full stop (`update_interval = None`) when
nothing is tracked or everything tracked is delivered. Swiss Post's
`planned_from` is normally populated even for a same-day `out_for_delivery`
parcel — `calculatedDeliveryDate` is a day-level estimate every parcel
carries, so the hot/mid split behaves as designed rather than always landing
on the "no `planned_from`" branch. See `coordinator.py`'s
`_hottest_tier_minutes` / `_next_update_interval`.

## Module layout

| File | Carrier-specific? |
|---|---|
| `api.py` (HTTP client, error types) | **yes** |
| `const.py` (domain, URLs, `ParcelStatus`, option keys) | partly (URLs) |
| `parcels.py` (status map, `normalize_parcel`, history, sort, filters — pure, no I/O) | partly (`_STATUS_MAP`, `normalize_parcel`) |
| `coordinator.py` (fetch, cache, event firing) | mostly not |
| `config_flow.py` | partly (code validation) |
| `sensor.py` / `button.py` / `calendar.py` / `device_trigger.py` | no |
| `diagnostics.py` | partly (`TO_REDACT`) |
| `services.py` (`track_parcel` / `untrack_parcel`, account-less only) | no |

`parcels.py` is deliberately free of I/O and HA objects so the per-carrier part
stays unit-testable without Home Assistant. Config: `ConfigEntry.runtime_data`
(typed, no `hass.data`), `PARALLEL_UPDATES = 0`, coordinator takes
`config_entry=entry`. `aiohttp.ClientError` is caught **per parcel** in the gather
loop (one bad parcel doesn't fail the poll) but **not** around the whole update
(the coordinator wraps that). Entities: `has_entity_name` + `translation_key`,
`icons.json`, translated units, `_attr_attribution`, `_unrecorded_attributes` on
anything with a parcel list or `raw`. Over-redact diagnostics — they get pasted
into public issues.

## Running tests

```
python -m pytest tests/ --cov=custom_components.swiss_post
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file in the same commit;
the API reference lives in the private `carrier-research/swiss-post/api/`,
not in this repo.
