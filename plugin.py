###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import supybot.conf as conf
import supybot.ircdb as ircdb
import supybot.ircmsgs as ircmsgs
import supybot.log as log
import supybot.registry as registry
import supybot.world as world
from supybot import callbacks
from supybot.commands import *
from supybot.i18n import PluginInternationalization

_ = PluginInternationalization("Pulse")

try:
    from .feeds import FeedError
    from .feeds import clean_text as _clean_text
    from .feeds import fetch_rss_feed
    from .feeds import parse_rss2_feed
    from .feeds import stable_entry_id as _stable_entry_id
    from .rendering import format_announce_change as _format_announce_change
    from .rendering import render_entry
    from .storage import LEGACY_FEEDS_NETWORK
    from .storage import PulseStorage
    from .storage import network_key
except ImportError:
    from feeds import FeedError
    from feeds import clean_text as _clean_text
    from feeds import fetch_rss_feed
    from feeds import parse_rss2_feed
    from feeds import stable_entry_id as _stable_entry_id
    from rendering import format_announce_change as _format_announce_change
    from rendering import render_entry
    from storage import LEGACY_FEEDS_NETWORK
    from storage import PulseStorage
    from storage import network_key

FEEDS_FILENAME = "Pulse.feeds.json"
SEEN_FILENAME = "Pulse.seen.json"
POLL_TICK_SECONDS = 5


def _is_pulse_flush_state(callback):
    return getattr(callback, "__self__", None) is not None and getattr(
        callback, "__func__", None
    ) is getattr(Pulse, "_flush_state", None)


def _pulse_flushers():
    return [flusher for flusher in world.flushers if _is_pulse_flush_state(flusher)]


def _dirize_data_file(filename):
    return conf.supybot.directories.data.dirize(filename)


def _merge_duplicate_json_object_pairs(pairs):
    data = {}
    duplicates = set()
    for key, value in pairs:
        if key in data:
            duplicates.add(key)
            if isinstance(data[key], dict) and isinstance(value, dict):
                merged = dict(data[key])
                merged.update(value)
                data[key] = merged
            else:
                data[key] = value
            continue
        data[key] = value
    if duplicates:
        data["__pulse_duplicate_keys__"] = sorted(duplicates)
    return data


def _pop_duplicate_key_markers(value):
    duplicates = []
    if isinstance(value, dict):
        marker = value.pop("__pulse_duplicate_keys__", None)
        if marker:
            duplicates.extend(marker)
        for child in value.values():
            duplicates.extend(_pop_duplicate_key_markers(child))
    elif isinstance(value, list):
        for child in value:
            duplicates.extend(_pop_duplicate_key_markers(child))
    return duplicates


def get_feed_name(irc, msg, args, state):
    if irc.isChannel(args[0]):
        state.errorInvalid("feed name", args[0], "must not be a channel name.")
    if not registry.isValidRegistryName(args[0]):
        state.errorInvalid("feed name", args[0], "must not include spaces.")
    if "." in args[0]:
        state.errorInvalid("feed name", args[0], "must not include dots.")
    state.args.append(callbacks.canonicalName(args.pop(0)))


addConverter("feedName", get_feed_name)


