import atexit
import hashlib
import logging
import numbers
import os
import platform
import sys
import warnings
from datetime import datetime, timedelta
from typing import Any, Optional, Union
from uuid import UUID, uuid4

import distro  # For Linux OS detection
from dateutil.tz import tzutc
from six import string_types

from posthog.consumer import Consumer
from posthog.exception_capture import ExceptionCapture
from posthog.exception_utils import exc_info_from_error, exceptions_from_error_tuple, handle_in_app
from posthog.feature_flags import InconclusiveMatchError, match_feature_flag_properties
from posthog.poller import Poller
from posthog.request import (
    DEFAULT_HOST,
    APIError,
    batch_post,
    decide,
    determine_server_host,
    flags,
    get,
    remote_config,
)
from posthog.types import (
    FeatureFlag,
    FlagMetadata,
    FlagsAndPayloads,
    FlagsResponse,
    FlagValue,
    normalize_flags_response,
    to_flags_and_payloads,
    to_payloads,
    to_values,
)
from posthog.utils import SizeLimitedDict, clean, guess_timezone, remove_trailing_slash
from posthog.version import VERSION

try:
    import queue
except ImportError:
    import Queue as queue


ID_TYPES = (numbers.Number, string_types, UUID)
MAX_DICT_SIZE = 50_000

# TODO: Get rid of these when you're done rolling out `/flags` to all customers
ROLLOUT_PERCENTAGE = 0.1
INCLUDED_HASHES = set({"c4c6803067869081a8c4686780f32de979ade862c6af9ff9ebe5b7161e18362f"})  # this is PostHog's API key
# Explicitly excluding all the API tokens associated with the top 10 customers; we'll get to them soon, but don't want to rollout to them just yet
EXCLUDED_HASHES = set(
    {
        "5fbb169efa185c2a78d43574b01b56c66d7bb594b310f72702e1f167e4e283a9",
        "374be8e6556709787d472e276ebe3c46c0ab4b868ec99f4c96168a44df8307df",
        "6c8a2d5e9dbd4c71854aebca3026fe50045b05e19a16780dccea5439625ee1b4",
        "0f1fa079412bb39b5fce8d96af3539925ede61cbc561ffcd38e27c8e8ae64edb",
        "e3bdce3350e62638ffbf79872c2fd69ef6cbbd35712d9faf735f874cf77ccbfc",
        "f96fe01cdf22f1ec75bc7c897e9605e6431fb5d8f6a8bb9d0e8fce2b0a1384a6",
        "6859b51ac773ea98e146bae47e98759f97ec64c253b9c0524ab56793cc5b6c75",
        "06b28c04e490ce1c9c017396b8b8e16fce1176a8b5de131a99d9af4df1d0fbc9",
        "d9c0afa45a34c9f3c1e615bfa77394b79ad7b434ea46856e3503445d5974d640",
        "320eb50509e2c58a50d80fac848ee0b86290c848a173a0402abdbb760b794595",
        "7380abb65605420dd6e61534c8eecaa6f14d25a6f90ec2edba811f7383123ded",
        "3182881fa027d1c8e4eea108df66dcb0387e375d1e4b551c3a3579fdb1e696d1",
        "d685aeb7d02ec757c4cbe591050a168d34be2f5305d9071d9695ed773057ef16",
        "875ab92bec4da51cf229145565364e98347fafaa2316a4a8e20f5d852bc95aed",
        "4a0d726e4b56d6f6d0407faf5396847146084bbabd042ca0dedba2873d8f9236",
        "a9dc6415c1ccd1874ed1cd303e3d5bf92ddb17ac2af968abed14a51dfb0c53be",
        "5f10a055c9e379869a159306b1d7242fec25584ce895f677f82a13133741c7f1",
        "e3e7608bbda7c15bf82fd7e2945ca74052f8b99e2090962318b6ef983c0ddb16",
        "7f0cbd50e11b475f6c2ed50e620c473e4bfc8df1f4c5174b49ecee1fcec6853e",
        "03004fb2209e6e4186c4364c71e5abc9cf272caf83cf58fb538c42684fd42fb0",
        "8721e8bf608c5eb4d74eeaf26fe588b4e5414742e0494ca7e67a89e1a297332b",
        "ac0d5c7daee8d2f89d5b3861fba0b9a0e560b0eb6944e974f37cdc52274f2d1f",
        "6581d65cf0c4c536122beb5d581ba2b128ed44b7528c07d4ec7837ea33d0cffe",
        "d0c2d4e122ecd4520af7bac133b09fde357622f20aa5a0f7a9328d25c9e9f28e",
        "d09de64bec03c750493b0771c9f2731204bc9a5f0479628848803e2ccded9aca",
        "a9f483f0cdc028a5e05d03d7ab683738f09a940c0173d9e6b004fbe85738a1f5",
        "2ffb5817a9fc465b9bb37b9112393cc1a274185f7f18618192421b7511b98830",
        "a6785a722fdb0f975a1a30302f8312709ae069358c901c609f4898a9ae14bdf2",
        "3d9ba35cab44358cf47c867f48c95f75b9ad54ca5407ed19576da55a085d3a8f",
        "ff59d2907ecb66f4d4a1705435460124a390d8cf7762dc7860d4b4171f832976",
        "aac9e8036d3e0efed49cd5fbea19ea8354c4e1dfc95a1585300c5178189e5bac",
        "1e7fa74813f733e35ea820f8272c6562b4b0c70429f1b549605cc9e8016f632e",
        "2cb74b224cb20b8e5a5a52f3fe5ca62672e5c77ce7f30223698bb4d4abff2293",
        "17a90589bfe29f40f826e2df4753c0bce17a05f4c04b9a0924304e7418aba9e8",
        "0925e4c5bc65ced02c65aa3afba5eaa98aed288d193f719a8fbaebafdeafc1ce",
        "a0308973730b505f1d6af7cd2f39c69bd86ea2a35b9d27118910e1c58d9a6a1a",
        "c780092461636d6d62179723f03cbfe4a7b5808a6b46de749d8b32c3384f1e74",
        "65d6083548c27387f9381ff2aa37581a41ba1d5e6162afdc18cf8130be528052",
        "e2241631d1211e15688735ec6d9f56b4839e65d2095f278630c884bd49f00be8",
        "f2d9e1c10371912c32e9eba18f348782345ff70d383ae8b38bc9e6b12c7841e7",
        "57411b20e1c406ac4339718287b3eaa83635291fb593c9a4068dd08ec1d03692",
        "06e91ecd6b2a9a02234951ab3a5a95aeb84ef34499a5001629aaa13d907ba1dc",
        "4d2f47e99000f6820307e525fcf972421335a86f39b6ada1c93d67410520af49",
        "538d3b1415c3feccbe68d59b5ad9ed35aa418fc64658ff603855494abf75f647",
        "68b11387ac9f805bdbea486b9d3e0724856180646f2b12617a81174d5c27833c",
        "a74797287c3d29f92fc729c2a8b3f17638cb273388e12cb8ffd972bcfbcdfdb8",
        "b53d2b6551ebd8d68321dbd2727a299b1d23ff15853be02fffb0c54f1f0e1349",
        "abad9dc57c9cb9a244b89b11f0a9123baf924a6908443dd8527cf6b411bbb33a",
        "d17b55c7d72052d76d76a039e1ceb613d443401d30eae91ac903a07d5ee0d2d2",
        "274a08018c6e4609dedc37e31aea589c527cd7b93242d305591c3f5313408ee8",
        "75ed9cca6d877ea218647d6021b89c5959156eed2ce4ccad29d4e497d9cd0119",
        "4862317bab4b4efc876a810b92a6841bcf6ba69ac7aa7ff792358862528e7fa8",
        "f0498fff4318e52729573a8bf451d7b978c5242af51ec8b1699798090bc00d32",
        "a6a3435402f66a94eefd07b16297f6b4a61e26992e8ed7742de2e49d7ea71104",
        "72d8ede07d3ef0fd8eb0cd7261d29f4f33b3554e06a726db151138a25a01b539",
        "937c4aae120326c861eb3ec23371e029d3cea21f5849e4d52d75e47e06473e5c",
        "e0138f35502faac574232bbbaab7ad769e2dcd449b596e32454368cb3cc035f9",
        "084e32dc89830d7bb120492ed55cc543de0405c7ae3d0c16c8f64ab07c44506d",
        "d59f0ce1670146019b2c77b56ff8faca6346adfcc93443712a613a89298e3fb9",
        "b99bd54a29c2e9adc17527f9df539415a1c0a83293f72e3e0c8744c5677ea1a1",
        "c252a61d3c19f58062ca9fe2b13dfe378bc11380705cec703d9d8d0a0e167995",
    }
)


