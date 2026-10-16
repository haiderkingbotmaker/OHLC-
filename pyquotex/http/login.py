import re
import json
import sys
import asyncio
import logging
from pathlib import Path
from pyquotex.config import update_session
from pyquotex.http.navigator import Browser
from pyquotex.session_utils import (
    is_valid_ws_token,
    extract_settings_json,
    extract_token_from_html,
    extract_ws_session_cookie,
)

logger = logging.getLogger(__name__)


class Login(Browser):
    """Class for Quotex login resource."""

    url = ""
    cookies = None
    ssid = None

    def __init__(self, api, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api = api
        self.base_url = api.host
        self.https_base_url = f'https://{self.base_url}'
        self.html = None
        self.headers = self.get_headers()
        self.full_url = f"{self.https_base_url}/{api.lang}"

    def get_token(self):
        self.headers["Connection"] = "keep-alive"
        self.headers["Accept-Encoding"] = "gzip, deflate, br"
        self.headers["Accept-Language"] = "pt-BR,pt;q=0.8,en-US;q=0.5,en;q=0.3"
        self.headers["Accept"] = (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        )
        self.headers["Referer"] = f"{self.full_url}/sign-in"
        self.headers["Upgrade-Insecure-Requests"] = "1"
        self.headers["Sec-Ch-Ua-Mobile"] = "?0"
        self.headers["Sec-Ch-Ua-Platform"] = '"Linux"'
        self.headers["Sec-Fetch-Site"] = "same-origin"
        self.headers["Sec-Fetch-User"] = "?1"
        self.headers["Sec-Fetch-Dest"] = "document"
        self.headers["Sec-Fetch-Mode"] = "navigate"
        self.headers["Dnt"] = "1"
        self.send_request(
            "GET",
            f"{self.full_url}/sign-in/modal/"
        )
        html = self.get_soup()
        match = html.find(
            "input", {"name": "_token"}
        )
        token = None if not match else match.get("value")
        return token

    async def awaiting_pin(self, data, input_message):
        self.headers["Content-Type"] = "application/x-www-form-urlencoded"
        self.headers["Referer"] = f"{self.full_url}/sign-in/modal"
        data["keep_code"] = 1
        try:
            code = input(input_message)
            if not code.isdigit():
                print("Please enter a valid code.")
                await self.awaiting_pin(data, input_message)
            data["code"] = code
        except KeyboardInterrupt:
            print("\nClosing program.")
            sys.exit()

        await asyncio.sleep(1)
        self.send_request(
            method="POST",
            url=f"{self.full_url}/sign-in/modal",
            data=data
        )

    def _save_session(self, token: str):
        self.cookies = self.get_cookies()
        self.api.session_data["cookies"] = self.cookies
        self.api.session_data["token"] = token
        self.api.session_data["user_agent"] = self.headers.get("User-Agent", "")
        self.ssid = token
        update_session(self.api.username, self.api.session_data)
        logger.info("Successfully loaded session token")

    def login_with_cloudscraper(self, username: str, password: str):
        """Login via Cloudscraper to bypass Cloudflare bot challenges."""
        try:
            import cloudscraper
        except ImportError:
            logger.warning("cloudscraper not installed, skipping Cloudflare bypass login")
            return False

        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "linux", "mobile": False}
        )
        sign_in_url = f"{self.full_url}/sign-in/"
        trade_url = f"{self.full_url}/trade"

        resp = scraper.get(sign_in_url, timeout=45)
        if resp.status_code != 200 or "Just a moment" in resp.text:
            logger.warning("Cloudscraper: sign-in page blocked or unavailable")
            return False

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, "html.parser")
        csrf_input = soup.find("input", {"name": "_token"})
        csrf = csrf_input.get("value") if csrf_input else None
        if not csrf:
            logger.warning("Cloudscraper: CSRF token not found")
            return False

        payload = {
            "_token": csrf,
            "email": username,
            "password": password,
            "remember": "1",
        }
        resp2 = scraper.post(
            sign_in_url,
            data=payload,
            headers={
                "Referer": sign_in_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=45,
            allow_redirects=True,
        )
        if resp2.status_code not in (200, 301, 302, 303, 307, 308):
            logger.warning("Cloudscraper: login POST failed with status %s", resp2.status_code)
            return False

        resp3 = scraper.get(trade_url, timeout=45, headers={"Referer": sign_in_url})
        if resp3.status_code != 200 or "Just a moment" in resp3.text:
            logger.warning("Cloudscraper: trade page blocked after login")
            return False

        token, settings_json = extract_token_from_html(resp3.text)
        cookies_dict = scraper.cookies.get_dict()
        if not token:
            token = extract_ws_session_cookie(cookies_dict)

        if not is_valid_ws_token(token):
            logger.warning(
                "Cloudscraper: no valid WebSocket token (settings keys: %s)",
                list(settings_json.keys()),
            )
            return False

        # Sync cloudscraper cookies into the requests session used by Browser
        self.cookies.clear()
        for name, value in cookies_dict.items():
            self.cookies.set(name, value)
        ua = scraper.headers.get("User-Agent")
        if ua:
            self.headers["User-Agent"] = ua

        self._save_session(token)
        logger.info("Login successful via Cloudscraper")
        return True

    def get_profile(self):
        self.response = self.send_request(
            method="GET",
            url=f"{self.full_url}/trade"
        )
        if not self.response:
            logger.error("get_profile: No response from /trade")
            return None, None

        if "Just a moment" in (self.response.text or ""):
            logger.error("get_profile: Cloudflare challenge page returned instead of /trade")
            return None, None

        try:
            token, settings_json = extract_token_from_html(self.response.text)
            cookie_jar = self.cookies

            if not token:
                cookies_dict = {c.name: c.value for c in cookie_jar}
                token = extract_ws_session_cookie(cookies_dict)

            if not is_valid_ws_token(token):
                logger.error("No valid WebSocket token found on /trade page")
                logger.info("window.settings keys: %s", list(settings_json.keys()))
                logger.info("Cookie names: %s", [c.name for c in cookie_jar])
                return None, None

            self._save_session(token)
            return self.response, settings_json

        except Exception as e:
            logger.error("Error in get_profile: %s", e, exc_info=True)
            return None, None

    def _get(self):
        return self.send_request(
            method="GET",
            url=f"{self.full_url}/trade"
        )

    async def _post(self, data):
        """Send get request for Quotex API login http resource.
        :returns: The instance of: class:`requests.Response`.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        self.response = self.send_request(
            method="POST",
            url=f"{self.full_url}/sign-in/",
            data=data
        )
        logger.info(f"Login POST response status code: {self.response.status_code if self.response else 'NO RESPONSE'}")
        if self.response:
            logger.info(f"Login POST response URL: {self.response.url}")
            
        required_keep_code = self.get_soup().find(
            "input", {"name": "keep_code"}
        )
        if required_keep_code:
            auth_body = self.get_soup().find(
                "main", {"class": "auth__body"}
            )
            input_message = (
                f'{auth_body.find("p").text}: ' if auth_body.find("p")
                else "Insira o código PIN que acabamos "
                     "de enviar para o seu e-mail: "
            )
            await self.awaiting_pin(data, input_message)
        await asyncio.sleep(1)
        success = self.success_login()
        return success
    
    def success_login(self):
        import logging
        logger = logging.getLogger(__name__)
        
        if "trade" in self.response.url:
            logger.info("success_login: URL contains 'trade' — login successful!")
            return True, "Login successful."

        soup = self.get_soup()

        not_available = soup.select_one("#tab-1 > div > div.modal-sign__not-avalible__title")
        if not_available:
            msg = not_available.get_text(strip=True)
            logger.error(f"success_login: Service unavailable: {msg}")
            return False, f"Service unavailable: {msg}"

        error = soup.select_one("#tab-1 form > div:nth-child(2) > div")
        msg = error.get_text(strip=True) if error else "Unknown error"
        logger.error(f"success_login: Login failed with message: {msg}")

        return False, f"Login failed. {msg}"

    async def __call__(self, username, password, user_data_dir=None):
        """Method to get Quotex API login http request."""
        if self.login_with_cloudscraper(username, password):
            return True, "Login successful via Cloudscraper."

        data = {
            "_token": self.get_token(),
            "email": username,
            "password": password,
            "remember": 1,
        }
        status, msg = await self._post(data)
        if status:
            result = self.get_profile()
            if isinstance(result, tuple):
                profile_resp, profile_data = result
            else:
                profile_resp = None
                profile_data = None

            if not is_valid_ws_token(self.ssid):
                return False, "Failed to get valid WebSocket token from profile page"

        return status, msg
