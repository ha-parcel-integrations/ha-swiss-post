# Swiss Post Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-swiss-post.svg)](https://github.com/ha-parcel-integrations/ha-swiss-post/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Swiss Post](https://www.post.ch) parcels in Switzerland. No account and no API key are needed — you enter the tracking code yourself, just like on the Swiss Post website.

> **Parcels from abroad.** Cross-border parcels are customs-cleared and often handed over by a foreign carrier, so Swiss Post only sees them from the moment they enter its network. Such a parcel can stay `unknown` for a while and then appear mid-journey — that is Swiss Post's view of it, not a fault in the integration.

Part of the [ha-parcel-integrations](https://github.com/ha-parcel-integrations) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Options](#options)
- [Dynamic polling](#dynamic-polling)
- [Removal](#removal)
- [Sensors](#sensors)
- [Parcel status reference](#parcel-status-reference)
- [Events](#events)
- [Services](#services)
- [Examples](#examples)
- [Debugging](#debugging)
- [Troubleshooting](#troubleshooting)
- [Related integrations](#related-integrations)
- [Disclaimer](#disclaimer)
- [Contributing](#contributing)
- [License](#license)

## Features

- Track any number of Swiss Post parcels by tracking code — no account needed
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, the expected delivery window and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels
- Read-only **Deliveries** calendar with the expected delivery windows
- `swiss_post.track_parcel` / `swiss_post.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

- A Swiss Post parcel and its tracking code (from the shipping
  confirmation email or the missed-delivery card) — no account needed
- Tracking codes come in two shapes, both accepted: the 18-digit domestic
  number (printed as `99.00 1234.5678 9012 34`) and the international form
  `RR123456789CH`

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-swiss-post` as an **Integration**.
3. Install **Swiss Post** and restart Home Assistant.

### Manual

Copy `custom_components/swiss_post` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Swiss Post**. There is nothing to fill in: the hub is created immediately (Swiss Post tracking needs no account).

Then add parcels via the integration's **Configure** dialog, the [`swiss_post.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. Swiss Post serves the timeline from a second endpoint, so this costs one extra request per parcel per poll. |

## Dynamic polling

Instead of polling Swiss Post at the same rate around the clock, the
integration adjusts its own cadence to what your tracked parcels are actually
doing:

- **Quiet hours** — no polling between 00:00–06:00 local time, aside from one
  catch-up check at each end of that window (around midnight and around 6
  AM).
- **Hot (every 15 minutes)** — as soon as a tracked parcel is
  `out_for_delivery`, starting an hour before its expected delivery time (or
  immediately if no time is known).
- **Mid (every 45 minutes)** — any other in-progress parcel.
- **Fully stopped** — nothing is tracked, or every tracked parcel has been
  delivered. Adding a parcel back (via the options dialog, the
  `swiss_post.track_parcel` service, or a dashboard button) resumes polling
  immediately.
- A small, fixed per-hub offset is added on top, so not every Swiss Post hub
  out there polls at exactly the same second.

This is not user-configurable — it is the only polling behaviour this
integration has.

## Removal

Standard HA removal applies: **Settings → Devices & Services → Swiss Post → ⋮ → Delete**. Nothing is stored on Swiss Post's side.

## Sensors

| Entity | Description |
|---|---|
| `sensor.swiss_post_incoming_parcels` | Number of active tracked parcels, full list under the `parcels` attribute |
| `sensor.swiss_post_parcel_<code>` | One per tracked parcel; state is the canonical status, attributes carry the full normalised parcel |
| `sensor.swiss_post_next_delivery` | Earliest expected delivery moment across all active parcels |
| `sensor.swiss_post_delivered_parcels` | Recently delivered parcels (see the retention option) |
| `sensor.swiss_post_last_successful_update` | Diagnostic: when Swiss Post was last polled successfully |

A delivered parcel moves from its per-parcel sensor to the delivered sensor automatically.

A **Deliveries** calendar entity is also created, showing expected delivery windows for active parcels — read-only, no extra API calls.

A **Refresh** button entity forces an immediate poll, without waiting for the next scheduled interval.

## Parcel status reference

The `status` field is the carrier-agnostic enum shared by the whole integration family:

| Status | Meaning | Swiss Post reports it as |
|---|---|---|
| `registered` | Announced / received by Swiss Post | `REGISTERED` |
| `in_transit` | In the sorting network, or clearing customs | `TO_BE_DELIVERED`, `CUSTOMS` |
| `out_for_delivery` | With the courier today | `IN_DELIVERY` |
| `at_pickup_point` | Waiting for you at a post office | *not known yet — see below* |
| `delivered` | Delivered | `DELIVERED` |
| `returning` | Going back to the sender | `RETURNED` |
| `problem` | Swiss Post reports an exception | `MISSED_DELIVERY`, `NOT_DELIVERED` |
| `unknown` | Not yet scanned, or a status we have not mapped yet | anything else |

Swiss Post's own status code is always available as `raw_status`.

**A parcel waiting at a post office will most likely show as `unknown` for
now.** Swiss Post clearly has the concept, but the exact value it reports has
never been seen in live data, so it is not in the table above. The integration
detects the situation from the post-office fields — `pickup` still becomes
`true` — and logs a warning asking you to report it. That is the fastest way to
get `at_pickup_point` mapped properly; see [Contributing](#contributing).

## Events

The integration fires these on the event bus (also available as device triggers on the Swiss Post device):

| Event | When |
|---|---|
| `swiss_post_parcel_registered` | A new parcel appears in the active list |
| `swiss_post_parcel_status_changed` | A parcel's canonical status changes (`old_status` / `new_status` in the payload), except the final hop to delivered |
| `swiss_post_parcel_delivered` | A parcel is delivered |
| `swiss_post_parcel_delivery_time_changed` | The expected delivery window changes |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

## Services

| Service | Fields | Description |
|---|---|---|
| `swiss_post.track_parcel` | `tracking_code` | Start tracking a parcel |
| `swiss_post.untrack_parcel` | `tracking_code` | Stop tracking a parcel |

## Examples

Ready-to-paste automations and dashboard snippets live in [`examples/`](examples/), including tracking a new parcel straight from a dashboard.

### Community Lovelace cards

Third-party cards that work with this integration's sensors:

- [jonisnet/hki-parcels-card](https://github.com/jonisnet/hki-parcels-card)
- [klaptafel/ha-package-tracker-card](https://github.com/klaptafel/ha-package-tracker-card)

## Debugging

```yaml
logger:
  logs:
    custom_components.swiss_post: debug
```

## Troubleshooting

- **A parcel shows `unknown`** — Swiss Post has not scanned it yet (their API returns nothing at all until the first scan), the parcel is still with a foreign carrier, or the code is wrong. It fills in automatically once Swiss Post picks it up.
- **"Swiss Post has no data for tracking code …"** — the same thing, said in the log. The parcel stays tracked; nothing needs doing unless the code is a typo.
- **A status logs "Unrecognised Swiss Post status"**, or any of the other warnings asking you to report something — please [open an issue](https://github.com/ha-parcel-integrations/ha-swiss-post/issues/new) with the logged line. This integration is still below 1.0: the status list, the delivery-window fields and the parcel-dimension order were all confirmed against a single real parcel, and every gap is deliberately noisy so it gets fixed.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://github.com/ha-parcel-integrations) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://github.com/ha-parcel-integrations) for the current list of supported carriers.

## Disclaimer

This integration uses the same public tracking endpoints as the Swiss Post consumer website and the Post Logistics tracking page. It is not affiliated with, endorsed by, or supported by Swiss Post.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
