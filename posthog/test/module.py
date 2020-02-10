import unittest

import analytics


class TestModule(unittest.TestCase):

    def failed(self):
        self.failed = True

    def setUp(self):
        self.failed = False
        analytics.api_key = 'testsecret'
        analytics.on_error = self.failed

    def test_no_api_key(self):
        analytics.api_key = None
        self.assertRaises(Exception, analytics.track)

    def test_no_host(self):
        analytics.host = None
        self.assertRaises(Exception, analytics.track)

    def test_track(self):
        analytics.track('distinct_id', 'python module event')
        analytics.flush()

    def test_identify(self):
        analytics.identify('distinct_id', {'email': 'user@email.com'})
        analytics.flush()

    def test_group(self):
        analytics.group('distinct_id', 'groupId')
        analytics.flush()

    def test_alias(self):
        analytics.alias('previousId', 'distinct_id')
        analytics.flush()

    def test_page(self):
        analytics.page('distinct_id')
        analytics.flush()

    def test_screen(self):
        analytics.screen('distinct_id')
        analytics.flush()

    def test_flush(self):
        analytics.flush()
