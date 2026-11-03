"""Isolated Chromium checks: no production profile, credentials, or HA requests."""
import asyncio
import importlib.util
import os
import unittest
from unittest.mock import patch
from playwright.async_api import async_playwright

os.environ.setdefault('NOVAFOS_LOGIN_URL', 'https://portal.example/')
os.environ.setdefault('HA_URL', 'https://ha.example/')
os.environ.setdefault('HA_TOKEN', 'test-only')
spec = importlib.util.spec_from_file_location('renewer', os.path.join(os.path.dirname(__file__), 'renew.py'))
renewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renewer)
URL = renewer.IDENTITY + '/Identity/Account/Sign/Login?test=1'

class SavedLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.submissions = []

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def form(self, populated=True, challenge=False):
        value = 'fixture' if populated else ''
        html = f'''<button id="reveal" onclick="document.querySelector('form').hidden=false;this.remove()">Log på med e-mail</button>
        <form hidden onsubmit="event.preventDefault();document.body.dataset.submitted='yes'">
        <input id="Input_Email" type="text" value="{'fixture@example.test' if populated else ''}">
        <input type="password" value="{value}">
        <button>Log på med e-mail</button></form>'''
        if challenge:
            html += '<iframe title="recaptcha challenge" srcdoc="Owner action required"></iframe>'
        await self.page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html; charset=utf-8'))
        await self.page.goto(URL)

    async def submitted(self):
        return await self.page.locator('body').get_attribute('data-submitted') == 'yes'

    async def test_saved_fields_submit(self):
        await self.form()
        self.assertTrue(await renewer.try_saved_login(self.page))
        self.assertTrue(await self.submitted())

    async def test_empty_fields_never_submit(self):
        await self.form(populated=False)
        real_sleep = asyncio.sleep
        async def fast_sleep(_):
            await real_sleep(0)
        with patch.object(renewer.asyncio, 'sleep', fast_sleep):
            self.assertFalse(await renewer.try_saved_login(self.page))
        self.assertFalse(await self.submitted())

    async def test_challenge_never_submit(self):
        await self.form(challenge=True)
        self.assertFalse(await renewer.try_saved_login(self.page))
        self.assertFalse(await self.submitted())

    async def test_disabled_never_submit(self):
        await self.form()
        with patch.object(renewer, 'AUTO_LOGIN', False):
            self.assertFalse(await renewer.try_saved_login(self.page))
        self.assertFalse(await self.submitted())

    async def test_cross_origin_never_submit(self):
        await self.form()
        await self.page.goto('https://untrusted.example/Identity/Account/Sign/Login')
        # Simulate navigation completing after the initial URL wait.
        with patch.object(self.page, 'wait_for_url'):
            self.assertFalse(await renewer.try_saved_login(self.page))
        self.assertFalse(await self.submitted())

    async def test_retry_then_consumption_captures_token(self):
        attempts = []
        portal = renewer.LOGIN_URL.split('/Login')[0].rstrip('/')
        # Use the configured portal origin, never send any request to it.
        from urllib.parse import urlsplit
        origin = urlsplit(renewer.LOGIN_URL)
        profile = f"{origin.scheme}://{origin.netloc}/profile/home"
        form = f"""<form method="post" action="{URL}">
        <input id="Input_Email" type="text" value="fixture@example.test">
        <input type="password" value="fixture">
        <button>Log på med e-mail</button></form>"""
        async def route_request(route):
            if route.request.url == renewer.TOKEN_URL:
                await route.fulfill(json={'access_token': 'fixture-token'})
            elif route.request.url == profile:
                await route.fulfill(body=f'<a href="{renewer.TOKEN_URL}">FORBRUG</a>', content_type='text/html; charset=utf-8')
            elif route.request.method == 'POST':
                attempts.append(1)
                if len(attempts) == 1:
                    await route.fulfill(body='<p>Vi er ikke sikre på om du er et menneske, og ikke en robot – prøv venligst at logge på igen</p>'+form, content_type='text/html; charset=utf-8')
                else:
                    await route.fulfill(body=f'<script>location.href={profile!r}</script>', content_type='text/html; charset=utf-8')
            else:
                await route.fulfill(body=form, content_type='text/html; charset=utf-8')
        await self.page.route('**/*', route_request)
        observer = renewer.Renewer(self.page.context, None)
        await self.page.goto(URL)
        self.assertTrue(await renewer.try_saved_login(self.page))
        token = await asyncio.wait_for(renewer.wait_for_portal_token(observer, self.page), timeout=5)
        self.assertEqual(token, 'fixture-token')
        self.assertEqual(len(attempts), 2)

    async def test_second_rejection_does_not_loop(self):
        attempts = []
        form = f"""<form method="post" action="{URL}">
        <input id="Input_Email" value="fixture@example.test">
        <input type="password" value="fixture">
        <button>Log på med e-mail</button></form>"""
        async def route_request(route):
            if route.request.method == 'POST':
                attempts.append(1)
                body = '<p>Vi er ikke sikre på om du er et menneske – prøv igen</p>'+form
            else:
                body = form
            await route.fulfill(body=body, content_type='text/html; charset=utf-8')
        await self.page.route('**/*', route_request)
        await self.page.goto(URL)
        self.assertTrue(await renewer.try_saved_login(self.page))
        self.assertEqual(len(attempts), 2)

if __name__ == '__main__':
    unittest.main()
