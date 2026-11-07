"""Keep the NovaFos integration supplied with KMD access tokens.

KMD Easy-Energy issues 60 minute access tokens and no refresh tokens, and its
login form is protected by reCAPTCHA.  This service keeps one Chromium
profile.  A person logs in once through noVNC (solving any reCAPTCHA
themselves).  Afterwards the service replays the portal's own OIDC
authorization request in that profile: while KMD's login session is valid the
identity server answers without showing the login form, and the new token is
sent to Home Assistant's novafos.update_token action.

When the session has expired the login form is shown instead.  The service
tries the saved browser credentials once. If they are absent, login fails,
or a CAPTCHA is shown, it leaves the portal open and asks for a human login.
It never supplies credentials or interacts with CAPTCHA controls.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
from contextlib import suppress
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError

IDENTITY = os.environ.get("KMD_IDENTITY_URL", "https://easy-energy-identity.kmd.dk")
AUTHORIZE_URL = f"{IDENTITY}/oidc/authorize"
TOKEN_URL = f"{IDENTITY}/oidc/token"

LOGIN_URL = os.environ["NOVAFOS_LOGIN_URL"]
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
NOVNC_URL = os.environ.get("NOVNC_URL", "the noVNC page of the token renewer")
INTERVAL = int(os.environ.get("RENEW_INTERVAL_MINUTES", "180")) * 60
# Optional fixed daily renewal time ("HH:MM", local time).  Overrides the interval.
RENEW_AT = os.environ.get("RENEW_AT", "").strip()
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
PROFILE_DIR = DATA_DIR / "profile"
# Parameters of the portal's own authorization request, recorded from the
# last human login.  Per-request values are regenerated on every renewal.
AUTHORIZE_FILE = DATA_DIR / "authorize.json"
PER_REQUEST = {"state", "nonce", "code_challenge", "code_challenge_method"}
# KMD's login session is a session cookie, which Chromium drops when it
# restarts.  It is saved here after each login or renewal and restored at start.
COOKIES_FILE = DATA_DIR / "cookies.json"
AUTO_LOGIN = os.environ.get("AUTO_LOGIN_SAVED_CREDENTIALS", "true").lower() == "true"
HA_TIMEOUT = int(os.environ.get("HA_TIMEOUT_SECONDS", "600")) * 1000
# Attach to an already running Chrome (e.g. http://127.0.0.1:9222) instead of
# launching Playwright's Chromium with PROFILE_DIR.
CDP_URL = os.environ.get("BROWSER_CDP_URL", "").strip()

NOTIFICATION_ID = "novafos_token_renewer"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
_LOGGER = logging.getLogger("novafos-token-renewer")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _seconds_until_next_run() -> float:
    """Seconds until the next renewal: RENEW_AT if set, otherwise INTERVAL."""
    if not RENEW_AT:
        return INTERVAL
    hour, minute = (int(part) for part in RENEW_AT.split(":"))
    now = datetime.now()
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return (run - now).total_seconds()


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


async def try_saved_login(page: Page) -> bool:
    """Submit browser-autofilled credentials once, only on our identity server.

    Read only presence flags, never extract or log the stored credentials.
    A challenge or failed submission is left to the owner in noVNC.
    """
    if not AUTO_LOGIN:
        return False
    try:
        await page.wait_for_url(f"{IDENTITY}/Identity/Account/Sign/Login*", timeout=15000)
        if urlsplit(page.url).netloc != urlsplit(IDENTITY).netloc:
            return False
        email_login = page.get_by_role("button", name="Log på med e-mail", exact=True)
        # The first button reveals the form; the second submits that same form.
        password = page.locator('input[type="password"]')
        if not await password.is_visible():
            await email_login.click(timeout=5000)
        await password.wait_for(state="visible", timeout=5000)
        email = page.locator('#Input_Email')
        for _ in range(30):
            if urlsplit(page.url).netloc != urlsplit(IDENTITY).netloc:
                return False
            if await page.locator('iframe[title*="challenge" i]').is_visible():
                _LOGGER.info("Login challenge requires the owner")
                return False
            populated = await email.evaluate("el => Boolean(el.value)") and await password.evaluate("el => Boolean(el.value)")
            if populated:
                await email_login.click(timeout=5000)
                _LOGGER.info("Submitted login using browser-saved credentials")
                # KMD can explicitly ask for another ordinary login submission.
                # Retry only that response, once; never operate CAPTCHA controls.
                try:
                    await page.wait_for_function(
                        """identity => location.origin !== identity ||
                        document.body.innerText.includes('Vi er ikke sikre på om du er et menneske')""",
                        arg=IDENTITY,
                        timeout=10000,
                    )
                    retry = page.get_by_text(
                        re.compile("Vi er ikke sikre på om du er et menneske")
                    )
                    if (
                        urlsplit(page.url).netloc == urlsplit(IDENTITY).netloc
                        and await retry.is_visible()
                        and not await page.locator('iframe[title*="challenge" i]').is_visible()
                        and await email.evaluate("el => Boolean(el.value)")
                        and await password.evaluate("el => Boolean(el.value)")
                    ):
                        await email_login.click(timeout=5000)
                        _LOGGER.info("Retried email login once after KMD requested another attempt")
                except PlaywrightError:
                    pass
                return True
            await asyncio.sleep(0.5)
        _LOGGER.info("Saved credentials were not autofilled; waiting for the owner")
    except PlaywrightError:
        _LOGGER.info("Automatic login unavailable; waiting for the owner")
    return False


async def open_consumption_when_ready(page: Page) -> None:
    """After login KMD may land on the profile, which does not issue API tokens.

    Open the ordinary Forbrug menu once. Keep watching while the owner handles
    a manual login too; otherwise an apparently successful login stays idle.
    """
    while True:
        parsed = urlsplit(page.url)
        if (
            (parsed.scheme, parsed.netloc)
            == (urlsplit(LOGIN_URL).scheme, urlsplit(LOGIN_URL).netloc)
            and parsed.path.rstrip("/").lower() == "/profile/home"
        ):
            try:
                await page.get_by_text(re.compile(r"^Forbrug$", re.I)).click(timeout=5000)
                _LOGGER.info("Opened Forbrug to obtain the portal API token")
            except PlaywrightError:
                _LOGGER.info("Could not open Forbrug; leaving the portal for the owner")
            return
        await asyncio.sleep(0.5)


async def wait_for_portal_token(renewer: Renewer, page: Page) -> str:
    """Capture a portal token while handling its post-login landing page."""
    navigation = asyncio.create_task(open_consumption_when_ready(page))
    try:
        return await renewer.tokens.get()
    finally:
        navigation.cancel()
        with suppress(asyncio.CancelledError):
            await navigation


class HomeAssistant:
    """Minimal Home Assistant REST client."""

    def __init__(self, request) -> None:
        self.request = request

    async def call(self, domain: str, service: str, data: dict) -> bool:
        # update_token returns only after the integration's data refresh,
        # which can take minutes.  A failure must not end the process: a
        # restart loses KMD's session cookie.
        try:
            response = await self.request.post(
                f"{HA_URL}/api/services/{domain}/{service}",
                headers={"Authorization": f"Bearer {HA_TOKEN}"},
                data=data,
                timeout=HA_TIMEOUT,
            )
        except PlaywrightError as err:
            _LOGGER.error("Home Assistant %s.%s failed: %s", domain, service, err)
            return False
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


async def save_cookies(context: BrowserContext) -> None:
    """Keep the session cookies across restarts.  Readable by the owner only."""
    COOKIES_FILE.touch(mode=0o600)
    COOKIES_FILE.write_text(json.dumps(await context.cookies()))


async def restore_cookies(context: BrowserContext) -> None:
    if not COOKIES_FILE.exists():
        return
    try:
        await context.add_cookies(json.loads(COOKIES_FILE.read_text()))
    except (ValueError, PlaywrightError) as err:
        _LOGGER.warning("Could not restore saved cookies: %s", err)


async def connect_to_running_chrome(playwright) -> BrowserContext:
    """Attach to an ordinary Chrome started separately with its own profile.

    Some KMD logins are rejected from Playwright's bundled Chromium. A regular
    Chrome with its sandbox intact, launched by the host and listening for
    DevTools on loopback only, keeps the browser-saved login and is accepted.
    """
    for attempt in range(10):
        try:
            browser = await playwright.chromium.connect_over_cdp(CDP_URL, timeout=2000)
            break
        except PlaywrightError:
            if attempt == 9:
                raise
            await asyncio.sleep(1)
    return browser.contexts[0]


async def main() -> None:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        if CDP_URL:
            context = await connect_to_running_chrome(playwright)
        else:
            context = await playwright.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                # Shown on the virtual display served by noVNC; HEADLESS=1 is
                # for testing only.
                headless=os.environ.get("HEADLESS") == "1",
                viewport={"width": 1280, "height": 860},
                locale="da-DK",
            )
        await restore_cookies(context)
        ha = HomeAssistant(await playwright.request.new_context())
        renewer = Renewer(context, ha)
        page = context.pages[0] if context.pages else await context.new_page()
        notified = False

        while True:
            token = await renewer.renew_silently()
            if token is None:
                await page.goto(LOGIN_URL)
                if renewer.tokens.empty():
                    await try_saved_login(page)
                try:
                    # Portal tokens are captured by the response listener.
                    token = await asyncio.wait_for(wait_for_portal_token(renewer, page), timeout=30)
                except asyncio.TimeoutError:
                    _LOGGER.info("Waiting for a login in noVNC")
                    if not notified:
                        await ha.ask_for_login()
                        notified = True
                    try:
                        token = await asyncio.wait_for(
                            wait_for_portal_token(renewer, page),
                            timeout=_seconds_until_next_run(),
                        )
                    except asyncio.TimeoutError:
                        continue

            await save_cookies(context)
            if await ha.send_token(token):
                _LOGGER.info("Sent a new token to Home Assistant")
                if notified:
                    await ha.clear_notification()
                    notified = False
            # Drop tokens the portal fetched on its own meanwhile.
            while not renewer.tokens.empty():
                renewer.tokens.get_nowait()
            await page.goto("about:blank")
            await asyncio.sleep(_seconds_until_next_run())


if __name__ == "__main__":
    asyncio.run(main())
