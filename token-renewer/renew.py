"""Keep the NovaFos integration supplied with KMD access tokens.

KMD Easy-Energy issues 60 minute access tokens and no refresh tokens, and its
login form is protected by reCAPTCHA.  This service keeps one Chromium
profile.  A person logs in once through noVNC (solving any reCAPTCHA
themselves).  Afterwards the service replays the portal's own OIDC
authorization request in that profile: while KMD's login session is valid the
identity server answers without showing the login form, and the new token is
sent to Home Assistant's novafos.update_token action.

When the session has expired the login form is shown instead.  The service
then leaves the portal open in noVNC, notifies Home Assistant, and waits for
the next human login.  It never enters credentials itself.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.async_api import BrowserContext, async_playwright

IDENTITY = os.environ.get("KMD_IDENTITY_URL", "https://easy-energy-identity.kmd.dk")
AUTHORIZE_URL = f"{IDENTITY}/oidc/authorize"
TOKEN_URL = f"{IDENTITY}/oidc/token"

LOGIN_URL = os.environ["NOVAFOS_LOGIN_URL"]
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
NOVNC_URL = os.environ.get("NOVNC_URL", "the noVNC page of the token renewer")
INTERVAL = int(os.environ.get("RENEW_INTERVAL_MINUTES", "180")) * 60
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "profile"
# Parameters of the portal's own authorization request, recorded from the
# last human login.  Per-request values are regenerated on every renewal.
AUTHORIZE_FILE = DATA_DIR / "authorize.json"
PER_REQUEST = {"state", "nonce", "code_challenge", "code_challenge_method"}

NOTIFICATION_ID = "novafos_token_renewer"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger("novafos-token-renewer")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Renewer:
    """Owns the browser profile and the token flow."""

    def __init__(self, context: BrowserContext, ha) -> None:
        self.context = context
        self.ha = ha
        self.tokens: asyncio.Queue[str] = asyncio.Queue()
        context.on("request", self._on_request)
        context.on("response", self._on_response)

    # -- Observing the portal during a human login -------------------------

    def _on_request(self, request) -> None:
        """Record the portal's authorization request parameters."""
        if not request.url.startswith(AUTHORIZE_URL):
            return
        params = {
            key: values[0]
            for key, values in parse_qs(urlsplit(request.url).query).items()
            if key not in PER_REQUEST
        }
        if "client_id" in params and "redirect_uri" in params:
            AUTHORIZE_FILE.write_text(json.dumps(params, indent=2))

    async def _on_response(self, response) -> None:
        """Capture tokens the portal obtains after a human login."""
        if not response.url.startswith(TOKEN_URL) or response.status != 200:
            return
        try:
            token = (await response.json()).get("access_token")
        except Exception:  # noqa: BLE001 - not JSON, not ours
            return
        if token:
            await self.tokens.put(token)

    # -- Silent renewal ------------------------------------------------------

    async def renew_silently(self) -> str | None:
        """Return a new token if KMD's login session is still valid."""
        if not AUTHORIZE_FILE.exists():
            return None
        params = json.loads(AUTHORIZE_FILE.read_text())
        redirect_uri = params["redirect_uri"]
        verifier = _b64url(secrets.token_bytes(48))
        state = _b64url(secrets.token_bytes(16))
        query = {
            **params,
            "state": state,
            "nonce": _b64url(secrets.token_bytes(16)),
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
        answer = await self._authorize(f"{AUTHORIZE_URL}?{urlencode(query)}", redirect_uri)
        if answer is None:
            return None
        if answer.get("state") != state:
            _LOGGER.warning("Identity server answered with an unexpected state")
            return None
        if "code" not in answer:
            _LOGGER.info("Identity server declined: %s", answer.get("error"))
            return None
        response = await self.context.request.post(
            TOKEN_URL,
            form={
                "grant_type": "authorization_code",
                "code": answer["code"],
                "redirect_uri": redirect_uri,
                "client_id": params["client_id"],
                "code_verifier": verifier,
            },
        )
        if not response.ok:
            _LOGGER.warning("Token exchange failed: HTTP %s", response.status)
            return None
        return (await response.json()).get("access_token")

    async def _authorize(self, url: str, redirect_uri: str) -> dict | None:
        """Follow the authorization redirects with the profile's cookies.

        Returns the query of the redirect to redirect_uri, or None when the
        identity server shows a page instead (its login form).  The redirect
        is never loaded in the browser, so the portal cannot consume the code.
        """
        for _ in range(10):
            response = await self.context.request.get(url, max_redirects=0)
            location = response.headers.get("location")
            if response.status not in (301, 302, 303, 307, 308) or not location:
                _LOGGER.info("No silent login: login required (HTTP %s)", response.status)
                return None
            url = urljoin(url, location)
            if url.startswith(redirect_uri):
                return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        _LOGGER.warning("No silent login: too many redirects")
        return None


class HomeAssistant:
    """Minimal Home Assistant REST client."""

    def __init__(self, request) -> None:
        self.request = request

    async def call(self, domain: str, service: str, data: dict) -> bool:
        response = await self.request.post(
            f"{HA_URL}/api/services/{domain}/{service}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            data=data,
        )
        if not response.ok:
            _LOGGER.error(
                "Home Assistant %s.%s failed: HTTP %s", domain, service, response.status
            )
        return response.ok

    async def send_token(self, token: str) -> bool:
        # The integration compares this timestamp with its local clock, so
        # the container must use Home Assistant's time zone (TZ).
        return await self.call(
            "novafos",
            "update_token",
            {
                "access_token": token,
                "access_token_date_updated": datetime.now().strftime(
                    "%Y-%m-%dT%H:%M:%S"
                ),
            },
        )

    async def ask_for_login(self) -> None:
        await self.call(
            "persistent_notification",
            "create",
            {
                "notification_id": NOTIFICATION_ID,
                "title": "NovaFos login needed",
                "message": (
                    "The KMD login session has expired. Log in once at "
                    f"{NOVNC_URL} to resume automatic token renewal."
                ),
            },
        )

    async def clear_notification(self) -> None:
        await self.call(
            "persistent_notification", "dismiss", {"notification_id": NOTIFICATION_ID}
        )


async def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            # Shown on the virtual display served by noVNC; HEADLESS=1 is
            # for testing only.
            headless=os.environ.get("HEADLESS") == "1",
            viewport={"width": 1280, "height": 860},
            locale="da-DK",
        )
        ha = HomeAssistant(await playwright.request.new_context())
        renewer = Renewer(context, ha)
        page = context.pages[0] if context.pages else await context.new_page()
        notified = False

        while True:
            token = await renewer.renew_silently()
            if token is None:
                _LOGGER.info("Waiting for a login in noVNC")
                if not notified:
                    await ha.ask_for_login()
                    notified = True
                await page.goto(LOGIN_URL)
                try:
                    # A token from the portal after a human login.
                    token = await asyncio.wait_for(renewer.tokens.get(), timeout=INTERVAL)
                except asyncio.TimeoutError:
                    continue

            if await ha.send_token(token):
                _LOGGER.info("Sent a new token to Home Assistant")
                if notified:
                    await ha.clear_notification()
                    notified = False
            # Drop tokens the portal fetched on its own meanwhile.
            while not renewer.tokens.empty():
                renewer.tokens.get_nowait()
            await page.goto("about:blank")
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
