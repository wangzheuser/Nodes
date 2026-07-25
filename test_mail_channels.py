import unittest
from unittest.mock import patch

import proxyscrape_register as app


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
        self.assertEqual(app.AUTO_MAIL_PROVIDERS, ("gonebox", "fce_ditpay", "fce_areueally"))


if __name__ == "__main__":
    unittest.main()
