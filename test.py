###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from supybot.test import PluginTestCase

try:
    from . import plugin as pulse_plugin
except ImportError:
    import plugin as pulse_plugin


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


class PulseTestCase(PluginTestCase):
    __test__ = False
    plugins = ("Pulse",)


class PulseHelperTestCase(unittest.TestCase):
    def test_plugin_module_exports_class(self):
        self.assertTrue(hasattr(pulse_plugin, "Class"))

    def test_stable_entry_id_prefers_guid(self):
        entry_id = pulse_plugin._stable_entry_id(
            guid="guid-123", link="https://example.com/item", title="Hello"
        )
        self.assertEqual(entry_id, "guid-123")

    def test_stable_entry_id_falls_back_to_link(self):
        entry_id = pulse_plugin._stable_entry_id(
            guid="", link="https://example.com/item", title="Hello"
        )
        self.assertEqual(entry_id, "https://example.com/item")

    def test_stable_entry_id_hashes_title_and_description(self):
        entry_id = pulse_plugin._stable_entry_id(
            guid="", link="", title="Hello", description="World"
        )
        self.assertTrue(entry_id.startswith("sha256:"))

    def test_parse_rss2_feed_extracts_metadata(self):
        parsed = pulse_plugin.parse_rss2_feed(RSS_SAMPLE)

        self.assertEqual(parsed["title"], "Example Feed")
        self.assertEqual(parsed["link"], "https://example.com/")
        self.assertEqual(parsed["language"], "en-au")
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(parsed["items"][0]["id"], "first-guid")
        self.assertEqual(parsed["items"][1]["id"], "https://example.com/second")

    def test_parse_rss2_feed_rejects_atom(self):
        with self.assertRaises(pulse_plugin.FeedError):
            pulse_plugin.parse_rss2_feed(ATOM_SAMPLE)

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
            parsed = plugin._refresh_feed("example", force=True)

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(parsed["title"], "Example Feed")
        self.assertEqual(len(parsed["items"]), 2)
        self.assertEqual(plugin._feed_cache["example"]["items"][0]["id"], "first-guid")
        self.assertEqual(plugin._feeds["example"]["etag"], "etag-2")

    def test_format_announce_add_change_is_clear(self):
        self.assertEqual(
            pulse_plugin._format_announce_change("add", "#test", ["LimnoriaNews"]),
            "Now announcing limnorianews in #test.",
        )

    def test_format_announce_remove_change_is_clear(self):
        self.assertEqual(
            pulse_plugin._format_announce_change("remove", "#test", ["LimnoriaNews"]),
            "Stopped announcing limnorianews in #test.",
        )

    def test_announce_add_help_mentions_current_channel(self):
        self.assertIn(
            "If <channel> is omitted, Pulse uses the current channel",
            pulse_plugin.Pulse.announce.add.__doc__,
        )


if __name__ == "__main__":
    unittest.main()


# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