def get_os_info():
    """
    Returns standardized OS name and version information.
    Similar to how user agent parsing works in JS.
    """
    os_name = ""
    os_version = ""

    platform_name = sys.platform

    if platform_name.startswith("win"):
        os_name = "Windows"
        if hasattr(platform, "win32_ver"):
            win_version = platform.win32_ver()[0]
            if win_version:
                os_version = win_version

    elif platform_name == "darwin":
        os_name = "Mac OS X"
        if hasattr(platform, "mac_ver"):
            mac_version = platform.mac_ver()[0]
            if mac_version:
                os_version = mac_version

    elif platform_name.startswith("linux"):
        os_name = "Linux"
        linux_info = distro.info()
        if linux_info["version"]:
            os_version = linux_info["version"]

    elif platform_name.startswith("freebsd"):
        os_name = "FreeBSD"
        if hasattr(platform, "release"):
            os_version = platform.release()

    else:
        os_name = platform_name
        if hasattr(platform, "release"):
            os_version = platform.release()

    return os_name, os_version


def system_context() -> dict[str, Any]:
    os_name, os_version = get_os_info()

    return {
        "$python_runtime": platform.python_implementation(),
        "$python_version": "%s.%s.%s" % (sys.version_info[:3]),
        "$os": os_name,
        "$os_version": os_version,
    }


