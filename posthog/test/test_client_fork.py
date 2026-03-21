import os
import gc
import unittest
import warnings
import weakref
from unittest import mock

from parameterized import parameterized

import posthog.request
from posthog.client import Client
from posthog.test.test_utils import FAKE_TEST_API_KEY
from posthog.utils import FlagCache, RedisFlagCache


class TestClientFork(unittest.TestCase):
    def test_redis_cache_reinit_logic(self):
        mock_redis_module = mock.MagicMock()
        mock_redis_client = mock.MagicMock()
        mock_redis_module.from_url.return_value = mock_redis_client

        with mock.patch.dict("sys.modules", {"redis": mock_redis_module}):
            client = Client(
                project_api_key="test_key",
                flag_fallback_cache_url="redis://localhost:6379/0",
                send=False,
            )

            self.assertIsInstance(client.flag_cache, RedisFlagCache)
            old_cache = client.flag_cache

            new_cache = RedisFlagCache(mock_redis_client)
            with mock.patch.object(client, "_initialize_flag_cache", return_value=new_cache) as mock_init:
                client._reinit_after_fork()

                mock_init.assert_called_with("redis://localhost:6379/0")

                self.assertIs(client.flag_cache, new_cache)
                self.assertIsNot(client.flag_cache, old_cache)

    def test_reinit_after_fork_keeps_memory_cache_instance(self):
        client = Client(
            project_api_key="test_key",
            flag_fallback_cache_url="memory://local/?ttl=300&size=10000",
            send=False,
        )
        old_cache = client.flag_cache

        with mock.patch.object(client, "_initialize_flag_cache") as mock_init:
            client._reinit_after_fork()

        self.assertIs(client.flag_cache, old_cache)
        mock_init.assert_not_called()

    @mock.patch("posthog.client.Poller")
    def test_reinit_after_fork_restarts_poller_when_enabled(self, mock_poller):
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key=FAKE_TEST_API_KEY,
            send=False,
            enable_local_evaluation=True,
            poll_interval=123,
        )
        old_poller = mock.Mock()
        new_poller = mock.Mock()
        mock_poller.return_value = new_poller
        client.poller = old_poller

        client._reinit_after_fork()

        mock_poller.assert_called_once_with(
            interval=mock.ANY,
            execute=client._load_feature_flags,
        )
        self.assertIs(client.poller, new_poller)
        self.assertIsNot(client.poller, old_poller)
        new_poller.start.assert_called_once_with()

    def test_reinit_after_fork_clears_poller_when_local_evaluation_disabled(self):
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key=FAKE_TEST_API_KEY,
            send=False,
            enable_local_evaluation=False,
        )
        client.poller = mock.Mock()

        client._reinit_after_fork()

        self.assertIsNone(client.poller)

    @mock.patch("posthog.client.reset_sessions")
    def test_reinit_after_fork_resets_sessions(self, mock_reset_sessions):
        client = Client(FAKE_TEST_API_KEY, send=False)

        client._reinit_after_fork()

        mock_reset_sessions.assert_called_once_with()

    @mock.patch("posthog.client.os.register_at_fork")
    def test_registers_at_fork_hook(self, mock_register_at_fork):
        client = Client(FAKE_TEST_API_KEY, send=False)

        mock_register_at_fork.assert_called_once()
        after_in_child = mock_register_at_fork.call_args.kwargs["after_in_child"]

        with mock.patch.object(client, "_reinit_after_fork") as mock_reinit:
            after_in_child()
            mock_reinit.assert_called_once()

    @mock.patch("posthog.client.os.register_at_fork")
    def test_register_at_fork_noop_after_client_gc(self, mock_register_at_fork):
        with mock.patch.object(Client, "_reinit_after_fork") as mock_reinit:
            client = Client(FAKE_TEST_API_KEY, send=False)
            after_in_child = mock_register_at_fork.call_args.kwargs["after_in_child"]
            client_ref = weakref.ref(client)

            del client
            gc.collect()

            self.assertIsNone(client_ref())
            after_in_child()
            mock_reinit.assert_not_called()

    @parameterized.expand([(True, 1), (False, 0)])
    def test_reinit_after_fork_replaces_queue_and_consumers(self, send, expected_starts):
        with mock.patch("posthog.client.Consumer.start") as mock_start:
            client = Client(FAKE_TEST_API_KEY, send=send, thread=1)
            mock_start.reset_mock()

            old_queue = client.queue
            old_consumers = list(client.consumers)

            client._reinit_after_fork()

            self.assertIsNot(client.queue, old_queue)
            self.assertEqual(len(client.consumers), len(old_consumers))
            self.assertIsNot(client.consumers[0], old_consumers[0])
            self.assertIs(client.consumers[0].queue, client.queue)
            self.assertEqual(mock_start.call_count, expected_starts)

    def test_reinit_after_fork_noop_for_sync_mode(self):
        client = Client(FAKE_TEST_API_KEY, sync_mode=True)
        old_queue = client.queue

        client._reinit_after_fork()

        self.assertIs(client.queue, old_queue)


