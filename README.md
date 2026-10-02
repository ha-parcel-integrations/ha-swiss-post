# Swiss Post Parcel Tracker

[![Release](https://img.shields.io/github/v/release/ha-parcel-integrations/ha-swiss-post.svg)](https://github.com/ha-parcel-integrations/ha-swiss-post/releases)
[![Downloads](https://img.shields.io/github/downloads/ha-parcel-integrations/ha-swiss-post/total.svg)](https://github.com/ha-parcel-integrations/ha-swiss-post/releases)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> 💬 Questions or feedback? Join the discussion on the [Home Assistant community](https://community.home-assistant.io/t/packages-postnl-dhl-nl-dpd-and-gls-parcel-integration/112433/).

A custom Home Assistant integration that tracks your [Swiss Post](https://www.post.ch) parcels in Switzerland. No API key is needed. Pick either of two sources at setup:

- **Tracking codes** — you enter each tracking code yourself, just like on the Swiss Post website. No account at all.
- **SwissID account** — sign in once and your parcels are imported automatically, incoming and outgoing, the same way the official Post app sees them.

You can run both side by side (one hub each) if you want codes for parcels that aren't linked to your account.

> **Parcels from abroad.** Cross-border parcels are customs-cleared and often handed over by a foreign carrier, so Swiss Post only sees them from the moment they enter its network. Such a parcel can stay `unknown` for a while and then appear mid-journey — that is Swiss Post's view of it, not a fault in the integration.

Part of the [ha-parcel-integrations](https://ha-parcel-integrations.github.io/) family: it publishes the same canonical parcel format, statuses and events as the other carrier integrations, so it plugs straight into the [Parcel Aggregator](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) and cross-carrier automations.

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

- Two sources: track any number of parcels **by tracking code** (no account), or sign in with **SwissID** and have your parcels imported automatically
- Per-parcel sensor with the canonical status (`registered` / `in_transit` / `out_for_delivery` / `delivered` / …), the carrier's own status text, the expected delivery window and a tracking deep-link
- Summary sensors: incoming parcels, next delivery, recently delivered parcels — plus outgoing and outgoing-delivered on an account hub
- Read-only **Deliveries** calendar with the expected delivery windows
- `swiss_post.track_parcel` / `swiss_post.untrack_parcel` services, so a dashboard button can add a parcel
- Events + device triggers for no-code automations (parcel registered, status changed, delivered, delivery time changed, and the outgoing equivalents)
- Opt-in per-parcel status history
- Manual refresh button and a diagnostic last-update sensor

## Requirements

For the **tracking-code** source:

- A Swiss Post parcel and its tracking code (from the shipping
  confirmation email or the missed-delivery card) — no account needed
- Tracking codes come in two shapes, both accepted: the 18-digit domestic
  number (printed as `99.00 1234.5678 9012 34`) and the international form
  `RR123456789CH`

For the **SwissID account** source:

- A SwissID account with your parcels linked to it (the same account you use in
  the Post app)
- A desktop browser to complete the sign-in once. Swiss Post's final redirect is
  built to open its mobile app, so the address Home Assistant needs never stays
  in the address bar — you copy it out of the browser's network log. See
  [finding the redirect URL](docs/finding-the-redirect-url.md).
- Whatever sign-in factors you already use (password, passkey, fingerprint, SMS
  code) keep working — the sign-in happens on SwissID's own pages, so you never
  have to weaken your account security for this integration

## Installation

### HACS (recommended)

1. In HACS, choose the three-dot menu → **Custom repositories**.
2. Add `https://github.com/ha-parcel-integrations/ha-swiss-post` as an **Integration**.
3. Install **Swiss Post** and restart Home Assistant.

### Manual

Copy `custom_components/swiss_post` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration → Swiss Post**, then pick a source.

### Tracking codes

There is nothing to fill in: the hub is created immediately (this source needs no account).

Then add parcels via the integration's **Configure** dialog, the [`swiss_post.track_parcel`](#services) service, or a [dashboard button](examples/dashboards/add_parcel_card.yaml). The tracking code is on your shipping confirmation email or the missed-delivery card.

### SwissID account

1. The form shows a sign-in link. Open it in a desktop browser and sign in with SwissID. If SwissID offers to create a passkey you can decline it.
2. Your browser will bounce on to a Swiss Post page advertising the mobile app. That is expected — the sign-in worked, but the address carrying the code is already gone from the address bar.
3. Copy that address out of your browser's developer tools' **Network** tab and paste it into the form. Step-by-step, per browser: [finding the redirect URL](docs/finding-the-redirect-url.md).

Parcels are then imported automatically — nothing to add by hand. Home Assistant keeps the session alive on its own; if it ever expires you get a normal "re-authenticate" prompt.

> **Why the copy-paste?** Swiss Post registers only one redirect address, and it is built to open its own mobile app. Doing the sign-in in your own browser is also what lets your existing passkey, fingerprint or SMS code keep working — this integration never asks you to turn a security feature off.

## Options

Open **Configure** on the integration entry:

| Section | Option | Default | Description |
|---|---|---|---|
| Parcels | Add / remove | — | Manage the tracked tracking codes. Changes apply immediately, no restart. **Tracking-code hubs only** — an account hub discovers its own parcels. |
| Delivered parcels | Filter by / amount | last 7 days | How long delivered parcels stay visible on the delivered sensor. |
| Parcel history | Include status history | off | Adds a `history` attribute per parcel with each status update. Swiss Post serves the timeline from a second endpoint, so this costs one extra request per parcel per poll. Registered **letters** are tracked normally but have no timeline on that endpoint, so their `history` stays empty. |

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
| `sensor.swiss_post_outgoing_parcels` | **Account hubs only:** parcels you sent that are still on their way |
| `sensor.swiss_post_outgoing_delivered_parcels` | **Account hubs only:** parcels you sent that have arrived (same retention option) |
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
| `swiss_post_outgoing_parcel_status_changed` | **Account hubs only:** a parcel you sent changes status |
| `swiss_post_outgoing_parcel_delivered` | **Account hubs only:** a parcel you sent is delivered |

Every payload is the full normalised parcel plus the hub's `device_id`. Events are suppressed on the first refresh after start-up.

Outgoing parcels deliberately get a smaller event set: a parcel you sent yourself is not news when it first appears, and its delivery window is the recipient's business.

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
- **A status logs "Unrecognised Swiss Post status"**, or any of the other warnings asking you to report something — please [open an issue](https://github.com/ha-parcel-integrations/ha-swiss-post/issues/new) with the logged line. This integration is still below 1.0: the status list and the delivery-window fields were confirmed against a small number of real parcels, and every gap is deliberately noisy so it gets fixed.

## Related integrations

This integration is part of [**ha-parcel-integrations**](https://ha-parcel-integrations.github.io/) — a family of
parcel-carrier integrations that all publish the same canonical parcel format,
statuses and events.

- [**Parcel Aggregator**](https://github.com/ha-parcel-integrations/ha-parcel-aggregator) rolls every installed carrier
  up into one set of sensors.
- Browse [the organisation](https://ha-parcel-integrations.github.io/) for the current list of supported carriers.

## Disclaimer

This is an independent, community-built project. It is not affiliated with, endorsed by, sponsored by, or supported by Swiss Post, Home Assistant, or any other third party referenced in this project. Please don't contact Swiss Post for support with this integration.

All third-party trademarks, trade names, product names, logos, and other brand assets are the property of their respective owners. References to them are solely to identify the relevant carrier or service and do not imply affiliation, sponsorship, or endorsement. Nothing in this project grants or implies any licence or right to use third-party brand assets.

This integration may rely on public, unofficial, or undocumented carrier interfaces, accessed with your own account or API key where required. These may change or be withdrawn without notice and may be subject to Swiss Post's terms. Data is sent only to Swiss Post's own services or those of its group; this project operates no servers of its own. You are responsible for ensuring that your use complies with applicable law and those terms. Use is at your own risk; see the [licence](LICENSE) for warranty limitations.

This integration uses the same public tracking endpoints as the Swiss Post consumer website and the Post Logistics tracking page.

## Contributing

Pull requests and issues are welcome. Please open an issue before
submitting a large change.

## License

[MIT](LICENSE)
