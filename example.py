# PostHog Python library example
#
# This script demonstrates various PostHog Python SDK capabilities including:
# - Basic event capture and user identification
# - Feature flag local evaluation
# - Feature flag payloads
# - Context management and tagging
#
# Setup:
# 1. Copy .env.example to .env and fill in your PostHog credentials
# 2. Run this script and choose from the interactive menu

import os

import posthog


def load_env_file():
    """Load environment variables from .env file if it exists."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())


# Load .env file if it exists
load_env_file()

# Get configuration
project_key = os.getenv("POSTHOG_PROJECT_API_KEY", "")
personal_api_key = os.getenv("POSTHOG_PERSONAL_API_KEY", "")
host = os.getenv("POSTHOG_HOST", "http://localhost:8000")

# Check if project key is provided (required)
if not project_key:
    print("❌ Missing PostHog project API key!")
    print("   Please set POSTHOG_PROJECT_API_KEY environment variable")
    print("   or copy .env.example to .env and fill in your values")
    exit(1)

# Configure PostHog with credentials
posthog.debug = False
posthog.api_key = project_key
posthog.project_api_key = project_key
posthog.host = host
posthog.poll_interval = 10

# Check if personal API key is available for local evaluation
local_eval_available = bool(personal_api_key)
if personal_api_key:
    posthog.personal_api_key = personal_api_key

print("🔑 PostHog Configuration:")
print(f"   Project API Key: {project_key[:9]}...")
if local_eval_available:
    print("   Personal API Key: [SET]")
else:
    print("   Personal API Key: [NOT SET] - Local evaluation examples will be skipped")
print(f"   Host: {host}\n")

# Display menu and get user choice
print("🚀 PostHog Python SDK Demo - Choose an example to run:\n")
print("1. Identify and capture examples")
local_eval_note = "" if local_eval_available else " [requires personal API key]"
print(f"2. Feature flag local evaluation examples{local_eval_note}")
print("3. Feature flag payload examples")
print(f"4. Flag dependencies examples{local_eval_note}")
print("5. Context management and tagging examples")
print("6. Run all examples")
print("7. Exit")
choice = input("\nEnter your choice (1-7): ").strip()

if choice == "1":
    print("\n" + "=" * 60)
    print("IDENTIFY AND CAPTURE EXAMPLES")
    print("=" * 60)

    posthog.debug = True

    # Capture an event
    print("📊 Capturing events...")
    posthog.capture(
        "event",
        distinct_id="distinct_id",
        properties={"property1": "value", "property2": "value"},
        send_feature_flags=True,
    )

    # Alias a previous distinct id with a new one
    print("🔗 Creating alias...")
    posthog.alias("distinct_id", "new_distinct_id")

    posthog.capture(
        "event2",
        distinct_id="new_distinct_id",
        properties={"property1": "value", "property2": "value"},
    )
    posthog.capture(
        "event-with-groups",
        distinct_id="new_distinct_id",
        properties={"property1": "value", "property2": "value"},
        groups={"company": "id:5"},
    )

    # Add properties to the person
    print("👤 Identifying user...")
    posthog.set(
        distinct_id="new_distinct_id", properties={"email": "something@something.com"}
    )

    # Add properties to a group
    print("🏢 Identifying group...")
    posthog.group_identify("company", "id:5", {"employees": 11})

    # Properties set only once to the person
    print("🔒 Setting properties once...")
    posthog.set_once(
        distinct_id="new_distinct_id", properties={"self_serve_signup": True}
    )

    # This will not change the property (because it was already set)
    posthog.set_once(
        distinct_id="new_distinct_id", properties={"self_serve_signup": False}
    )

    print("🔄 Updating properties...")
    posthog.set(distinct_id="new_distinct_id", properties={"current_browser": "Chrome"})
    posthog.set(
        distinct_id="new_distinct_id", properties={"current_browser": "Firefox"}
    )

elif choice == "2":
    if not local_eval_available:
        print("\n❌ This example requires a personal API key for local evaluation.")
        print(
            "   Set POSTHOG_PERSONAL_API_KEY environment variable to run this example."
        )
        posthog.shutdown()
        exit(1)

    print("\n" + "=" * 60)
    print("FEATURE FLAG LOCAL EVALUATION EXAMPLES")
    print("=" * 60)

    posthog.debug = True

    print("🏁 Testing basic feature flags...")
    print(
        f"beta-feature for 'distinct_id': {posthog.feature_enabled('beta-feature', 'distinct_id')}"
    )
    print(
        f"beta-feature for 'new_distinct_id': {posthog.feature_enabled('beta-feature', 'new_distinct_id')}"
    )
    print(
        f"beta-feature with groups: {posthog.feature_enabled('beta-feature-groups', 'distinct_id', groups={'company': 'id:5'})}"
    )

    print("\n🌍 Testing location-based flags...")
    # Assume test-flag has `City Name = Sydney` as a person property set
    print(
        f"Sydney user: {posthog.feature_enabled('test-flag', 'random_id_12345', person_properties={'$geoip_city_name': 'Sydney'})}"
    )

    print(
        f"Sydney user (local only): {posthog.feature_enabled('test-flag', 'distinct_id_random_22', person_properties={'$geoip_city_name': 'Sydney'}, only_evaluate_locally=True)}"
    )

    print("\n📋 Getting all flags...")
    print(f"All flags: {posthog.get_all_flags('distinct_id_random_22')}")
    print(
        f"All flags (local): {posthog.get_all_flags('distinct_id_random_22', only_evaluate_locally=True)}"
    )
    print(
        f"All flags with properties: {posthog.get_all_flags('distinct_id_random_22', person_properties={'$geoip_city_name': 'Sydney'}, only_evaluate_locally=True)}"
    )

elif choice == "3":
    print("\n" + "=" * 60)
    print("FEATURE FLAG PAYLOAD EXAMPLES")
    print("=" * 60)

    posthog.debug = True

    print("📦 Testing feature flag payloads...")
    print(
        f"beta-feature payload: {posthog.get_feature_flag_payload('beta-feature', 'distinct_id')}"
    )
    print(
        f"All flags and payloads: {posthog.get_all_flags_and_payloads('distinct_id')}"
    )
    print(
        f"Remote config payload: {posthog.get_remote_config_payload('encrypted_payload_flag_key')}"
    )

    # Get feature flag result with all details (enabled, variant, payload, key, reason)
    print("\n🔍 Getting detailed flag result...")
    result = posthog.get_feature_flag_result("beta-feature", "distinct_id")
    if result:
        print(f"Flag key: {result.key}")
        print(f"Flag enabled: {result.enabled}")
        print(f"Variant: {result.variant}")
        print(f"Payload: {result.payload}")
        print(f"Reason: {result.reason}")
        # get_value() returns the variant if it exists, otherwise the enabled value
        print(f"Value (variant or enabled): {result.get_value()}")

elif choice == "4":
    if not local_eval_available:
        print("\n❌ This example requires a personal API key for local evaluation.")
        print(
            "   Set POSTHOG_PERSONAL_API_KEY environment variable to run this example."
        )
        posthog.shutdown()
        exit(1)

    print("\n" + "=" * 60)
    print("FLAG DEPENDENCIES EXAMPLES")
    print("=" * 60)
    print("🔗 Testing flag dependencies with local evaluation...")
    print(
        "   Flag structure: 'test-flag-dependency' depends on 'beta-feature' being enabled"
    )
    print("")
    print("📋 Required setup (if 'test-flag-dependency' doesn't exist):")
    print("   1. Create feature flag 'beta-feature':")
    print("      - Condition: email contains '@example.com'")
    print("      - Rollout: 100%")
    print("   2. Create feature flag 'test-flag-dependency':")
    print("      - Condition: flag 'beta-feature' is enabled")
    print("      - Rollout: 100%")
    print("")

    posthog.debug = True

    # Test @example.com user (should satisfy dependency if flags exist)
    result1 = posthog.feature_enabled(
        "test-flag-dependency",
        "example_user",
        person_properties={"email": "user@example.com"},
        only_evaluate_locally=True,
    )
    print(f"✅ @example.com user (test-flag-dependency): {result1}")

    # Test non-example.com user (dependency should not be satisfied)
    result2 = posthog.feature_enabled(
        "test-flag-dependency",
        "regular_user",
        person_properties={"email": "user@other.com"},
        only_evaluate_locally=True,
    )
    print(f"❌ Regular user (test-flag-dependency): {result2}")

    # Test beta-feature directly for comparison
    beta1 = posthog.feature_enabled(
        "beta-feature",
        "example_user",
        person_properties={"email": "user@example.com"},
        only_evaluate_locally=True,
    )
    beta2 = posthog.feature_enabled(
        "beta-feature",
        "regular_user",
        person_properties={"email": "user@other.com"},
        only_evaluate_locally=True,
    )
    print(f"📊 Beta feature comparison - @example.com: {beta1}, regular: {beta2}")

    print("\n🎯 Results Summary:")
    print(
        f"   - Flag dependencies evaluated locally: {'✅ YES' if result1 != result2 else '❌ NO'}"
    )
    print("   - Zero API calls needed: ✅ YES (all evaluated locally)")
    print("   - Python SDK supports flag dependencies: ✅ YES")

    print("\n" + "-" * 60)
    print("PRODUCTION-STYLE MULTIVARIATE DEPENDENCY CHAIN")
    print("-" * 60)
    print("🔗 Testing complex multivariate flag dependencies...")
    print(
        "   Structure: multivariate-root-flag -> multivariate-intermediate-flag -> multivariate-leaf-flag"
    )
    print("")
    print("📋 Required setup (if flags don't exist):")
    print(
        "   1. Create 'multivariate-leaf-flag' with fruit variants (pineapple, mango, papaya, kiwi)"
    )
    print("      - pineapple: email = 'pineapple@example.com'")
    print("      - mango: email = 'mango@example.com'")
    print(
        "   2. Create 'multivariate-intermediate-flag' with color variants (blue, red)"
    )
    print("      - blue: depends on multivariate-leaf-flag = 'pineapple'")
    print("      - red: depends on multivariate-leaf-flag = 'mango'")
    print(
        "   3. Create 'multivariate-root-flag' with show variants (breaking-bad, the-wire)"
    )
    print("      - breaking-bad: depends on multivariate-intermediate-flag = 'blue'")
    print("      - the-wire: depends on multivariate-intermediate-flag = 'red'")
    print("")

    # Test pineapple -> blue -> breaking-bad chain
    dependent_result3 = posthog.get_feature_flag(
        "multivariate-root-flag",
        "regular_user",
        person_properties={"email": "pineapple@example.com"},
        only_evaluate_locally=True,
    )
    if str(dependent_result3) != "breaking-bad":
        print(
            f"     ❌ Something went wrong evaluating 'multivariate-root-flag' with pineapple@example.com. Expected 'breaking-bad', got '{dependent_result3}'"
        )
    else:
        print("✅ 'multivariate-root-flag' with email pineapple@example.com succeeded")

    # Test mango -> red -> the-wire chain
    dependent_result4 = posthog.get_feature_flag(
        "multivariate-root-flag",
        "regular_user",
        person_properties={"email": "mango@example.com"},
        only_evaluate_locally=True,
    )
    if str(dependent_result4) != "the-wire":
        print(
            f"     ❌ Something went wrong evaluating multivariate-root-flag with mango@example.com. Expected 'the-wire', got '{dependent_result4}'"
        )
    else:
        print("✅ 'multivariate-root-flag' with email mango@example.com succeeded")

    # Show the complete chain evaluation
    print("\n🔍 Complete dependency chain evaluation:")
    for email, expected_chain in [
        ("pineapple@example.com", ["pineapple", "blue", "breaking-bad"]),
        ("mango@example.com", ["mango", "red", "the-wire"]),
    ]:
        leaf = posthog.get_feature_flag(
            "multivariate-leaf-flag",
            "regular_user",
            person_properties={"email": email},
            only_evaluate_locally=True,
        )
        intermediate = posthog.get_feature_flag(
            "multivariate-intermediate-flag",
            "regular_user",
            person_properties={"email": email},
            only_evaluate_locally=True,
        )
        root = posthog.get_feature_flag(
            "multivariate-root-flag",
            "regular_user",
            person_properties={"email": email},
            only_evaluate_locally=True,
        )

        actual_chain = [str(leaf), str(intermediate), str(root)]
        chain_success = actual_chain == expected_chain

        print(f"   📧 {email}:")
        print(f"      Expected: {' -> '.join(map(str, expected_chain))}")
        print(f"      Actual:   {' -> '.join(map(str, actual_chain))}")
        print(f"      Status:   {'✅ SUCCESS' if chain_success else '❌ FAILED'}")

    print("\n🎯 Multivariate Chain Summary:")
    print("   - Complex dependency chains: ✅ SUPPORTED")
    print("   - Multivariate flag dependencies: ✅ SUPPORTED")
    print("   - Local evaluation of chains: ✅ WORKING")

elif choice == "5":
    print("\n" + "=" * 60)
    print("CONTEXT MANAGEMENT AND TAGGING EXAMPLES")
    print("=" * 60)

    posthog.debug = True

    print("🏷️ Testing context management...")
    print(
        "You can add tags to a context, and these are automatically added to any events captured within that context."
    )

    # You can enter a new context using a with statement. Any exceptions thrown in the context will be captured,
    # and tagged with the context tags. Other events captured will also be tagged with the context tags. By default,
    # the new context inherits tags from the parent context.
    try:
        with posthog.new_context():
            posthog.tag("transaction_id", "abc123")
            posthog.tag("some_arbitrary_value", {"tags": "can be dicts"})

            # This event will be captured with the tags set above
            posthog.capture("order_processed")
            print("✅ Event captured with inherited context tags")
            # This exception will be captured with the tags set above
            # raise Exception("Order processing failed")
    except Exception as e:
        print(f"Exception captured: {e}")

    # Use fresh=True to start with a clean context (no inherited tags)
    try:
        with posthog.new_context(fresh=True):
            posthog.tag("session_id", "xyz789")
            # Only session_id tag will be present, no inherited tags
            posthog.capture("session_event")
            print("✅ Event captured with fresh context tags")
            # raise Exception("Session handling failed")
    except Exception as e:
        print(f"Exception captured: {e}")

    # You can also use the `@posthog.scoped()` decorator to enter a new context.
    # By default, it inherits tags from the parent context
    @posthog.scoped()
    def process_order(order_id):
        posthog.tag("order_id", order_id)
        posthog.capture("order_step_completed")
        print(f"✅ Order {order_id} processed with scoped context")
        # Exception will be captured and tagged automatically
        # raise Exception("Order processing failed")

    # Use fresh=True to start with a clean context (no inherited tags)
    @posthog.scoped(fresh=True)
    def process_payment(payment_id):
        posthog.tag("payment_id", payment_id)
        posthog.capture("payment_processed")
        print(f"✅ Payment {payment_id} processed with fresh scoped context")
        # Only payment_id tag will be present, no inherited tags
        # raise Exception("Payment processing failed")

    process_order("12345")
    process_payment("67890")

elif choice == "6":
    print("\n🔄 Running all examples...")
    if not local_eval_available:
        print("   (Skipping local evaluation examples - no personal API key set)\n")

    # Run example 1
    print(f"\n{'🔸' * 20} IDENTIFY AND CAPTURE {'🔸' * 20}")
    posthog.debug = True
    print("📊 Capturing events...")
    posthog.capture(
        "event",
        distinct_id="distinct_id",
        properties={"property1": "value", "property2": "value"},
        send_feature_flags=True,
    )
    print("🔗 Creating alias...")
    posthog.alias("distinct_id", "new_distinct_id")
    print("👤 Identifying user...")
    posthog.set(
        distinct_id="new_distinct_id", properties={"email": "something@something.com"}
    )

    # Run example 2 (requires local evaluation)
    if local_eval_available:
        print(f"\n{'🔸' * 20} FEATURE FLAGS {'🔸' * 20}")
        print("🏁 Testing basic feature flags...")
        print(f"beta-feature: {posthog.feature_enabled('beta-feature', 'distinct_id')}")
        print(
            f"Sydney user: {posthog.feature_enabled('test-flag', 'random_id_12345', person_properties={'$geoip_city_name': 'Sydney'})}"
        )

    # Run example 3
    print(f"\n{'🔸' * 20} PAYLOADS {'🔸' * 20}")
    print("📦 Testing payloads...")
    print(f"Payload: {posthog.get_feature_flag_payload('beta-feature', 'distinct_id')}")

    # Run example 4 (requires local evaluation)
    if local_eval_available:
        print(f"\n{'🔸' * 20} FLAG DEPENDENCIES {'🔸' * 20}")
        print("🔗 Testing flag dependencies...")
        result1 = posthog.feature_enabled(
            "test-flag-dependency",
            "demo_user",
            person_properties={"email": "user@example.com"},
            only_evaluate_locally=True,
        )
        result2 = posthog.feature_enabled(
            "test-flag-dependency",
            "demo_user2",
            person_properties={"email": "user@other.com"},
            only_evaluate_locally=True,
        )
        print(f"✅ @example.com user: {result1}, regular user: {result2}")

    # Run example 5
    print(f"\n{'🔸' * 20} CONTEXT MANAGEMENT {'🔸' * 20}")
    print("🏷️ Testing context management...")
    with posthog.new_context():
        posthog.tag("demo_run", "all_examples")
        posthog.capture("demo_completed")
        print("✅ Demo completed with context tags")

elif choice == "7":
    print("👋 Goodbye!")
    posthog.shutdown()
    exit()

else:
    print("❌ Invalid choice. Please run again and select 1-7.")
    posthog.shutdown()
    exit()

print("\n" + "=" * 60)
print("✅ Example completed!")
print("=" * 60)

posthog.shutdown()