class Pulse(callbacks.Plugin):
    """
    Poll RSS 2.0 feeds on demand or announce new items to configured channels.
    """

    threaded = True

    def __init__(self, irc):
        self.__parent = super(Pulse, self)
        self.__parent.__init__(irc)
        self._lock = threading.RLock()
        self._storage = PulseStorage(self._lock)
        self._stop_event = threading.Event()
        self._load_feeds()
        self._load_seen()
        stale_flushers = [
            flusher for flusher in _pulse_flushers() if flusher.__self__ is not self
        ]
        for flusher in stale_flushers:
            world.flushers.remove(flusher)
        if stale_flushers:
            log.warning(f"Pulse: removed {len(stale_flushers)} stale state flusher(s)")
        world.flushers.append(self._flush_state)
        log.info(
            f"Pulse: registered state flusher for instance {id(self)}; "
            f"active Pulse flushers: {len(_pulse_flushers())}"
        )
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="PulsePoller", daemon=True
        )
        self._poll_thread.start()

    def _check_owner(self, msg) -> bool:
        try:
            user = ircdb.users.getUser(msg.prefix)
        except KeyError:
            pass
        except Exception:
            return False
        else:
            try:
                return bool(user._checkCapability("owner"))
            except Exception:
                return False

        try:
            return bool(ircdb.checkCapability(msg.prefix, "owner"))
        except Exception:
            return False

    @property
    def _feeds(self):
        return self._storage.feeds

    @_feeds.setter
    def _feeds(self, value):
        self._storage.feeds = value

    @property
    def _seen(self):
        return self._storage.seen

    @_seen.setter
    def _seen(self, value):
        self._storage.seen = value

    @property
    def _feed_cache(self):
        return self._storage.feed_cache

    @_feed_cache.setter
    def _feed_cache(self, value):
        self._storage.feed_cache = value

    def die(self):
        self._stop_event.set()
        self._poll_thread.join(timeout=POLL_TICK_SECONDS)
        self._flush_state()
        while self._flush_state in world.flushers:
            world.flushers.remove(self._flush_state)
        self.__parent.die()

    def _state_path(self, filename):
        return Path(_dirize_data_file(filename))

    def _feeds_path(self):
        return self._state_path(FEEDS_FILENAME)

    def _seen_path(self):
        return self._state_path(SEEN_FILENAME)

    def _load_json_file(self, path, default):
        path = Path(path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(
                    handle, object_pairs_hook=_merge_duplicate_json_object_pairs
                )
            duplicates = _pop_duplicate_key_markers(data)
            if duplicates:
                log.warning(
                    "Pulse: merged duplicate keys in "
                    f"{path}: {', '.join(sorted(set(duplicates)))}"
                )
            return data
        except FileNotFoundError:
            log.info(f"Pulse: state file does not exist: {path}")
            return default
        except json.JSONDecodeError as e:
            log.warning(f"Pulse: could not parse {path}: {e}")
            return default
        except OSError as e:
            log.warning(f"Pulse: could not read {path}: {e}")
            return default

    def _write_json_file(self, path, data):
        path = Path(path)
        tmp_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                delete=False,
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as handle:
                tmp_path = Path(handle.name)
                json.dump(data, handle, indent=2)
                handle.write("\n")
            os.replace(tmp_path, path)
        except (TypeError, ValueError) as e:
            log.warning(f"Pulse: could not serialise {path}: {e}")
        except OSError as e:
            log.warning(f"Pulse: could not write {path}: {e}")
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError as e:
                    log.warning(
                        f"Pulse: could not remove temporary state file {tmp_path}: {e}"
                    )

    def _load_feeds(self):
        path = self._feeds_path()
        self._storage.load_feeds(self._load_json_file(path, {}))
        feeds, _ = self._storage.snapshot_state()
        network_count = len(feeds)
        feed_count = sum(
            len(network_feeds)
            for network_feeds in feeds.values()
            if isinstance(network_feeds, dict)
        )
        log.info(
            f"Pulse: loaded {feed_count} feed(s) across "
            f"{network_count} network(s) from {path}"
        )

    def _load_seen(self):
        path = self._seen_path()
        self._storage.load_seen(self._load_json_file(path, {}))
        _, seen = self._storage.snapshot_state()
        channel_count = len(seen)
        log.info(f"Pulse: loaded seen state for {channel_count} channel(s) from {path}")

    def _flush_state(self):
        self._storage.prune_empty_networks()
        feeds, seen = self._storage.snapshot_state()
        self._write_json_file(self._feeds_path(), feeds)
        self._write_json_file(self._seen_path(), seen)

    def _channel_key(self, network, channel):
        return self._storage.channel_key(network, channel)

    def _feed_key(self, network, name):
        return self._storage.feed_key(network, name)

    def _network_feeds(self, network):
        return self._storage.network_feeds(network)

    def _get_feed_record(self, network, name):
        return self._storage.get_feed_record(network, name)

    def _set_feed_record(self, network, name, record):
        self._storage.set_feed_record(network, name, record)

    def _delete_feed_record(self, network, name):
        self._storage.delete_feed_record(network, name)

    def _mark_seen_ids(self, network, channel, feed_name, entry_ids):
        self._storage.mark_seen_ids(network, channel, feed_name, entry_ids)

    def _forget_seen_ids(self, network, channel, feed_name):
        self._storage.forget_seen_ids(network, channel, feed_name)

    def _refresh_feed(self, network, name, force=False):
        feed_name = callbacks.canonicalName(name)
        feed_key = self._feed_key(network, feed_name)
        record = self._get_feed_record(network, feed_name)
        if not record:
            raise FeedError(f"Unknown feed: {feed_name}")

        now = time.time()
        if not force:
            last_checked = float(record.get("last_checked", 0) or 0)
            if now - last_checked < self.registryValue("pollIntervalSeconds"):
                cached = self._storage.feed_cache.get(feed_key)
                if cached is not None:
                    return cached

        result = fetch_rss_feed(
            record["url"],
            timeout_seconds=self.registryValue("requestTimeoutSeconds"),
            max_feed_bytes=self.registryValue("maxFeedBytes"),
            etag=record.get("etag", ""),
            modified=record.get("modified", ""),
        )

        if result["status"] == 304:
            cached = self._storage.feed_cache.get(feed_key)
            if cached is None:
                result = fetch_rss_feed(
                    record["url"],
                    timeout_seconds=self.registryValue("requestTimeoutSeconds"),
                    max_feed_bytes=self.registryValue("maxFeedBytes"),
                )
            else:
                record["last_checked"] = now
                record["last_error"] = ""
                self._set_feed_record(network, feed_name, record)
                return cached

        if result["status"] == 304:
            record["last_checked"] = now
            record["last_error"] = ""
            self._set_feed_record(network, feed_name, record)
            return self._storage.feed_cache.get(feed_key, {"items": []})

        parsed = parse_rss2_feed(result["body"])
        record.update(
            {
                "url": record["url"],
                "title": parsed["title"],
                "link": parsed["link"],
                "description": parsed["description"],
                "language": parsed["language"],
                "etag": result.get("etag", ""),
                "modified": result.get("modified", ""),
                "last_checked": now,
                "last_error": "",
            }
        )
        self._set_feed_record(network, feed_name, record)
        self._storage.feed_cache[feed_key] = parsed
        return parsed

    def _entry_template(self, channel, network):
        return self.registryValue("headlineFormat", channel, network)

    def _render_entry(self, feed_name, entry, channel=None, network=None):
        template = (
            self._entry_template(channel, network)
            if channel
            else "$feed: $title <$link>"
        )
        return render_entry(feed_name, entry, template)

    def _current_targets(self):
        targets = {}
        for irc in world.ircs:
            for channel in list(irc.state.channels):
                if not self.registryValue("enabled", channel, irc.network):
                    continue
                for feed_name in self.registryValue(
                    "announceFeeds", channel, irc.network
                ):
                    name = callbacks.canonicalName(feed_name)
                    targets.setdefault((irc.network, name), []).append((irc, channel))
        return targets

    def _prime_subscription(self, irc, channel, feed_name):
        parsed = self._refresh_feed(irc.network, feed_name, force=True)
        items = parsed["items"]
        backfill = self.registryValue("initialBackfillCount")
        unseen_ids = [item["id"] for item in items]
        self._mark_seen_ids(irc.network, channel, feed_name, unseen_ids)
        if backfill <= 0:
            return
        for entry in items[:backfill]:
            self._send_entry(irc, channel, feed_name, entry)

    def _entries_to_announce(self, network, channel, feed_name, entries):
        return self._storage.entries_to_announce(
            network,
            channel,
            feed_name,
            entries,
            self.registryValue("maximumAnnouncements", channel, network),
        )

    def _send_entry(self, irc, channel, feed_name, entry):
        text = self._render_entry(
            feed_name, entry, channel=channel, network=irc.network
        )
        if self.registryValue("announceAsNotice", channel, irc.network):
            irc.queueMsg(ircmsgs.notice(channel, text))
        else:
            irc.queueMsg(ircmsgs.privmsg(channel, text))

    def _poll_loop(self):
        startup_delay = self.registryValue("startupDelaySeconds")
        if self._stop_event.wait(startup_delay):
            return

        while not self._stop_event.is_set():
            try:
                self._poll_announced_feeds()
            except Exception as e:
                log.error(f"Pulse: polling failure: {e}")
            self._stop_event.wait(POLL_TICK_SECONDS)

    def _poll_announced_feeds(self):
        for (network, feed_name), destinations in self._current_targets().items():
            if not self._get_feed_record(network, feed_name):
                log.warning(
                    f"Pulse: announced feed {feed_name} is not registered on {network}."
                )
                continue
            try:
                parsed = self._refresh_feed(network, feed_name, force=False)
            except FeedError as e:
                record = self._get_feed_record(network, feed_name) or {}
                record["last_error"] = str(e)
                record["last_checked"] = time.time()
                if record:
                    self._set_feed_record(network, feed_name, record)
                log.warning(f"Pulse: failed to refresh {feed_name} on {network}: {e}")
                continue

            for irc, channel in destinations:
                entries = self._entries_to_announce(
                    irc.network, channel, feed_name, parsed["items"]
                )
                for entry in entries:
                    self._send_entry(irc, channel, feed_name, entry)

    def add(self, irc, msg, args, name, url):
        """<name> <url>

        Adds an RSS 2.0 feed to Pulse after validating that the feed can be fetched
        and parsed.
        """
        name = callbacks.canonicalName(name)
        if self._get_feed_record(irc.network, name):
            irc.error(f"Feed {name} already exists.", prefixNick=False)
            return

        result = fetch_rss_feed(
            url,
            timeout_seconds=self.registryValue("requestTimeoutSeconds"),
            max_feed_bytes=self.registryValue("maxFeedBytes"),
        )
        parsed = parse_rss2_feed(result["body"])
        self._set_feed_record(
            irc.network,
            name,
            {
                "url": url,
                "title": parsed["title"],
                "link": parsed["link"],
                "description": parsed["description"],
                "language": parsed["language"],
                "etag": result.get("etag", ""),
                "modified": result.get("modified", ""),
                "last_checked": time.time(),
                "last_error": "",
            },
        )
        self._storage.feed_cache[self._feed_key(irc.network, name)] = parsed
        self._flush_state()
        irc.reply(f"Added {name}: {parsed['title']}", prefixNick=False)

    add = wrap(add, ["feedName", "url"])

    def remove(self, irc, msg, args, name):
        """<name>

        Removes a registered feed from Pulse.
        """
        name = callbacks.canonicalName(name)
        if not self._get_feed_record(irc.network, name):
            irc.error("Unknown feed.", prefixNick=False)
            return
        self._delete_feed_record(irc.network, name)
        self._storage.remove_feed_from_network_seen(irc.network, name)
        self._flush_state()
        irc.replySuccess()

    remove = wrap(remove, ["feedName"])

    def list(self, irc, msg, args):
        """takes no arguments

        Lists the registered feeds known to Pulse.
        """
        names = sorted(self._network_feeds(irc.network))
        irc.reply(format("%L", names) or "No feeds are registered.", prefixNick=False)

    list = wrap(list)

    def state(self, irc, msg, args):
        """takes no arguments

        Shows the state files Pulse is using and how many feeds are loaded.
        """
        if not self._check_owner(msg):
            irc.errorNoCapability("owner", prefixNick=False)
            return

        feeds_path = self._feeds_path()
        seen_path = self._seen_path()
        feeds, seen = self._storage.snapshot_state()
        current_network = network_key(irc.network)
        current_feeds = sorted(feeds.get(current_network, {}))
        stored_networks = [
            f"{network}: {format('%L', sorted(network_feeds)) or 'none'}"
            for network, network_feeds in sorted(feeds.items())
            if isinstance(network_feeds, dict)
        ]
        pulse_threads = [
            thread.name
            for thread in threading.enumerate()
            if thread.name == "PulsePoller"
        ]
        network_count = len(feeds)
        feed_count = sum(
            len(network_feeds)
            for network_feeds in feeds.values()
            if isinstance(network_feeds, dict)
        )
        irc.queueMsg(
            ircmsgs.notice(
                msg.nick,
                "Feeds: "
                f"{feed_count} across {network_count} network(s); "
                f"feed file: {feeds_path} "
                f"({'exists' if feeds_path.exists() else 'missing'}); "
                f"seen file: {seen_path} "
                f"({'exists' if seen_path.exists() else 'missing'}); "
                f"seen channels: {len(seen)}; "
                f"current network: {current_network}; "
                f"current network feeds: {format('%L', current_feeds) or 'none'}; "
                f"stored networks: {' | '.join(stored_networks) or 'none'}; "
                f"instance: {id(self)}; Pulse flushers: {len(_pulse_flushers())}; "
                f"Pulse pollers: {len(pulse_threads)}",
            )
        )

    state = wrap(state)

    def show(self, irc, msg, args, name):
        """<name>

        Shows the stored metadata for a registered feed.
        """
        name = callbacks.canonicalName(name)
        record = self._get_feed_record(irc.network, name)
        if not record:
            irc.error("Unknown feed.", prefixNick=False)
            return

        last_checked = record.get("last_checked", 0)
        if last_checked:
            checked_text = time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime(last_checked)
            )
        else:
            checked_text = "never"

        bits = [
            f"Feed: {name}",
            f"Title: {record.get('title') or 'Unknown'}",
            f"URL: {record.get('url') or 'Unknown'}",
            f"Last checked: {checked_text}",
        ]
        last_error = _clean_text(record.get("last_error", ""), limit=120)
        if last_error:
            bits.append(f"Last error: {last_error}")
        irc.reply(" | ".join(bits), prefixNick=False)

    show = wrap(show, ["feedName"])

    def latest(self, irc, msg, args, name, count):
        """<name> [<count>]

        Fetches the latest items from a registered RSS 2.0 feed.
        """
        name = callbacks.canonicalName(name)
        if count is None:
            count = 1
        if count < 1 or count > 10:
            irc.error("Count must be between 1 and 10.", prefixNick=False)
            return

        try:
            parsed = self._refresh_feed(irc.network, name, force=True)
        except FeedError as e:
            irc.error(str(e), prefixNick=False)
            return

        items = parsed["items"][:count]
        if not items:
            irc.error("The feed returned no items.", prefixNick=False)
            return
        irc.replies(
            [
                self._render_entry(name, entry, msg.args[0], irc.network)
                for entry in items
            ],
            joiner=" | ",
            prefixNick=False,
        )

    latest = wrap(latest, ["feedName", optional("int")])

    def refresh(self, irc, msg, args, name):
        """[<name>]

        Forces a refresh of one feed, or all registered feeds if no name is given.
        """
        if name:
            feed_names = [callbacks.canonicalName(name)]
        else:
            feed_names = sorted(self._network_feeds(irc.network))
        if not feed_names:
            irc.error("No feeds are registered.", prefixNick=False)
            return

        refreshed = 0
        failures = []
        for feed_name in feed_names:
            try:
                self._refresh_feed(irc.network, feed_name, force=True)
            except FeedError as e:
                failures.append(f"{feed_name}: {e}")
                continue
            refreshed += 1

        if failures and not refreshed:
            irc.error(" | ".join(failures), prefixNick=False)
            return

        if failures:
            irc.reply(
                f"Refreshed {refreshed} feed(s). Failures: {' | '.join(failures)}",
                prefixNick=False,
            )
            return

        irc.reply(f"Refreshed {refreshed} feed(s).", prefixNick=False)

    refresh = wrap(refresh, [optional("feedName")])

    class announce(callbacks.Commands):
        def list(self, irc, msg, args, channel):
            """[<channel>]

            Lists the registered feeds Pulse announces in the given channel. If
            <channel> is omitted, Pulse uses the current channel.
            """
            announce = conf.supybot.plugins.Pulse.announceFeeds
            feeds = sorted(announce.getSpecific(channel=channel, network=irc.network)())
            irc.reply(
                format("%L", feeds) or "No feeds are announced there.", prefixNick=False
            )

        list = wrap(list, ["channel"])

        def add(self, irc, msg, args, channel, feeds):
            """[<channel>] <feed> [<feed> ...]

            Adds one or more already-registered feeds to a channel's announce
            list. If <channel> is omitted, Pulse uses the current channel, so
            '@pulse announce add limnorianews' means 'announce the registered
            feed limnorianews here.'
            """
            plugin = irc.getCallback("Pulse")
            invalid = [
                feed for feed in feeds if not plugin._get_feed_record(irc.network, feed)
            ]
            if invalid:
                irc.error(format("Unknown feeds: %L", invalid), prefixNick=False)
                return

            announce = conf.supybot.plugins.Pulse.announceFeeds
            current = announce.getSpecific(channel=channel, network=irc.network)()
            for feed in feeds:
                current.add(callbacks.canonicalName(feed))
            announce.getSpecific(
                channel=channel, network=irc.network, fallback_to_channel=False
            ).setValue(current)

            for feed in feeds:
                plugin._prime_subscription(irc, channel, callbacks.canonicalName(feed))
            plugin._flush_state()
            irc.reply(_format_announce_change("add", channel, feeds), prefixNick=False)

        add = wrap(add, [("checkChannelCapability", "op"), many("feedName")])

        def remove(self, irc, msg, args, channel, feeds):
            """[<channel>] <feed> [<feed> ...]

            Removes one or more feeds from a channel's announce list. If
            <channel> is omitted, Pulse uses the current channel.
            """
            plugin = irc.getCallback("Pulse")
            announce = conf.supybot.plugins.Pulse.announceFeeds
            current = announce.getSpecific(channel=channel, network=irc.network)()
            for feed in feeds:
                name = callbacks.canonicalName(feed)
                current.discard(name)
                plugin._forget_seen_ids(irc.network, channel, name)
            announce.getSpecific(
                channel=channel, network=irc.network, fallback_to_channel=False
            ).setValue(current)
            plugin._flush_state()
            irc.reply(
                _format_announce_change("remove", channel, feeds), prefixNick=False
            )

        remove = wrap(remove, [("checkChannelCapability", "op"), many("feedName")])


Class = Pulse


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
