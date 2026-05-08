###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

import json
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from supybot import test as supybot_test

try:
    from . import feeds
    from . import plugin as pulse_plugin
    from . import rendering
    from . import storage
except ImportError:
    import feeds
    import plugin as pulse_plugin
    import rendering
    import storage


RSS_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <link>https://example.com/</link>
    <description>Example description</description>
    <language>en-au</language>
    <item>
      <title>First item</title>
      <link>https://example.com/first</link>
      <description>First description</description>
      <guid>first-guid</guid>
      <pubDate>Tue, 28 Apr 2026 08:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second item</title>
      <link>https://example.com/second</link>
      <description>Second description</description>
    </item>
  </channel>
</rss>
"""

ATOM_SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Feed</title>
</feed>
"""


class DisplayNetwork:
    def __str__(self):
        return "ChatLounge"


class PulseTestCase(supybot_test.PluginTestCase):
    __test__ = False
    plugins = ("Pulse",)


class PulseHelperTestCase(unittest.TestCase):
    def test_plugin_module_exports_class(self):
        self.assertTrue(hasattr(pulse_plugin, "Class"))

    def test_stable_entry_id_prefers_guid(self):
        entry_id = feeds.stable_entry_id(
            guid="guid-123", link="https://example.com/item", title="Hello"
        )
        self.assertEqual(entry_id, "guid-123")

    def test_stable_entry_id_falls_back_to_link(self):
        entry_id = feeds.stable_entry_id(
            guid="", link="https://example.com/item", title="Hello"
        )
        self.assertEqual(entry_id, "https://example.com/item")

    def test_stable_entry_id_hashes_title_and_description(self):
        entry_id = feeds.stable_entry_id(
            guid="", link="", title="Hello", description="World"
        )
        self.assertTrue(entry_id.startswith("sha256:"))

    def test_parse_rss2_feed_extracts_metadata(self):
        parsed = feeds.parse_rss2_feed(RSS_SAMPLE)

        self.assertEqual(parsed["title"], "Example Feed")
        self.assertEqual(parsed["link"], "https://example.com/")
        self.assertEqual(parsed["language"], "en-au")
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(parsed["items"][0]["id"], "first-guid")
        self.assertEqual(parsed["items"][1]["id"], "https://example.com/second")

    def test_parse_rss2_feed_rejects_atom(self):
        with self.assertRaises(feeds.FeedError):
            feeds.parse_rss2_feed(ATOM_SAMPLE)

    def test_prime_subscription_reads_global_backfill_setting(self):
        original_start = threading.Thread.start
        threading.Thread.start = lambda self: None
        try:
            plugin = pulse_plugin.Pulse(MagicMock())
        finally:
            threading.Thread.start = original_start

        def fake_registry_value(name, *args):
            if name == "initialBackfillCount":
                self.assertEqual(args, ())
                return 0
            raise AssertionError(f"Unexpected registry lookup: {name} {args}")

        plugin.registryValue = fake_registry_value
        plugin._refresh_feed = MagicMock(
            return_value={"items": [{"id": "entry-1"}, {"id": "entry-2"}]}
        )
        plugin._mark_seen_ids = MagicMock()
        plugin._send_entry = MagicMock()

        irc = SimpleNamespace(network="testnet")
        plugin._prime_subscription(irc, "#test", "example")

        plugin._mark_seen_ids.assert_called_once_with(
            "testnet", "#test", "example", ["entry-1", "entry-2"]
        )
        plugin._send_entry.assert_not_called()

    def test_refresh_feed_refetches_after_304_when_cache_is_cold(self):
        original_start = threading.Thread.start
        threading.Thread.start = lambda self: None
        try:
            plugin = pulse_plugin.Pulse(MagicMock())
        finally:
            threading.Thread.start = original_start

        plugin._feeds = {
            "testnet": {
                "example": {
                    "url": "https://example.com/rss.xml",
                    "title": "Example Feed",
                    "link": "https://example.com/",
                    "description": "Example description",
                    "language": "en-au",
                    "etag": "etag-1",
                    "modified": "Tue, 28 Apr 2026 08:00:00 GMT",
                    "last_checked": 0,
                    "last_error": "",
                }
            }
        }

        def fake_registry_value(name, *args):
            values = {
                "pollIntervalSeconds": 300,
                "requestTimeoutSeconds": 10,
                "maxFeedBytes": 1048576,
            }
            if name in values:
                return values[name]
            raise AssertionError(f"Unexpected registry lookup: {name} {args}")

        plugin.registryValue = fake_registry_value

        with patch.object(
            pulse_plugin,
            "fetch_rss_feed",
            side_effect=[
                {"status": 304, "etag": "etag-1", "modified": "old", "body": b""},
                {
                    "status": 200,
                    "etag": "etag-2",
                    "modified": "new",
                    "body": RSS_SAMPLE,
                },
            ],
        ) as fetch:
            parsed = plugin._refresh_feed("testnet", "example", force=True)

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(parsed["title"], "Example Feed")
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(
            plugin._feed_cache[("testnet", "example")]["items"][0]["id"],
            "first-guid",
        )
        self.assertEqual(plugin._feeds["testnet"]["example"]["etag"], "etag-2")

    def test_feed_records_are_network_scoped(self):
        original_start = threading.Thread.start
        threading.Thread.start = lambda self: None
        try:
            plugin = pulse_plugin.Pulse(MagicMock())
        finally:
            threading.Thread.start = original_start

        plugin._feeds = {}
        plugin._set_feed_record("net1", "example", {"url": "https://net1.example/"})
        plugin._set_feed_record("net2", "example", {"url": "https://net2.example/"})

        self.assertEqual(
            plugin._get_feed_record("net1", "example")["url"],
            "https://net1.example/",
        )
        self.assertEqual(
            plugin._get_feed_record("net2", "example")["url"],
            "https://net2.example/",
        )

        plugin._delete_feed_record("net1", "example")

        self.assertIsNone(plugin._get_feed_record("net1", "example"))
        self.assertEqual(
            plugin._get_feed_record("net2", "example")["url"],
            "https://net2.example/",
        )

    def test_legacy_flat_feeds_are_readable_without_creating_network(self):
        original_start = threading.Thread.start
        threading.Thread.start = lambda self: None
        try:
            plugin = pulse_plugin.Pulse(MagicMock())
        finally:
            threading.Thread.start = original_start

        plugin._feeds = {
            storage.LEGACY_FEEDS_NETWORK: {
                "example": {"url": "https://example.com/rss.xml"}
            }
        }

        self.assertEqual(
            plugin._get_feed_record("testnet", "example")["url"],
            "https://example.com/rss.xml",
        )
        self.assertNotIn("testnet", plugin._feeds)

    def test_init_removes_stale_pulse_flushers(self):
        original_flushers = list(pulse_plugin.world.flushers)
        original_start = threading.Thread.start
        old_plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)
        pulse_plugin.world.flushers[:] = [old_plugin._flush_state]
        threading.Thread.start = lambda self: None
        try:
            plugin = pulse_plugin.Pulse(MagicMock())

            self.assertNotIn(old_plugin._flush_state, pulse_plugin.world.flushers)
            self.assertIn(plugin._flush_state, pulse_plugin.world.flushers)
            self.assertEqual(len(pulse_plugin._pulse_flushers()), 1)
        finally:
            threading.Thread.start = original_start
            pulse_plugin.world.flushers[:] = original_flushers

    def test_format_announce_add_change_is_clear(self):
        self.assertEqual(
            rendering.format_announce_change("add", "#test", ["LimnoriaNews"]),
            "Now announcing limnorianews in #test.",
        )

    def test_format_announce_remove_change_is_clear(self):
        self.assertEqual(
            rendering.format_announce_change("remove", "#test", ["LimnoriaNews"]),
            "Stopped announcing limnorianews in #test.",
        )

    def test_render_entry_uses_template_and_cleans_output(self):
        entry = {
            "title": "\x02Title\x02",
            "link": "https://example.com/item",
            "description": "Description",
            "published": "Tue, 28 Apr 2026 08:00:00 GMT",
        }

        self.assertEqual(
            rendering.render_entry("example", entry, "$feed: $title <$link>"),
            "example: Title <https://example.com/item>",
        )

    def test_storage_marks_and_limits_seen_ids(self):
        store = storage.PulseStorage(threading.RLock())

        store.mark_seen_ids("net", "#chan", "example", ["old", "new", "new"])

        self.assertEqual(store.seen["net:#chan"]["example"], ["old", "new"])

    def test_storage_entries_to_announce_returns_unseen_only(self):
        store = storage.PulseStorage(threading.RLock())
        store.mark_seen_ids("net", "#chan", "example", ["old"])

        entries = [{"id": "old"}, {"id": "new-1"}, {"id": "new-2"}]

        self.assertEqual(
            store.entries_to_announce("net", "#chan", "example", entries, 1),
            [{"id": "new-1"}],
        )
        self.assertEqual(
            store.seen["net:#chan"]["example"],
            ["old", "new-1", "new-2"],
        )

    def test_storage_missing_feed_lookup_does_not_create_network(self):
        store = storage.PulseStorage(threading.RLock())

        self.assertIsNone(store.get_feed_record("ChatLounge", "bbc"))
        self.assertEqual(store.feeds, {})

    def test_storage_normalises_network_objects_for_lookup(self):
        store = storage.PulseStorage(threading.RLock())
        store.feeds = {"ChatLounge": {"bbc": {"url": "https://example.com/rss.xml"}}}

        self.assertEqual(
            store.get_feed_record(DisplayNetwork(), "bbc"),
            {"url": "https://example.com/rss.xml"},
        )

    def test_storage_prunes_empty_networks(self):
        store = storage.PulseStorage(threading.RLock())
        store.feeds = {
            "ChatLounge": {},
            "DALnet": {"bbc": {"url": "https://example.com/rss.xml"}},
        }

        store.prune_empty_networks()

        self.assertEqual(
            store.feeds,
            {"DALnet": {"bbc": {"url": "https://example.com/rss.xml"}}},
        )

    def test_storage_snapshot_is_isolated_from_live_state(self):
        store = storage.PulseStorage(threading.RLock())
        store.set_feed_record("net", "example", {"url": "https://example.com/rss.xml"})
        feeds, seen = store.snapshot_state()

        feeds["net"]["example"]["url"] = "https://changed.example/rss.xml"
        seen["net:#chan"] = {"example": ["entry-1"]}

        self.assertEqual(
            store.get_feed_record("net", "example")["url"],
            "https://example.com/rss.xml",
        )
        self.assertEqual(store.seen, {})

    def test_flush_state_logs_unserialisable_state_without_raising(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)
        plugin._lock = threading.RLock()
        plugin._storage = storage.PulseStorage(plugin._lock)
        plugin._storage.seen = {"net:#chan": {"example": {"entry-1"}}}

        with tempfile.TemporaryDirectory() as tmpdir:
            feeds_path = str(Path(tmpdir) / "feeds.json")
            seen_path = str(Path(tmpdir) / "seen.json")
            with patch.object(plugin, "_feeds_path", return_value=Path(feeds_path)):
                with patch.object(plugin, "_seen_path", return_value=Path(seen_path)):
                    with patch.object(pulse_plugin.log, "warning") as warning:
                        plugin._flush_state()

        warning.assert_called_once()
        self.assertIn("could not serialise", warning.call_args[0][0])

    def test_write_json_file_does_not_sort_unorderable_keys(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = str(Path(tmpdir) / "state.json")
            plugin._write_json_file(path, {1: "number", "2": "string"})

            self.assertEqual(
                json.loads(Path(path).read_text(encoding="utf-8")),
                {"1": "number", "2": "string"},
            )

    def test_write_json_file_keeps_existing_file_on_serialisation_failure(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text('{"existing": true}\n', encoding="utf-8")

            with patch.object(pulse_plugin.log, "warning") as warning:
                plugin._write_json_file(path, {"broken": {"entry-1"}})

            self.assertEqual(path.read_text(encoding="utf-8"), '{"existing": true}\n')
            warning.assert_called_once()
            self.assertIn("could not serialise", warning.call_args[0][0])

    def test_state_path_resolves_data_directory_at_runtime(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)

        with patch.object(
            pulse_plugin,
            "_dirize_data_file",
            return_value="pulse-state/Pulse.feeds.json",
        ) as dirize_data_file:
            self.assertEqual(plugin._feeds_path(), Path("pulse-state/Pulse.feeds.json"))

        dirize_data_file.assert_called_once_with("Pulse.feeds.json")

    def test_check_owner_accepts_authenticated_owner_user(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)
        msg = SimpleNamespace(prefix="owner!ident@transient.example")
        user = MagicMock()
        user._checkCapability.return_value = True

        with patch.object(
            pulse_plugin.ircdb,
            "users",
            SimpleNamespace(getUser=MagicMock(return_value=user)),
        ):
            with patch.object(
                pulse_plugin.ircdb, "checkCapability", return_value=False
            ) as check_capability:
                self.assertTrue(plugin._check_owner(msg))

        user._checkCapability.assert_called_once_with("owner")
        check_capability.assert_not_called()

    def test_state_command_reports_resolved_files_and_loaded_feeds(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)
        plugin._lock = threading.RLock()
        plugin._storage = storage.PulseStorage(plugin._lock)
        plugin._storage.feeds = {"ChatLounge": {"bbc": {"url": "https://example/"}}}
        plugin._storage.seen = {"ChatLounge:#test": {"bbc": ["entry-1"]}}
        plugin.log = MagicMock()
        irc = MagicMock(network="ChatLounge")
        msg = MagicMock(nick="OwnerNick")

        with tempfile.TemporaryDirectory() as tmpdir:
            feeds_path = Path(tmpdir) / "Pulse.feeds.json"
            seen_path = Path(tmpdir) / "Pulse.seen.json"
            feeds_path.write_text("{}", encoding="utf-8")
            with patch.object(plugin, "_feeds_path", return_value=feeds_path):
                with patch.object(plugin, "_seen_path", return_value=seen_path):
                    with patch.object(plugin, "_check_owner", return_value=True):
                        plugin.state(irc, msg, [])

        notice = irc.queueMsg.call_args[0][0]
        self.assertEqual(notice.command, "NOTICE")
        self.assertEqual(notice.args[0], "OwnerNick")
        reply = notice.args[1]
        self.assertIn("Feeds: 1 across 1 network(s)", reply)
        self.assertIn(f"feed file: {feeds_path} (exists)", reply)
        self.assertIn(f"seen file: {seen_path} (missing)", reply)
        self.assertIn("seen channels: 1", reply)
        self.assertIn("current network: ChatLounge", reply)
        self.assertIn("current network feeds: bbc", reply)
        self.assertIn("stored networks: ChatLounge: bbc", reply)

    def test_load_json_file_merges_duplicate_network_keys(self):
        plugin = pulse_plugin.Pulse.__new__(pulse_plugin.Pulse)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "Pulse.feeds.json"
            path.write_text(
                """
{
  "ChatLounge": {
    "bbc": {
      "url": "https://feeds.bbci.co.uk/news/world/rss.xml"
    }
  },
  "ChatLounge": {}
}
""".lstrip(),
                encoding="utf-8",
            )

            with patch.object(pulse_plugin.log, "warning") as warning:
                data = plugin._load_json_file(path, {})

        self.assertEqual(
            data,
            {
                "ChatLounge": {
                    "bbc": {"url": "https://feeds.bbci.co.uk/news/world/rss.xml"}
                }
            },
        )
        warning.assert_called_once()
        self.assertIn("merged duplicate keys", warning.call_args[0][0])
        self.assertIn("ChatLounge", warning.call_args[0][0])

    def test_announce_add_help_mentions_current_channel(self):
        self.assertIn(
            "If <channel> is omitted, Pulse uses the current channel",
            pulse_plugin.Pulse.announce.add.__doc__,
        )


if __name__ == "__main__":
    unittest.main()


# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
