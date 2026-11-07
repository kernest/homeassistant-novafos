# NovaFos token renewer

A small Docker service that keeps the NovaFos integration supplied with KMD
access tokens, replacing the Chrome extension.

KMD Easy-Energy issues access tokens that last 60 minutes, offers no refresh
tokens, and protects its login form with reCAPTCHA. This service keeps one
Chromium profile:

1. **Once:** you log in to the portal through noVNC in your own browser and
   solve the reCAPTCHA yourself. The service records the portal's login
   request and captures the token.
2. **Every few hours:** it replays that login request in the same profile.
   While KMD's login session is valid, KMD answers without showing the login
   form. The new token goes to Home Assistant's `novafos.update_token` action,
   which triggers a data refresh.
3. **When KMD's session expires:** the service selects email login and submits
   if Chromium has autofilled both saved fields. If KMD explicitly asks to try
   again, it retries once. After login it opens **Forbrug** when the portal
   lands on **Mine oplysninger**, so the API token is actually issued.
   If credentials are missing,
   login fails, or a CAPTCHA needs attention, it creates a Home Assistant
   notification with the noVNC link and waits for you.

It never reads credentials out of the browser profile, supplies credentials, or
interacts with reCAPTCHA. Save your login in Chromium through noVNC first if
you want automatic email login. How long a KMD login
session lasts, and so how often you need to log in again, can only be found by
running it.

## Setup

1. Create a Home Assistant long-lived access token (your profile → Security).
2. Find your login URL: on novafos.dk go to Selvbetjening → Mit forbrug,
   choose your municipality, and copy the address of the KMD portal page you
   land on.
3. Put the secrets in `.env` next to `docker-compose.yml`:

   ```
   HA_TOKEN=<long-lived access token>
   VNC_PASSWORD=<password for the noVNC page>
   ```

4. Set `NOVAFOS_LOGIN_URL`, `HA_URL` and `NOVNC_URL` in `docker-compose.yml`,
   then start it:

   ```bash
   docker compose up -d --build
   ```

5. Open `http://<docker-host>:6080/vnc.html`, enter the VNC password, and log
   in to the portal. Home Assistant receives the first token within seconds.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `NOVAFOS_LOGIN_URL` | required | Portal page to open for a human login |
| `HA_URL` | required | Home Assistant base URL, for example `http://homeassistant.local:8123` |
| `HA_TOKEN` | required | Home Assistant long-lived access token |
| `VNC_PASSWORD` | required | Password for the noVNC page |
| `NOVNC_URL` | | Link shown in the Home Assistant notification |
| `RENEW_INTERVAL_MINUTES` | `180` | Time between renewals. Each token triggers a 31-day refresh of about 60 KMD requests, so more often than hourly is rarely useful |
| `AUTO_LOGIN_SAVED_CREDENTIALS` | `true` | Submit browser-autofilled email login, with one explicit KMD-requested retry; set `false` for manual login only |
| `BROWSER_CDP_URL` | empty | Attach to an already running Chrome over DevTools (e.g. `http://127.0.0.1:9222`) instead of launching Playwright's Chromium; see below |
| `RENEW_AT` | | Fixed daily renewal time in local time, for example `06:00`. Overrides `RENEW_INTERVAL_MINUTES`. A KMD session that expires within a day then needs a new login each day |
| `TZ` | | Must match Home Assistant's time zone; the integration checks the token timestamp against its local clock |

## Using a separately started Chrome

KMD has rejected logins from Playwright's bundled Chromium and from Chrome run
with `--no-sandbox`. An ordinary Google Chrome with its sandbox intact is
accepted. To use one, start Chrome yourself (on a virtual display served by
noVNC) with its own profile and DevTools bound to loopback only, for example:

    google-chrome --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
        --user-data-dir=/var/lib/novafos-browser/profile --no-first-run

and run `renew.py` on the same host with `BROWSER_CDP_URL=http://127.0.0.1:9222`.
The renewer then uses Chrome's first browser context, so Chrome's own saved
login and cookies apply. Never expose the DevTools port beyond loopback: it
gives full control of the logged-in browser.

## Security

- The `data/` volume holds a browser profile that is logged in to your KMD
  account. Treat it like a password.
- noVNC shows that logged-in browser. Keep port 6080 on your local network,
  or behind a VPN, and use a strong `VNC_PASSWORD`.
- `HA_TOKEN` has full access to Home Assistant. Consider a dedicated Home
  Assistant user for it.
- Tokens are only sent to `HA_URL`, and are never logged.
