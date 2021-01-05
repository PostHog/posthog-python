import unittest

import posthog


class TestModule(unittest.TestCase):
    def failed(self):
        self.failed = True

    def setUp(self):
        self.failed = False
        posthog.api_key = "testsecret"
        posthog.on_error = self.failed

    def test_no_api_key(self):
        posthog.api_key = None
        self.assertRaises(Exception, posthog.capture)

    def test_no_host(self):
        posthog.host = None
        self.assertRaises(Exception, posthog.capture)

    def test_track(self):
        posthog.capture("distinct_id", "python module event")
        posthog.flush()

    def test_identify(self):
        posthog.identify("distinct_id", {"email": "user@email.com"})
        posthog.flush()

    def test_alias(self):
        posthog.alias("previousId", "distinct_id")
        posthog.flush()

    def test_page(self):
        posthog.page("distinct_id", "https://posthog.com/contact")
        posthog.flush()

    def test_flush(self):
        posthog.flush()