@unittest.skipUnless(
    hasattr(os, "fork") and hasattr(os, "register_at_fork"),
    "requires os.fork and os.register_at_fork",
)
class TestClientForkEndToEnd(unittest.TestCase):
    def _run_fork_probe(self, child_probe):
        warnings.filterwarnings("ignore", category=DeprecationWarning)

        read_fd, write_fd = os.pipe()
        pid = os.fork()

        if pid == 0:
            try:
                os.write(write_fd, child_probe().encode())
            except Exception as error:
                import traceback

                traceback.print_exc()
                os.write(write_fd, f"exception: {error}".encode())
            finally:
                os.close(write_fd)
                os.close(read_fd)
                os._exit(0)

        os.close(write_fd)
        result = os.read(read_fd, 4096)
        os.close(read_fd)
        _, status = os.waitpid(pid, 0)

        return status, result.decode()

    def test_register_at_fork_preserves_memory_cache_in_child_process(self):
        client = Client(
            project_api_key="test_key",
            flag_fallback_cache_url="memory://local/?ttl=300&size=10000",
            send=False,
        )
        client.flag_cache.set_cached_flag("user_1", "flag_a", "value_a", 1)

        self.assertIsInstance(client.flag_cache, FlagCache)
        self.assertEqual(client.flag_cache.get_cached_flag("user_1", "flag_a", 1), "value_a")

        def child_probe():
            value = client.flag_cache.get_cached_flag("user_1", "flag_a", 1)
            return "ok" if value == "value_a" else f"cached_value={value!r}"

        status, result = self._run_fork_probe(child_probe)

        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, msg=result)
        self.assertEqual(result, "ok")

    def test_register_at_fork_reinitializes_queue_and_consumers_in_child_process(self):
        client = Client(FAKE_TEST_API_KEY, send=False)
        old_queue = client.queue
        old_consumer = client.consumers[0]

        def child_probe():
            reinitialized_queue = client.queue is not old_queue
            replaced_consumer = client.consumers[0] is not old_consumer
            consumer_points_to_new_queue = client.consumers[0].queue is client.queue

            if reinitialized_queue and replaced_consumer and consumer_points_to_new_queue:
                return "ok"

            return (
                f"reinitialized_queue={reinitialized_queue}, "
                f"replaced_consumer={replaced_consumer}, "
                f"consumer_points_to_new_queue={consumer_points_to_new_queue}"
            )

        status, result = self._run_fork_probe(child_probe)

        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, msg=result)
        self.assertEqual(result, "ok")

    def test_register_at_fork_reinitializes_poller_and_sessions_in_child_process(self):
        client = Client(
            FAKE_TEST_API_KEY,
            personal_api_key=FAKE_TEST_API_KEY,
            send=False,
            enable_local_evaluation=True,
            poll_interval=100,
        )

        with mock.patch.object(client, "_fetch_feature_flags_from_api"):
            client.load_feature_flags()

        self.assertIsNotNone(client.poller)
        self.assertTrue(client.poller.is_alive())

        old_poller = client.poller
        session_before = posthog.request._get_session()
        flags_session_before = posthog.request._get_flags_session()

        def child_probe():
            poller_ok = False
            poller_msg = "Poller check failed"

            if client.poller:
                if client.poller is not old_poller:
                    if client.poller.is_alive():
                        poller_ok = True
                        poller_msg = "Poller ok"
                    else:
                        poller_msg = "Poller not alive"
                else:
                    poller_msg = "Poller object same"
            else:
                poller_msg = "Poller is None"

            session_after = posthog.request._get_session()
            flags_session_after = posthog.request._get_flags_session()
            sessions_ok = (session_after is not session_before) and (
                flags_session_after is not flags_session_before
            )

            if poller_ok and sessions_ok:
                return "ok"

            return f"{poller_msg}, sessions_ok={sessions_ok}"

        try:
            status, result = self._run_fork_probe(child_probe)
        finally:
            if client.poller:
                client.poller.stop()

        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, msg=result)
        self.assertEqual(result, "ok")
