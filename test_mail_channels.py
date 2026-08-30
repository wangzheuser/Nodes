import unittest
from unittest.mock import patch

import proxyscrape_register as app
from mail_providers import FreeCustomAreueallyClient, create_mail_client


class MailChannelTest(unittest.TestCase):
    def test_guide_selects_channel_and_extracts_alphanumeric_code(self):
        class FakeClient:
            def list_messages(self, _token):
                return [{"id": "message-1", "subject": "Verify your email"}]

            def get_message(self, _token, _message_id):
                return {"html": "Your verification code: <b>d253ff02f7</b>"}

        with patch("builtins.input", side_effect=["1", "1", "Y", "3"]):
            self.assertEqual(app.guide(), (1, 1, True, "fce_areueally"))
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

    def test_mail_client_is_loaded_from_this_project(self):
        client = create_mail_client({"mail": {"provider": "fce_areueally"}})
        self.assertIsInstance(client, FreeCustomAreueallyClient)
        self.assertEqual(app._load_mail_client("fce_areueally").domain, "areueally.info")


if __name__ == "__main__":
    unittest.main()
