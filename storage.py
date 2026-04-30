###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

from supybot import callbacks

LEGACY_FEEDS_NETWORK = "__legacy__"
SEEN_ID_LIMIT = 200


def looks_like_feed_record(value):
    return isinstance(value, dict) and isinstance(value.get("url"), str)


class PulseStorage:
    def __init__(self, lock):
        self._lock = lock
        self.feeds = {}
        self.seen = {}
        self.feed_cache = {}

    def load_feeds(self, data):
        if not isinstance(data, dict):
            return
        if any(looks_like_feed_record(value) for value in data.values()):
            self.feeds = {LEGACY_FEEDS_NETWORK: data}
        else:
            self.feeds = data

    def load_seen(self, data):
        if isinstance(data, dict):
            self.seen = data

    def channel_key(self, network, channel):
        return f"{network}:{channel}"

    def feed_key(self, network, name):
        return (network, callbacks.canonicalName(name))

    def network_feeds(self, network):
        if network not in self.feeds and LEGACY_FEEDS_NETWORK in self.feeds:
            self.feeds[network] = {
                name: dict(record)
                for name, record in self.feeds[LEGACY_FEEDS_NETWORK].items()
                if looks_like_feed_record(record)
            }
        return self.feeds.setdefault(network, {})

    def get_feed_record(self, network, name):
        return self.network_feeds(network).get(callbacks.canonicalName(name))

    def set_feed_record(self, network, name, record):
        self.network_feeds(network)[callbacks.canonicalName(name)] = record

    def delete_feed_record(self, network, name):
        feed_name = callbacks.canonicalName(name)
        network_feeds = self.feeds.get(network, {})
        network_feeds.pop(feed_name, None)
        if not network_feeds and LEGACY_FEEDS_NETWORK not in self.feeds:
            self.feeds.pop(network, None)
        self.feed_cache.pop(self.feed_key(network, feed_name), None)

    def mark_seen_ids(self, network, channel, feed_name, entry_ids):
        if not entry_ids:
            return

        channel_key = self.channel_key(network, channel)
        with self._lock:
            channel_state = self.seen.setdefault(channel_key, {})
            seen_ids = channel_state.setdefault(feed_name, [])
            existing = set(seen_ids)
            for entry_id in entry_ids:
                if entry_id not in existing:
                    seen_ids.append(entry_id)
                    existing.add(entry_id)
            if len(seen_ids) > SEEN_ID_LIMIT:
                channel_state[feed_name] = seen_ids[-SEEN_ID_LIMIT:]

    def forget_seen_ids(self, network, channel, feed_name):
        channel_key = self.channel_key(network, channel)
        with self._lock:
            channel_state = self.seen.get(channel_key, {})
            channel_state.pop(feed_name, None)
            if not channel_state:
                self.seen.pop(channel_key, None)

    def remove_feed_from_network_seen(self, network, feed_name):
        prefix = f"{network}:"
        for channel_key in list(self.seen.keys()):
            if not channel_key.startswith(prefix):
                continue
            self.seen[channel_key].pop(feed_name, None)
            if not self.seen[channel_key]:
                self.seen.pop(channel_key, None)

    def entries_to_announce(self, network, channel, feed_name, entries, maximum):
        channel_key = self.channel_key(network, channel)
        channel_state = self.seen.get(channel_key, {})
        seen_ids = set(channel_state.get(feed_name, []))
        new_entries = [entry for entry in entries if entry["id"] not in seen_ids]
        if not new_entries:
            return []
        self.mark_seen_ids(
            network, channel, feed_name, [entry["id"] for entry in new_entries]
        )
        return new_entries[:maximum]


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
