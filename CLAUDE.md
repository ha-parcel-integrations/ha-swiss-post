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

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
Where this repo diverges from it, that is recorded below under
*Divergences from the scaffold*.

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

**The history option means different things per source, deliberately.** On a
tracking hub `include_history` buys the event timeline from the second public
host and nothing else. On an account hub the timeline, the weight and the
dimensions all come from the *same* enrichment call, so one option
(`account_details`) buys all three and the options page shows only that one —
two switches would misrepresent what the extra requests pay for.

**No `awaiting_pickup` sensor yet — pending a status token seen on the wire,
not a structural exemption.** Swiss Post does have a pickup-point concept and
both normalisers derive a `pickup: bool`. On the public surface no
`globalStatus` value has been confirmed to map to
`ParcelStatus.AT_PICKUP_POINT` (pickup is inferred from
`deliveryPostOfficeZip`/`avis`/`displayedAvisCode` instead, one-shot WARNING in
place). The **account** surface does have the token — `WAITING_FOR_PICKUP`, read
from the app's own enum — and even names the office (`deliveryAddress.pickupOffice`),
but it has not been seen on a real parcel yet. Add the sensor once one shows up;
this is not the same as a locker-less carrier's structural exemption
(`.github/CONVENTIONS.md`'s pickup-point convention).

**Two hosts, each with half the data.** `service.post.ch/ekp-web` has status,
ETA, weight, dimensions and the delivery booleans but its `events` array is
always empty; `eosapi.postlogistics.ch` has the event timeline and no usable
status vocabulary. `tracking/api.py` merges the second into the first's `events` key, so
`tracking/parcels.py` only ever sees one payload shape.

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

**Deliberate `None`s in `tracking/parcels.normalize_parcel`:** `sender` (the payload has a
`sender` field but it has never been populated; `senderCountry` is a country,
not a sender) and `pickup_point` on anything but a pickup parcel (Swiss Post
gives the office's postcode, never its name).

**`delivered` comes from the payload's own boolean**, not from a status match —
it keeps working when an unmapped status token shows up. The status enum is
mapped from `globalStatus` only; the per-event `Status` on the timeline is
near-constant (`PST` on almost everything, including the delivery itself) and
mapping it would mis-file delivered parcels, so history entries keep
`status: null` on purpose.

**`dimension1/2/3` carry no axis semantics — sort them.** Swiss Post's own
tracking frontend does
`[d1, d2, d3].map(v => v / 10).sort((a, b) => a - b)` before labelling anything,
so the payload order is meaningless. `tracking/parcels._dimensions_cm()` sorts
too and labels largest → length, middle → width, smallest → height (the suite's
usual `length >= width`; Swiss Post's own frontend calls the largest *width*).
The old "we assume length, width, height" WARNING is gone — this is derived, not
guessed. Do not "simplify" the sort away.

**Pre-1.0 unknowns**, each with a one-shot WARNING and an issue link
(`tracking/parcels.py`): no pickup-point `globalStatus` token is known on the
public surface (pickup is inferred from `deliveryPostOfficeZip` / `avis`
instead), and no real delivery window has been seen yet. The warnings log field
*names*, never values — a pickup point or a delivery window is location data.

**`deliveryTimeWindow` is a delivery class, not a time range.** A plain
domestic letter carries `"STANDARD"` while `deliveryTimeInterval` stays null, so
truthiness-checking the field made every ordinary shipment announce itself as
the first delivery window ever observed (reported from the field, issue #2).
`_NO_WINDOW_VALUES` holds the values known to mean "no window"; anything else
still reports, because an evening- or express-class value would be worth seeing.

**Letter post has no timeline on surface B, and that is not an error.**
`eosapi.postlogistics.ch` answers `{"Data": null}` for letter codes — the same
shape as a request it did not understand. `async_get_history` takes an
`is_letter` flag (from `tracking/parcels.is_letter_shipment`, keyed on
`source`/`product`) that downgrades those two warnings to debug, so a user who
tracks letters is not told their integration is broken. The call is still made,
so letter events start flowing if Swiss Post ever serves them. Letters are
**not** filtered out of the tracking surface — only the account surface excludes
them.

## Two sources: `tracking/` and `account/`

The repo follows the suite's multi-source layout (as `ha-bpost` does). The
domain root holds only dispatch and the shared presentation layer; each source
package owns its client, coordinator and normaliser:

- `tracking/` — the public keyless surfaces (above). Entries created before the
  split carry no `CONF_SOURCE` and are treated as tracking hubs, so no options
  migration was needed.
- `account/` — the SwissID-authenticated app backend (`mobserv`). Discovers the
  logged-in user's parcels; no tracking codes.

`parcels.py` in the root holds only **source-agnostic** pure helpers
(`parse_iso`, `epoch_ms_to_iso`, `format_dimensions`, `sort_parcels_by_ts`,
`apply_delivered_filter`, `warn_once`) and `events.py` holds the one bus-event
contract both coordinators fire, so the two sources cannot drift apart on either.

**`single_config_entry` was deliberately removed** from the manifest: an account
hub and a tracking hub must be able to coexist.

**The two coordinators publish different shapes, on purpose.** Tracking returns
a plain active list plus a `delivered` attribute; the account inbox returns a
four-way dict (`incoming_active` / `incoming_delivered` / `outgoing_active` /
`outgoing_delivered`). `sensor._bucket()` hides that from the entities — read
buckets through it, never `coordinator.data` directly.

### Account-source traps

- **The bearer is the JWT `id_token`, not the opaque `access_token`.** The inbox
  rejects the access token as `"Malformed token"`. It also wants any
  `x-device-id` UUID (generated once per entry).
- **The `refresh_token` rotates on every refresh.** `__init__.py` persists the
  new pair to the config entry via a token callback — drop that and the chain is
  lost on the next restart.
- **A dead chain must raise `ConfigEntryAuthFailed` from inside
  `_async_update_data`.** It is the only exception `DataUpdateCoordinator` turns
  into HA's reauth flow; re-raising our own error gets swallowed into
  `ConfigEntryNotReady` and the entry retries a dead chain forever.
- **Letters are excluded** (`mailpieceType == "LETTER"`). Swiss Post has no
  envelope-scan feature like PostNL's, so a letter is just a thinner parcel —
  deliberately filtered rather than surfaced. An unknown type is kept.
- **The overview is a summary**: no weight, dimensions, ETA window or event
  timeline. The first three come from a per-parcel enrichment call behind the
  `account_details` option (see below); no ETA exists on either call.
- **`isComplete` is not `delivered`.** It means "this parcel is finished", and a
  **returned** parcel is finished too — mapping it to `delivered` filed parcels
  that went back to the sender as delivered. Only `mailpieceStatusType`
  separates the two, so it is the sole source for `delivered`. The side effect
  is deliberate: an unmapped status on a completed parcel now reads
  *undelivered* rather than guessing.
- **The enrichment call is the anonymous `search`, not the authenticated
  `detail`.** Both return the same `MailpieceTrackingDetail` record, but
  `search` is keyed on the tracking number the overview already gives us, has
  been seen answering a real `200`, and carries **no bearer** — so a failed
  enrichment can never cost the entry its token chain. `detail` needs the opaque
  `mailpieceKey` and has only ever been probed returning `404`. Keep
  `dontFollow=true`: without it every poll re-follows the parcel onto this
  device's list.
- **Weight and dimensions arrive as display strings** (`"1.14 kg"`,
  `"40.0 x 25.0 x 15.5 cm"`), the one place this backend is harder to read than
  the public surface. Whether they are localised under another
  `Accept-Language` is unprobed, so the parsers accept a decimal comma up front
  and report an unreadable value once (logging its *length*, not the value).
  The three dimensions are sorted before being labelled, exactly as on the
  public surface.
- **`eventType` is not a status vocabulary** — it read `OTHER` on every event of
  every parcel ever seen, including the delivery itself, so account history
  entries keep `status: null` like the public timeline's. Anything other than
  `OTHER` fires a one-shot report, because that *would* be mappable.
- **Event coordinates never leave `raw`.** Each enrichment event can carry a
  `location` with latitude/longitude, and the last event of a delivered parcel
  is the user's doorstep. `build_account_history` drops it, and
  `location`/`latitude`/`longitude` are in the diagnostics redaction set.
- **`statusEndTimestamp` is the only ETA candidate in the whole model** and has
  never been populated. It is *not* mapped to `planned_from`/`planned_to`: on a
  pickup parcel it could just as easily be the collection deadline. A populated
  one on an in-flight parcel fires a one-shot report instead.
- **The login is browser paste-back, and that is a feature.** SwissID registers
  one redirect URI, built to open the mobile app, so it bounces to a Post app
  page and the code never stays in the address bar — users copy it from the
  network log (`docs/finding-the-redirect-url.md`, mirroring DHL DE). Replicating
  SwissID's *native* `api-login` journey was considered and rejected: it only
  works at `qoa1` (password), so anyone with a passkey or enforced 2FA would have
  to weaken their account security. Do not reintroduce it.

## Divergences from the scaffold

Everything not listed here follows the scaffold exactly.

*Dynamic polling* — unlike most account-less carriers, Swiss Post **does**
populate `planned_from` even for a same-day `out_for_delivery` parcel:
`calculatedDeliveryDate` is a day-level estimate every parcel carries. The
hot/mid split therefore behaves as designed rather than always taking the
"no `planned_from`" branch.

## Running tests

```
python -m pytest tests/ --cov=custom_components.swiss_post
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file in the same commit;
the API reference lives in the private `carrier-research/swiss-post/api/`,
not in this repo.