def is_token_in_rollout(
    token: str,
    percentage: float = 0,
    included_hashes: Optional[set[str]] = None,
    excluded_hashes: Optional[set[str]] = None,
) -> bool:
    """
    Determines if a token should be included in a rollout based on:
    1. If its hash matches any included_hashes provided
    2. If its hash falls within the percentage rollout

    Args:
        token: String to hash (usually API key)
        percentage: Float between 0 and 1 representing rollout percentage
        included_hashes: Optional set of specific SHA1 hashes to match against
        excluded_hashes: Optional set of specific SHA1 hashes to exclude from rollout
    Returns:
        bool: True if token should be included in rollout
    """
    # First generate SHA1 hash of token
    token_hash = hashlib.sha1(token.encode("utf-8")).hexdigest()

    # Check if hash matches any included hashes
    if included_hashes and token_hash in included_hashes:
        return True

    # Check if hash matches any excluded hashes
    if excluded_hashes and token_hash in excluded_hashes:
        return False

    # Convert first 8 chars of hash to int and divide by max value to get number between 0-1
    hash_int = int(token_hash[:8], 16)
    hash_float = hash_int / 0xFFFFFFFF

    return hash_float < percentage


class Client(object):
    """Create a new PostHog client."""

    log = logging.getLogger("posthog")

    def __init__(
        self,
        api_key=None,
        host=None,
        debug=False,
        max_queue_size=10000,
        send=True,
        on_error=None,
        flush_at=100,
        flush_interval=0.5,
        gzip=False,
        max_retries=3,
        sync_mode=False,
        timeout=15,
        thread=1,
        poll_interval=30,
        personal_api_key=None,
        project_api_key=None,
        disabled=False,
        disable_geoip=True,
        historical_migration=False,
        feature_flags_request_timeout_seconds=3,
        super_properties=None,
        enable_exception_autocapture=False,
        log_captured_exceptions=False,
        exception_autocapture_integrations=None,
        project_root=None,
        privacy_mode=False,
    ):
        self.queue = queue.Queue(max_queue_size)

        # api_key: This should be the Team API Key (token), public
        self.api_key = project_api_key or api_key

        require("api_key", self.api_key, string_types)

        self.on_error = on_error
        self.debug = debug
        self.send = send
        self.sync_mode = sync_mode
        # Used for session replay URL generation - we don't want the server host here.
        self.raw_host = host or DEFAULT_HOST
        self.host = determine_server_host(host)
        self.gzip = gzip
        self.timeout = timeout
        self._feature_flags = None  # private variable to store flags
        self.feature_flags_by_key = None
        self.group_type_mapping = None
        self.cohorts = None
        self.poll_interval = poll_interval
        self.feature_flags_request_timeout_seconds = feature_flags_request_timeout_seconds
        self.poller = None
        self.distinct_ids_feature_flags_reported = SizeLimitedDict(MAX_DICT_SIZE, set)
        self.disabled = disabled
        self.disable_geoip = disable_geoip
        self.historical_migration = historical_migration
        self.super_properties = super_properties
        self.enable_exception_autocapture = enable_exception_autocapture
        self.log_captured_exceptions = log_captured_exceptions
        self.exception_autocapture_integrations = exception_autocapture_integrations
        self.exception_capture = None
        self.privacy_mode = privacy_mode

        if project_root is None:
            try:
                project_root = os.getcwd()
            except Exception:
                project_root = None

        self.project_root = project_root

        # personal_api_key: This should be a generated Personal API Key, private
        self.personal_api_key = personal_api_key
        if debug:
            # Ensures that debug level messages are logged when debug mode is on.
            # Otherwise, defaults to WARNING level. See https://docs.python.org/3/howto/logging.html#what-happens-if-no-configuration-is-provided
            logging.basicConfig()
            self.log.setLevel(logging.DEBUG)
        else:
            self.log.setLevel(logging.WARNING)

        if self.enable_exception_autocapture:
            self.exception_capture = ExceptionCapture(self, integrations=self.exception_autocapture_integrations)

        if sync_mode:
            self.consumers = None
        else:
            # On program exit, allow the consumer thread to exit cleanly.
            # This prevents exceptions and a messy shutdown when the
            # interpreter is destroyed before the daemon thread finishes
            # execution. However, it is *not* the same as flushing the queue!
            # To guarantee all messages have been delivered, you'll still need
            # to call flush().
            if send:
                atexit.register(self.join)
            for n in range(thread):
                self.consumers = []
                consumer = Consumer(
                    self.queue,
                    self.api_key,
                    host=self.host,
                    on_error=on_error,
                    flush_at=flush_at,
                    flush_interval=flush_interval,
                    gzip=gzip,
                    retries=max_retries,
                    timeout=timeout,
                    historical_migration=historical_migration,
                )
                self.consumers.append(consumer)

                # if we've disabled sending, just don't start the consumer
                if send:
                    consumer.start()

    @property
    def feature_flags(self):
        """
        Get the local evaluation feature flags.
        """
        return self._feature_flags

    @feature_flags.setter
    def feature_flags(self, flags):
        """
        Set the local evaluation feature flags.
        """
        self._feature_flags = flags or []
        self.feature_flags_by_key = {flag["key"]: flag for flag in self._feature_flags if flag.get("key") is not None}
        assert (
            self.feature_flags_by_key is not None
        ), "feature_flags_by_key should be initialized when feature_flags is set"

    def identify(self, distinct_id=None, properties=None, context=None, timestamp=None, uuid=None, disable_geoip=None):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        properties = properties or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set": properties,
            "event": "$identify",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def get_feature_variants(
        self, distinct_id, groups=None, person_properties=None, group_properties=None, disable_geoip=None
    ) -> dict[str, Union[bool, str]]:
        """
        Get feature flag variants for a distinct_id by calling decide.
        """
        resp_data = self.get_flags_decision(distinct_id, groups, person_properties, group_properties, disable_geoip)
        return to_values(resp_data) or {}

    def get_feature_payloads(
        self, distinct_id, groups=None, person_properties=None, group_properties=None, disable_geoip=None
    ) -> dict[str, str]:
        """
        Get feature flag payloads for a distinct_id by calling decide.
        """
        resp_data = self.get_flags_decision(distinct_id, groups, person_properties, group_properties, disable_geoip)
        return to_payloads(resp_data) or {}

    def get_feature_flags_and_payloads(
        self, distinct_id, groups=None, person_properties=None, group_properties=None, disable_geoip=None
    ) -> FlagsAndPayloads:
        """
        Get feature flags and payloads for a distinct_id by calling decide.
        """
        resp = self.get_flags_decision(distinct_id, groups, person_properties, group_properties, disable_geoip)
        return to_flags_and_payloads(resp)

    def get_flags_decision(
        self, distinct_id, groups=None, person_properties=None, group_properties=None, disable_geoip=None
    ) -> FlagsResponse:
        """
        Get feature flags decision, using either flags() or decide() API based on rollout.
        """
        require("distinct_id", distinct_id, ID_TYPES)

        if disable_geoip is None:
            disable_geoip = self.disable_geoip

        if groups:
            require("groups", groups, dict)
        else:
            groups = {}

        request_data = {
            "distinct_id": distinct_id,
            "groups": groups,
            "person_properties": person_properties,
            "group_properties": group_properties,
            "disable_geoip": disable_geoip,
        }

        use_flags = is_token_in_rollout(
            self.api_key, ROLLOUT_PERCENTAGE, included_hashes=INCLUDED_HASHES, excluded_hashes=EXCLUDED_HASHES
        )

        if use_flags:
            resp_data = flags(
                self.api_key, self.host, timeout=self.feature_flags_request_timeout_seconds, **request_data
            )
        else:
            resp_data = decide(
                self.api_key, self.host, timeout=self.feature_flags_request_timeout_seconds, **request_data
            )

        return normalize_flags_response(resp_data)

    def capture(
        self,
        distinct_id=None,
        event=None,
        properties=None,
        context=None,
        timestamp=None,
        uuid=None,
        groups=None,
        send_feature_flags=False,
        disable_geoip=None,
    ):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        properties = {**(properties or {}), **system_context()}

        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)
        require("event", event, string_types)

        msg = {
            "properties": properties,
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "event": event,
            "uuid": uuid,
        }

        if groups:
            require("groups", groups, dict)
            msg["properties"]["$groups"] = groups

        extra_properties: dict[str, Any] = {}
        feature_variants: Optional[dict[str, Union[bool, str]]] = {}
        if send_feature_flags:
            try:
                feature_variants = self.get_feature_variants(distinct_id, groups, disable_geoip=disable_geoip)
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get feature variants: {e}")

        elif self.feature_flags and event != "$feature_flag_called":
            # Local evaluation is enabled, flags are loaded, so try and get all flags we can without going to the server
            feature_variants = self.get_all_flags(
                distinct_id, groups=(groups or {}), disable_geoip=disable_geoip, only_evaluate_locally=True
            )

        for feature, variant in (feature_variants or {}).items():
            extra_properties[f"$feature/{feature}"] = variant

        active_feature_flags = [key for (key, value) in (feature_variants or {}).items() if value is not False]
        if active_feature_flags:
            extra_properties["$active_feature_flags"] = active_feature_flags

        if extra_properties:
            msg["properties"] = {**extra_properties, **msg["properties"]}

        return self._enqueue(msg, disable_geoip)

    def set(self, distinct_id=None, properties=None, context=None, timestamp=None, uuid=None, disable_geoip=None):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        properties = properties or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set": properties,
            "event": "$set",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def set_once(self, distinct_id=None, properties=None, context=None, timestamp=None, uuid=None, disable_geoip=None):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        properties = properties or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        msg = {
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "$set_once": properties,
            "event": "$set_once",
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def group_identify(
        self,
        group_type=None,
        group_key=None,
        properties=None,
        context=None,
        timestamp=None,
        uuid=None,
        disable_geoip=None,
        distinct_id=None,
    ):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )
        properties = properties or {}
        require("group_type", group_type, ID_TYPES)
        require("group_key", group_key, ID_TYPES)
        require("properties", properties, dict)

        if distinct_id:
            require("distinct_id", distinct_id, ID_TYPES)
        else:
            distinct_id = "${}_{}".format(group_type, group_key)

        msg = {
            "event": "$groupidentify",
            "properties": {
                "$group_type": group_type,
                "$group_key": group_key,
                "$group_set": properties,
            },
            "distinct_id": distinct_id,
            "timestamp": timestamp,
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def alias(self, previous_id=None, distinct_id=None, context=None, timestamp=None, uuid=None, disable_geoip=None):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        require("previous_id", previous_id, ID_TYPES)
        require("distinct_id", distinct_id, ID_TYPES)

        msg = {
            "properties": {
                "distinct_id": previous_id,
                "alias": distinct_id,
            },
            "timestamp": timestamp,
            "event": "$create_alias",
            "distinct_id": previous_id,
        }

        return self._enqueue(msg, disable_geoip)

    def page(
        self, distinct_id=None, url=None, properties=None, context=None, timestamp=None, uuid=None, disable_geoip=None
    ):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        properties = properties or {}
        require("distinct_id", distinct_id, ID_TYPES)
        require("properties", properties, dict)

        require("url", url, string_types)
        properties["$current_url"] = url

        msg = {
            "event": "$pageview",
            "properties": properties,
            "timestamp": timestamp,
            "distinct_id": distinct_id,
            "uuid": uuid,
        }

        return self._enqueue(msg, disable_geoip)

    def capture_exception(
        self,
        exception=None,
        distinct_id=None,
        properties=None,
        context=None,
        timestamp=None,
        uuid=None,
        groups=None,
        **kwargs,
    ):
        if context is not None:
            warnings.warn(
                "The 'context' parameter is deprecated and will be removed in a future version.",
                DeprecationWarning,
                stacklevel=2,
            )

        # this function shouldn't ever throw an error, so it logs exceptions instead of raising them.
        # this is important to ensure we don't unexpectedly re-raise exceptions in the user's code.
        try:
            properties = properties or {}

            # if there's no distinct_id, we'll generate one and set personless mode
            # via $process_person_profile = false
            if distinct_id is None:
                properties["$process_person_profile"] = False
                distinct_id = uuid4()

            require("distinct_id", distinct_id, ID_TYPES)
            require("properties", properties, dict)

            if exception is not None:
                exc_info = exc_info_from_error(exception)
            else:
                exc_info = sys.exc_info()

            if exc_info is None or exc_info == (None, None, None):
                self.log.warning("No exception information available")
                return

            # Format stack trace for cymbal
            all_exceptions_with_trace = exceptions_from_error_tuple(exc_info)

            # Add in-app property to frames in the exceptions
            event = handle_in_app(
                {
                    "exception": {
                        "values": all_exceptions_with_trace,
                    },
                },
                project_root=self.project_root,
            )
            all_exceptions_with_trace_and_in_app = event["exception"]["values"]

            properties = {
                "$exception_type": all_exceptions_with_trace_and_in_app[0].get("type"),
                "$exception_message": all_exceptions_with_trace_and_in_app[0].get("value"),
                "$exception_list": all_exceptions_with_trace_and_in_app,
                "$exception_personURL": f"{remove_trailing_slash(self.raw_host)}/project/{self.api_key}/person/{distinct_id}",
                **properties,
            }

            if self.log_captured_exceptions:
                self.log.exception(exception, extra=kwargs)

            return self.capture(distinct_id, "$exception", properties, context, timestamp, uuid, groups)
        except Exception as e:
            self.log.exception(f"Failed to capture exception: {e}")

    def _enqueue(self, msg, disable_geoip):
        """Push a new `msg` onto the queue, return `(success, msg)`"""

        if self.disabled:
            return False, "disabled"

        timestamp = msg["timestamp"]
        if timestamp is None:
            timestamp = datetime.now(tz=tzutc())

        require("timestamp", timestamp, datetime)

        # add common
        timestamp = guess_timezone(timestamp)
        msg["timestamp"] = timestamp.isoformat()

        # only send if "uuid" is truthy
        if "uuid" in msg:
            uuid = msg.pop("uuid")
            if uuid:
                msg["uuid"] = stringify_id(uuid)

        if not msg.get("properties"):
            msg["properties"] = {}
        msg["properties"]["$lib"] = "posthog-python"
        msg["properties"]["$lib_version"] = VERSION

        if disable_geoip is None:
            disable_geoip = self.disable_geoip

        if disable_geoip:
            msg["properties"]["$geoip_disable"] = True

        if self.super_properties:
            msg["properties"] = {**msg["properties"], **self.super_properties}

        msg["distinct_id"] = stringify_id(msg.get("distinct_id", None))

        msg = clean(msg)
        self.log.debug("queueing: %s", msg)

        # if send is False, return msg as if it was successfully queued
        if not self.send:
            return True, msg

        if self.sync_mode:
            self.log.debug("enqueued with blocking %s.", msg["event"])
            batch_post(
                self.api_key,
                self.host,
                gzip=self.gzip,
                timeout=self.timeout,
                batch=[msg],
                historical_migration=self.historical_migration,
            )

            return True, msg

        try:
            self.queue.put(msg, block=False)
            self.log.debug("enqueued %s.", msg["event"])
            return True, msg
        except queue.Full:
            self.log.warning("analytics-python queue is full")
            return False, msg

    def flush(self):
        """Forces a flush from the internal queue to the server"""
        queue = self.queue
        size = queue.qsize()
        queue.join()
        # Note that this message may not be precise, because of threading.
        self.log.debug("successfully flushed about %s items.", size)

    def join(self):
        """Ends the consumer thread once the queue is empty.
        Blocks execution until finished
        """
        for consumer in self.consumers:
            consumer.pause()
            try:
                consumer.join()
            except RuntimeError:
                # consumer thread has not started
                pass

        if self.poller:
            self.poller.stop()

    def shutdown(self):
        """Flush all messages and cleanly shutdown the client"""
        self.flush()
        self.join()

        if self.exception_capture:
            self.exception_capture.close()

    def _load_feature_flags(self):
        try:
            response = get(
                self.personal_api_key,
                f"/api/feature_flag/local_evaluation/?token={self.api_key}&send_cohorts",
                self.host,
                timeout=10,
            )

            self.feature_flags = response["flags"] or []
            self.group_type_mapping = response["group_type_mapping"] or {}
            self.cohorts = response["cohorts"] or {}

        except APIError as e:
            if e.status == 401:
                self.log.error(
                    "[FEATURE FLAGS] Error loading feature flags: To use feature flags, please set a valid personal_api_key. More information: https://posthog.com/docs/api/overview"
                )
                if self.debug:
                    raise APIError(
                        status=401,
                        message="You are using a write-only key with feature flags. "
                        "To use feature flags, please set a personal_api_key "
                        "More information: https://posthog.com/docs/api/overview",
                    )
            elif e.status == 402:
                self.log.warning(
                    "[FEATURE FLAGS] PostHog feature flags quota limited, resetting feature flag data.  Learn more about billing limits at https://posthog.com/docs/billing/limits-alerts"
                )
                # Reset all feature flag data when quota limited
                self.feature_flags = []
                self.group_type_mapping = {}
                self.cohorts = {}

                if self.debug:
                    raise APIError(
                        status=402,
                        message="PostHog feature flags quota limited",
                    )
            else:
                self.log.error(f"[FEATURE FLAGS] Error loading feature flags: {e}")
        except Exception as e:
            self.log.warning(
                "[FEATURE FLAGS] Fetching feature flags failed with following error. We will retry in %s seconds."
                % self.poll_interval
            )
            self.log.warning(e)

        self._last_feature_flag_poll = datetime.now(tz=tzutc())

    def load_feature_flags(self):
        if not self.personal_api_key:
            self.log.warning("[FEATURE FLAGS] You have to specify a personal_api_key to use feature flags.")
            self.feature_flags = []
            return

        self._load_feature_flags()
        if not (self.poller and self.poller.is_alive()):
            self.poller = Poller(interval=timedelta(seconds=self.poll_interval), execute=self._load_feature_flags)
            self.poller.start()

    def _compute_flag_locally(
        self,
        feature_flag,
        distinct_id,
        *,
        groups={},
        person_properties={},
        group_properties={},
        warn_on_unknown_groups=True,
    ) -> FlagValue:
        if feature_flag.get("ensure_experience_continuity", False):
            raise InconclusiveMatchError("Flag has experience continuity enabled")

        if not feature_flag.get("active"):
            return False

        flag_filters = feature_flag.get("filters") or {}
        aggregation_group_type_index = flag_filters.get("aggregation_group_type_index")
        if aggregation_group_type_index is not None:
            group_name = self.group_type_mapping.get(str(aggregation_group_type_index))

            if not group_name:
                self.log.warning(
                    f"[FEATURE FLAGS] Unknown group type index {aggregation_group_type_index} for feature flag {feature_flag['key']}"
                )
                # failover to `/decide/`
                raise InconclusiveMatchError("Flag has unknown group type index")

            if group_name not in groups:
                # Group flags are never enabled in `groups` aren't passed in
                # don't failover to `/decide/`, since response will be the same
                if warn_on_unknown_groups:
                    self.log.warning(
                        f"[FEATURE FLAGS] Can't compute group feature flag: {feature_flag['key']} without group names passed in"
                    )
                else:
                    self.log.debug(
                        f"[FEATURE FLAGS] Can't compute group feature flag: {feature_flag['key']} without group names passed in"
                    )
                return False

            focused_group_properties = group_properties[group_name]
            return match_feature_flag_properties(feature_flag, groups[group_name], focused_group_properties)
        else:
            return match_feature_flag_properties(feature_flag, distinct_id, person_properties, self.cohorts)

    def feature_enabled(
        self,
        key,
        distinct_id,
        *,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ):
        response = self.get_feature_flag(
            key,
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            send_feature_flag_events=send_feature_flag_events,
            disable_geoip=disable_geoip,
        )

        if response is None:
            return None
        return bool(response)

    def get_feature_flag(
        self,
        key,
        distinct_id,
        *,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ) -> Optional[FlagValue]:
        """
        Get a feature flag value for a key by evaluating locally or remotely
        depending on whether local evaluation is enabled and the flag can be
        locally evaluated.

        This also captures the $feature_flag_called event unless send_feature_flag_events is False.
        """
        require("key", key, string_types)
        require("distinct_id", distinct_id, ID_TYPES)
        require("groups", groups, dict)

        if self.disabled:
            return None

        person_properties, group_properties = self._add_local_person_and_group_properties(
            distinct_id, groups, person_properties, group_properties
        )

        response = self._locally_evaluate_flag(key, distinct_id, groups, person_properties, group_properties)

        flag_details = None
        request_id = None

        flag_was_locally_evaluated = response is not None
        if not flag_was_locally_evaluated and not only_evaluate_locally:
            try:
                flag_details, request_id = self._get_feature_flag_details_from_decide(
                    key, distinct_id, groups, person_properties, group_properties, disable_geoip
                )
                response = flag_details.get_value() if flag_details else False
                self.log.debug(f"Successfully computed flag remotely: #{key} -> #{response}")
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get flag remotely: {e}")

        if send_feature_flag_events:
            self._capture_feature_flag_called(
                distinct_id,
                key,
                response or False,
                None,
                flag_was_locally_evaluated,
                groups,
                disable_geoip,
                request_id,
                flag_details,
            )

        return response

    def _locally_evaluate_flag(
        self,
        key: str,
        distinct_id: str,
        groups: dict[str, str],
        person_properties: dict[str, str],
        group_properties: dict[str, str],
    ) -> Optional[FlagValue]:
        if self.feature_flags is None and self.personal_api_key:
            self.load_feature_flags()
        response = None

        if self.feature_flags:
            assert (
                self.feature_flags_by_key is not None
            ), "feature_flags_by_key should be initialized when feature_flags is set"
            # Local evaluation
            flag = self.feature_flags_by_key.get(key)
            if flag:
                try:
                    response = self._compute_flag_locally(
                        flag,
                        distinct_id,
                        groups=groups,
                        person_properties=person_properties,
                        group_properties=group_properties,
                    )
                    self.log.debug(f"Successfully computed flag locally: {key} -> {response}")
                except InconclusiveMatchError as e:
                    self.log.debug(f"Failed to compute flag {key} locally: {e}")
                except Exception as e:
                    self.log.exception(f"[FEATURE FLAGS] Error while computing variant locally: {e}")
        return response

    def get_feature_flag_payload(
        self,
        key,
        distinct_id,
        *,
        match_value=None,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
        send_feature_flag_events=True,
        disable_geoip=None,
    ):
        if self.disabled:
            return None

        if match_value is None:
            person_properties, group_properties = self._add_local_person_and_group_properties(
                distinct_id, groups, person_properties, group_properties
            )
            match_value = self._locally_evaluate_flag(key, distinct_id, groups, person_properties, group_properties)

        response = None
        payload = None
        flag_details = None
        request_id = None

        if match_value is not None:
            payload = self._compute_payload_locally(key, match_value)

        flag_was_locally_evaluated = payload is not None
        if not flag_was_locally_evaluated and not only_evaluate_locally:
            try:
                flag_details, request_id = self._get_feature_flag_details_from_decide(
                    key, distinct_id, groups, person_properties, group_properties, disable_geoip
                )
                payload = flag_details.metadata.payload if flag_details else None
                response = flag_details.get_value() if flag_details else False
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get feature flags and payloads: {e}")

        if send_feature_flag_events:
            self._capture_feature_flag_called(
                distinct_id,
                key,
                response or False,
                payload,
                flag_was_locally_evaluated,
                groups,
                disable_geoip,
                request_id,
                flag_details,
            )

        return payload

    def _get_feature_flag_details_from_decide(
        self,
        key: str,
        distinct_id: str,
        groups: dict[str, str],
        person_properties: dict[str, str],
        group_properties: dict[str, str],
        disable_geoip: Optional[bool],
    ) -> tuple[Optional[FeatureFlag], Optional[str]]:
        """
        Calls /decide and returns the flag details and request id
        """
        resp_data = self.get_flags_decision(distinct_id, groups, person_properties, group_properties, disable_geoip)
        request_id = resp_data.get("requestId")
        flags = resp_data.get("flags")
        flag_details = flags.get(key) if flags else None
        return flag_details, request_id

    def _capture_feature_flag_called(
        self,
        distinct_id: str,
        key: str,
        response: FlagValue,
        payload: Optional[str],
        flag_was_locally_evaluated: bool,
        groups: dict[str, str],
        disable_geoip: Optional[bool],
        request_id: Optional[str],
        flag_details: Optional[FeatureFlag],
    ):
        feature_flag_reported_key = f"{key}_{str(response)}"

        if feature_flag_reported_key not in self.distinct_ids_feature_flags_reported[distinct_id]:
            properties: dict[str, Any] = {
                "$feature_flag": key,
                "$feature_flag_response": response,
                "locally_evaluated": flag_was_locally_evaluated,
                f"$feature/{key}": response,
            }

            if payload:
                properties["$feature_flag_payload"] = payload

            if request_id:
                properties["$feature_flag_request_id"] = request_id
            if isinstance(flag_details, FeatureFlag):
                if flag_details.reason and flag_details.reason.description:
                    properties["$feature_flag_reason"] = flag_details.reason.description
                if isinstance(flag_details.metadata, FlagMetadata):
                    if flag_details.metadata.version:
                        properties["$feature_flag_version"] = flag_details.metadata.version
                    if flag_details.metadata.id:
                        properties["$feature_flag_id"] = flag_details.metadata.id

            self.capture(
                distinct_id,
                "$feature_flag_called",
                properties,
                groups=groups,
                disable_geoip=disable_geoip,
            )
            self.distinct_ids_feature_flags_reported[distinct_id].add(feature_flag_reported_key)

    def get_remote_config_payload(self, key: str):
        if self.disabled:
            return None

        if self.personal_api_key is None:
            self.log.warning(
                "[FEATURE FLAGS] You have to specify a personal_api_key to fetch decrypted feature flag payloads."
            )
            return None

        try:
            return remote_config(
                self.personal_api_key,
                self.host,
                key,
                timeout=self.feature_flags_request_timeout_seconds,
            )
        except Exception as e:
            self.log.exception(f"[FEATURE FLAGS] Unable to get decrypted feature flag payload: {e}")

    def _compute_payload_locally(self, key: str, match_value: FlagValue) -> Optional[str]:
        payload = None

        if self.feature_flags_by_key is None:
            return payload

        flag_definition = self.feature_flags_by_key.get(key)
        if flag_definition:
            flag_filters = flag_definition.get("filters") or {}
            flag_payloads = flag_filters.get("payloads") or {}
            # For boolean flags, convert True to "true"
            # For multivariate flags, use the variant string as-is
            lookup_value = "true" if isinstance(match_value, bool) and match_value else str(match_value)
            payload = flag_payloads.get(lookup_value, None)
        return payload

    def get_all_flags(
        self,
        distinct_id,
        *,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
        disable_geoip=None,
    ) -> Optional[dict[str, Union[bool, str]]]:
        response = self.get_all_flags_and_payloads(
            distinct_id,
            groups=groups,
            person_properties=person_properties,
            group_properties=group_properties,
            only_evaluate_locally=only_evaluate_locally,
            disable_geoip=disable_geoip,
        )

        return response["featureFlags"]

    def get_all_flags_and_payloads(
        self,
        distinct_id,
        *,
        groups={},
        person_properties={},
        group_properties={},
        only_evaluate_locally=False,
        disable_geoip=None,
    ) -> FlagsAndPayloads:
        if self.disabled:
            return {"featureFlags": None, "featureFlagPayloads": None}

        person_properties, group_properties = self._add_local_person_and_group_properties(
            distinct_id, groups, person_properties, group_properties
        )

        response, fallback_to_decide = self._get_all_flags_and_payloads_locally(
            distinct_id, groups=groups, person_properties=person_properties, group_properties=group_properties
        )

        if fallback_to_decide and not only_evaluate_locally:
            try:
                decide_response = self.get_flags_decision(
                    distinct_id,
                    groups=groups,
                    person_properties=person_properties,
                    group_properties=group_properties,
                    disable_geoip=disable_geoip,
                )
                return to_flags_and_payloads(decide_response)
            except Exception as e:
                self.log.exception(f"[FEATURE FLAGS] Unable to get feature flags and payloads: {e}")

        return response

    def _get_all_flags_and_payloads_locally(
        self, distinct_id, *, groups={}, person_properties={}, group_properties={}, warn_on_unknown_groups=False
    ) -> tuple[FlagsAndPayloads, bool]:
        require("distinct_id", distinct_id, ID_TYPES)
        require("groups", groups, dict)

        if self.feature_flags is None and self.personal_api_key:
            self.load_feature_flags()

        flags: dict[str, FlagValue] = {}
        payloads: dict[str, str] = {}
        fallback_to_decide = False
        # If loading in previous line failed
        if self.feature_flags:
            for flag in self.feature_flags:
                try:
                    flags[flag["key"]] = self._compute_flag_locally(
                        flag,
                        distinct_id,
                        groups=groups,
                        person_properties=person_properties,
                        group_properties=group_properties,
                        warn_on_unknown_groups=warn_on_unknown_groups,
                    )
                    matched_payload = self._compute_payload_locally(flag["key"], flags[flag["key"]])
                    if matched_payload:
                        payloads[flag["key"]] = matched_payload
                except InconclusiveMatchError:
                    # No need to log this, since it's just telling us to fall back to `/decide`
                    fallback_to_decide = True
                except Exception as e:
                    self.log.exception(f"[FEATURE FLAGS] Error while computing variant and payload: {e}")
                    fallback_to_decide = True
        else:
            fallback_to_decide = True

        return {"featureFlags": flags, "featureFlagPayloads": payloads}, fallback_to_decide

    def feature_flag_definitions(self):
        return self.feature_flags

    def _add_local_person_and_group_properties(self, distinct_id, groups, person_properties, group_properties):
        all_person_properties = {"distinct_id": distinct_id, **(person_properties or {})}

        all_group_properties = {}
        if groups:
            for group_name in groups:
                all_group_properties[group_name] = {
                    "$group_key": groups[group_name],
                    **(group_properties.get(group_name) or {}),
                }

        return all_person_properties, all_group_properties


def require(name, field, data_type):
    """Require that the named `field` has the right `data_type`"""
    if not isinstance(field, data_type):
        msg = "{0} must have {1}, got: {2}".format(name, data_type, field)
        raise AssertionError(msg)


def stringify_id(val):
    if val is None:
        return None
    if isinstance(val, string_types):
        return val
    return str(val)
