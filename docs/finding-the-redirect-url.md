# Finding the Swiss Post redirect URL

During setup of the **SwissID account** source, SwissID finishes signing you in
by redirecting your browser to an address that starts with
`https://app.post.ch/mainapp/auth/callback?code=…`. Home Assistant needs that
address.

The catch: that page is meant to open the Swiss Post mobile app, not to be
viewed in a browser. In a desktop browser it loads for a split second and then
**immediately redirects you on to a Swiss Post page advertising the Post app**
("Post-App für Smartphones" / "Die Post App"). So unlike a sign-in that visibly
fails, this one *looks like it worked* — you just end up on the wrong page, and
the address with the `code=` in it is already gone from the address bar.

It is never possible to copy it from the address bar. You have to catch it in
your browser's developer tools' **Network** tab before the redirect happens.

This only has to be done once per login — afterwards Home Assistant keeps
itself signed in and renews the session on its own.

Two things you may see on the way, both harmless:

- **"Simplify your log in — with passkeys you can use your fingerprint, face
  recognition or screen lock to log in."** This is SwissID offering to *create*
  a passkey. You can decline it; you do not need a passkey for this
  integration, and you do not have to change any security setting on your
  SwissID account.
- Whatever factors you already have enabled (password, passkey, SMS code) work
  normally here, because the sign-in happens on SwissID's own pages.

The general idea is the same in every browser:

1. Open your browser's developer tools and switch to the **Network** tab
   *before* you finish signing in.
2. Turn on **"preserve log"** (the exact wording differs per browser — see
   below) so the request isn't cleared when the page redirects. **This is the
   important step** — without it the redirect wipes the log and the address is
   gone.
3. Complete the SwissID sign-in normally.
4. Once you land on the Post app page, look through the Network tab's request
   list for one whose URL contains **`auth/callback`** (it will show as a
   `302`).
5. Copy that request's **full URL** — the complete address including
   everything after `?code=`.
6. Paste that whole address into the Home Assistant setup form.

The sections below show exactly where to click for each browser.

## Google Chrome

1. Press `F12` (Windows/Linux) or `Cmd+Option+I` (Mac) to open DevTools, or
   right-click anywhere on the page → **Inspect**.
2. Click the **Network** tab at the top of the DevTools panel.
3. Check the **"Preserve log"** checkbox, near the top of the Network panel.
4. Go back to the page and complete the SwissID sign-in.
5. Type `callback` into the **Filter** box above the request list to find it
   instantly.
6. Click that request to select it. In the panel that opens on the right, the
   **Headers** tab shows a **General** section with **Request URL** — that is
   the full address you need. Right-click the request → **Copy** → **Copy link
   address** also works.

## Microsoft Edge

Edge uses the same Chromium DevTools as Chrome, so the steps are identical:

1. Press `F12` or right-click → **Inspect**.
2. Open the **Network** tab.
3. Enable **"Preserve log"**.
4. Complete the SwissID sign-in.
5. Filter for `callback` in the request list, click the matching request.
6. Under **Headers → General → Request URL**, copy the full address.

## Mozilla Firefox

1. Press `F12` or right-click → **Inspect**.
2. Click the **Network** tab.
3. Click the **gear icon** (⚙) in the Network panel and enable
   **"Persist Logs"** — this is Firefox's equivalent of "preserve log".
4. Complete the SwissID sign-in.
5. Type `callback` into the Network panel's filter box to find the request.
6. Right-click it → **Copy Value** → **Copy URL**, or click it and read the
   **URL** at the top of the **Headers** pane.

## Safari

Safari's developer tools are hidden by default:

1. Open **Safari → Settings → Advanced** and turn on
   **"Show features for web developers"**.
2. Open the **Develop** menu (in the menu bar) → **Show Web Inspector**, or
   press `Cmd+Option+I`.
3. Click the **Network** tab in the Web Inspector.
4. Enable the **"Preserve Log"** button/checkbox in the Network tab's toolbar.
5. Complete the SwissID sign-in.
6. Search for `callback` in the filter field at the top of the Network tab.
7. Click the request and copy the full request URL from the **Headers**
   section.

## Mobile browsers (Chrome/Safari on a phone)

Mobile browsers don't have on-device developer tools with a Network tab, and on
a phone the callback address will simply open the Swiss Post app instead.
Complete the sign-in on a **desktop/laptop browser** using the steps above —
the redirect URL isn't tied to a device, only to that one sign-in attempt.

## Troubleshooting

- **I don't see any request containing `auth/callback`.** Make sure "Preserve
  log"/"Persist Logs" was turned on *before* you submitted the sign-in form.
  The redirect to the Post app page counts as a page navigation, which clears
  the network log — so without that option the request is gone before you can
  look for it.
- **The address I copied gets rejected by Home Assistant.** Paste the request
  URL exactly as shown, including the full query string (`?code=…&state=…`).
  The code is single-use and short-lived — if more than a couple of minutes
  have passed, or if you already submitted it once, redo the sign-in and copy
  the new address rather than retrying the old one.
- **Home Assistant says the sign-in was for a different account.** When
  renewing an expired sign-in, SwissID has to be the same account the entry was
  originally set up with. Sign out of SwissID in that browser first, then
  repeat with the right account.
- **I ended up on a page asking about a passkey or my fingerprint.** That is
  the optional passkey offer described above. Decline it (or complete it if you
  want a passkey for yourself) — either way the sign-in then continues to the
  callback address you need.
