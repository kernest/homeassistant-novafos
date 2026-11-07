"""Attach to a separately started browser over CDP; no profile, credentials or HA."""
import importlib.util
import os
import shutil
import socket
import subprocess
import tempfile
import unittest

from playwright.async_api import async_playwright

os.environ.setdefault('NOVAFOS_LOGIN_URL', 'https://portal.example/')
os.environ.setdefault('HA_URL', 'https://ha.example/')
os.environ.setdefault('HA_TOKEN', 'test-only')
spec = importlib.util.spec_from_file_location('renewer_cdp', os.path.join(os.path.dirname(__file__), 'renew.py'))
renewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renewer)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


CHROME = os.environ.get('TEST_CHROME', 'google-chrome')


@unittest.skipUnless(shutil.which(CHROME), 'needs a Chrome binary (TEST_CHROME)')
class CdpTests(unittest.IsolatedAsyncioTestCase):
    async def test_attaches_to_separately_started_chrome(self):
        port = free_port()
        with tempfile.TemporaryDirectory() as profile:
            chrome = subprocess.Popen(
                [CHROME, '--headless=new', f'--user-data-dir={profile}',
                 f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
                 '--no-first-run', 'about:blank'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                renewer.CDP_URL = f'http://127.0.0.1:{port}'
                async with async_playwright() as p:
                    context = await renewer.connect_to_running_chrome(p)
                    page = await context.new_page()
                    await page.set_content('<p id="ok">attached</p>')
                    self.assertEqual(await page.text_content('#ok'), 'attached')
            finally:
                chrome.terminate()
                chrome.wait(timeout=10)

class CdpFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_browser_raises_after_retries(self):
        renewer.CDP_URL = f'http://127.0.0.1:{free_port()}'
        async with async_playwright() as p:
            with self.assertRaises(renewer.PlaywrightError):
                await renewer.connect_to_running_chrome(p)


if __name__ == '__main__':
    unittest.main()
