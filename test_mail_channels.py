import json
import os
import tempfile
import unittest
from unittest.mock import patch

import requests

import proxyscrape_register as app
from mail_providers import FreeCustomAreueallyClient, create_mail_client


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(payload)

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(self.text)


class MailChannelTest(unittest.TestCase):
    def test_guide_selects_channel_and_extracts_alphanumeric_code(self):
        class FakeClient:
            def list_messages(self, _token):
                return [{"id": "message-1", "subject": "Verify your email"}]

            def get_message(self, _token, _message_id):
                return {"html": "Your verification code: <b>d253ff02f7</b>"}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "_PROXY_CONFIG_PATH", os.path.join(tmp, "proxy.json")), \
             patch.dict(os.environ, {}, clear=True), \
             patch("builtins.input", side_effect=["1", "1", "Y", "", "3"]):
            self.assertEqual(app.guide(), (1, 1, True, "fce_areueally", ""))
        self.assertEqual(
            app.wait_mail_code("user@example.test", "token", FakeClient(), "fake", timeout=1, interval=1),
            "d253ff02f7",
        )
        self.assertEqual(
            app.AUTO_MAIL_PROVIDERS,
            ("gonebox",),
        )

    def test_verified_account_without_proxies_retries_gonebox(self):
        partial = {"email": "partial@example.test", "verified": True, "proxy_count": 0}
        success = {"email": "success@example.test", "verified": True, "proxy_count": 100}
        with patch.object(app, "_register_once", side_effect=[partial, success]) as run, \
             patch.object(app, "save_account") as save:
            result = app.register_one(1, True, "accounts.jsonl", "proxies.txt", "auto_zero_config", 2)
            self.assertIs(result, success)
        self.assertEqual([item.args[2] for item in run.call_args_list], ["gonebox", "gonebox"])
        save.assert_called_once_with(success, "accounts.jsonl")

    def test_claim_trial_returns_new_subaccount_id(self):
        calls = []

        def fake_post(url, **_kwargs):
            calls.append(url)
            return FakeResponse(200, {"success": True, "account_id": "trial-account-1"})

        with patch("proxyscrape_register.requests.post", side_effect=fake_post):
            self.assertEqual(app.claim_trial("access-token"), "trial-account-1")
        self.assertEqual(calls, [app.PS_CLAIM_TRIAL])

    def test_claim_trial_reuses_already_claimed_subaccount(self):
        calls = []

        def fake_post(url, **_kwargs):
            calls.append(url)
            if url == app.PS_CLAIM_TRIAL:
                return FakeResponse(400, {"success": False,
                                          "error": "You have already claimed the Premium free trial."})
            return FakeResponse(200, {"associatedSubaccounts": [{"AccountID": "trial-account-2"}]})

        with patch("proxyscrape_register.requests.post", side_effect=fake_post):
            self.assertEqual(app.claim_trial("access-token"), "trial-account-2")
        self.assertEqual(calls, [app.PS_CLAIM_TRIAL, app.PS_ME])

    def test_mail_client_is_loaded_from_this_project(self):
        client = create_mail_client({"mail": {"provider": "fce_areueally"}})
        self.assertIsInstance(client, FreeCustomAreueallyClient)
        self.assertEqual(app._load_mail_client("fce_areueally").domain, "areueally.info")

    def test_proxy_input_is_normalized_saved_and_credentials_are_masked(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "_PROXY_CONFIG_PATH", os.path.join(tmp, "proxy.json")), \
             patch.dict(os.environ, {}, clear=True), \
             patch("builtins.input", side_effect=["socks5://127.0.0.1:1080", "user:secret@127.0.0.1:7890"]):
            proxy = app._choose_proxy()
            self.assertEqual(proxy, "http://user:secret@127.0.0.1:7890")
            with open(app._PROXY_CONFIG_PATH, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh), {"mode": "proxy", "proxy": proxy})
        self.assertEqual(app._mask_proxy(proxy), "http://***:***@127.0.0.1:7890")

    def test_saved_proxy_precedes_environment_and_blank_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "_PROXY_CONFIG_PATH", os.path.join(tmp, "proxy.json")), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7000"}, clear=True):
            app._save_proxy_preference("http://127.0.0.1:8000")
            with patch("builtins.input", return_value=""):
                self.assertEqual(app._choose_proxy(), "http://127.0.0.1:8000")

    def test_environment_proxy_is_default_without_saved_config(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "_PROXY_CONFIG_PATH", os.path.join(tmp, "proxy.json")), \
             patch.dict(os.environ, {"HTTP_PROXY": "127.0.0.1:7890"}, clear=True), \
             patch("builtins.input", return_value=""):
            self.assertEqual(app._choose_proxy(), "http://127.0.0.1:7890")

    def test_invalid_saved_config_falls_back_to_environment(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "_PROXY_CONFIG_PATH", os.path.join(tmp, "proxy.json")), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7000"}, clear=True):
            with open(app._PROXY_CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(["invalid"], fh)
            self.assertEqual(app._load_proxy_preference(), "http://127.0.0.1:7000")

    def test_direct_is_saved_and_clears_inherited_proxy(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(app, "_PROXY_CONFIG_PATH", os.path.join(tmp, "proxy.json")), \
             patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:7890", "ALL_PROXY": "socks5://127.0.0.1:1080"}, clear=True), \
             patch("builtins.input", return_value="direct"):
            self.assertEqual(app._choose_proxy(), "")
            app._apply_proxy("")
            self.assertNotIn("HTTP_PROXY", os.environ)
            self.assertNotIn("ALL_PROXY", os.environ)
            with open(app._PROXY_CONFIG_PATH, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["mode"], "direct")

    def test_apply_proxy_updates_browser_and_requests_environment(self):
        with patch.dict(os.environ, {"NO_PROXY": "example.test"}, clear=True):
            app._apply_proxy("http://127.0.0.1:7890")
            self.assertEqual(app.BROWSER_PROXY, "http://127.0.0.1:7890")
            self.assertEqual(os.environ["HTTP_PROXY"], app.BROWSER_PROXY)
            self.assertEqual(os.environ["HTTPS_PROXY"], app.BROWSER_PROXY)
            self.assertEqual(os.environ["NO_PROXY"], "localhost,127.0.0.1,::1")
            local_settings = requests.Session().merge_environment_settings(
                "http://127.0.0.1:9222", {}, None, None, None
            )
            self.assertEqual(local_settings["proxies"], {})
            settings = requests.Session().merge_environment_settings(
                "https://example.test", {}, None, None, None
            )
            self.assertEqual(settings["proxies"]["https"], app.BROWSER_PROXY)
            mail_client = FreeCustomAreueallyClient()
            mail_settings = mail_client.session.merge_environment_settings(
                "https://example.test", {}, None, None, None
            )
            self.assertEqual(mail_settings["proxies"]["https"], app.BROWSER_PROXY)

            class FakeOptions:
                proxy = None

                def set_proxy(self, value):
                    self.proxy = value

            options = FakeOptions()
            app._apply_browser_proxy(options)
            self.assertEqual(options.proxy, app.BROWSER_PROXY)


if __name__ == "__main__":
    unittest.main()
