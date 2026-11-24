"""The shell greeting runs scrobble-say with SCROBBLE_SAY_CACHED_ONLY=1: a cache miss must exit quietly, never call op."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import scrobble_say  # noqa: E402

CFG = {"lastfm": {"username": "u", "secrets_source": "op", "op_vault": "V", "op_item_id": "i"}}


class CachedOnly(unittest.TestCase):
    def test_a_cache_miss_in_cached_only_mode_exits_3_without_reading_the_key(self):
        with mock.patch.dict(os.environ, {"SCROBBLE_SAY_CACHED_ONLY": "1"}), \
                mock.patch.object(scrobble_say, "read_secret_via_op") as op:
            with self.assertRaises(SystemExit) as e:
                scrobble_say.api_key_fn(CFG)()
        self.assertEqual(e.exception.code, scrobble_say.EXIT_CACHE_MISS)
        op.assert_not_called()

    def test_otherwise_the_key_is_read_as_before(self):
        env = {k: v for k, v in os.environ.items() if k != "SCROBBLE_SAY_CACHED_ONLY"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(scrobble_say, "read_secret_via_op", return_value="k") as op:
            self.assertEqual(scrobble_say.api_key_fn(CFG)(), "k")
        op.assert_called_once()


if __name__ == "__main__":
    unittest.main()
