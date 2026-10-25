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
3. **When KMD's session expires:** the login form would be needed again. The
   service creates a Home Assistant notification with the noVNC link and
   waits for you to log in.

It never enters your credentials or solves a reCAPTCHA. How long a KMD login
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
| `TZ` | | Must match Home Assistant's time zone; the integration checks the token timestamp against its local clock |

## Security

- The `data/` volume holds a browser profile that is logged in to your KMD
  account. Treat it like a password.
- noVNC shows that logged-in browser. Keep port 6080 on your local network,
  or behind a VPN, and use a strong `VNC_PASSWORD`.
- `HA_TOKEN` has full access to Home Assistant. Consider a dedicated Home
  Assistant user for it.
- Tokens are only sent to `HA_URL`, and are never logged.
