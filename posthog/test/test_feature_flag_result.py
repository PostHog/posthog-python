import unittest


from posthog.types import FeatureFlagResult, FeatureFlag, FlagMetadata, FlagReason

class TestFeatureFlagResult(unittest.TestCase):
    def test_from_bool_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload("test-flag", True, '{"some": "value"}')

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, '{"some": "value"}')

    def test_from_bool_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload("test-flag", False, '{"some": "value"}')

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, False)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, '{"some": "value"}')
    
    def test_from_variant_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload("test-flag", "control", '{"some": "value"}')

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, "control")
        self.assertEqual(result.payload, '{"some": "value"}')

    def test_from_none_value_and_payload(self):
        result = FeatureFlagResult.from_value_and_payload("test-flag", None, '{"some": "value"}')
        self.assertIsNone(result)

    def test_from_boolean_flag_details(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant=None,
            metadata=FlagMetadata(
                id=1,
                version=1,
                description="test-flag",
                payload='{"some": "value"}'
            ),
            reason=FlagReason(
                code="test-reason",
                description="test-reason",
                condition_index=0
            )
        )

        result = FeatureFlagResult.from_flag_details(flag_details)

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertEqual(result.payload, '{"some": "value"}')

    def test_from_variant_flag_details(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant="control",
            metadata=FlagMetadata(
                id=1,
                version=1,
                description="test-flag",
                payload='{"some": "value"}'
            ),
            reason=FlagReason(
                code="test-reason",
                description="test-reason",
                condition_index=0
            )
        )

        result = FeatureFlagResult.from_flag_details(flag_details)

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, "control")
        self.assertEqual(result.payload, '{"some": "value"}')

    def test_from_none_flag_details(self):
        result = FeatureFlagResult.from_flag_details(None)

        self.assertIsNone(result)

    def test_from_flag_details_with_none_payload(self):
        flag_details = FeatureFlag(
            key="test-flag",
            enabled=True,
            variant=None,
            metadata=FlagMetadata(
                id=1,
                version=1,
                description="test-flag",
                payload=None
            ),
            reason=FlagReason(
                code="test-reason",
                description="test-reason",
                condition_index=0
            )
        )

        result = FeatureFlagResult.from_flag_details(flag_details)

        self.assertEqual(result.key, "test-flag")
        self.assertEqual(result.enabled, True)
        self.assertEqual(result.variant, None)
        self.assertIsNone(result.payload)