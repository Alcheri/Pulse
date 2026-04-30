###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

import builtins
import hashlib
import json
import re
import string
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import supybot.conf as conf
import supybot.ircmsgs as ircmsgs
import supybot.ircutils as ircutils
import supybot.log as log
import supybot.registry as registry
import supybot.world as world
from supybot import callbacks
from supybot.commands import *
from supybot.i18n import PluginInternationalization

_ = PluginInternationalization("Pulse")

USER_AGENT = "Limnoria-Pulse/0.1 (+https://github.com/Alcheri/Pulse)"
FEEDS_FILENAME = conf.supybot.directories.data.dirize("Pulse.feeds.json")
SEEN_FILENAME = conf.supybot.directories.data.dirize("Pulse.seen.json")
LEGACY_FEEDS_NETWORK = "__legacy__"
POLL_TICK_SECONDS = 5
SEEN_ID_LIMIT = 200
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def get_feed_name(irc, msg, args, state):
    if irc.isChannel(args[0]):
        state.errorInvalid("feed name", args[0], "must not be a channel name.")
    if not registry.isValidRegistryName(args[0]):
        state.errorInvalid("feed name", args[0], "must not include spaces.")
    if "." in args[0]:
        state.errorInvalid("feed name", args[0], "must not include dots.")
    state.args.append(callbacks.canonicalName(args.pop(0)))


addConverter("feedName", get_feed_name)


class FeedError(callbacks.Error):
    pass


def _clean_text(value, limit=None):
    text = ircutils.stripFormatting(str(value or ""))
    text = CONTROL_CHARS_RE.sub("", text)
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        return f"{text[: max(0, limit - 3)].rstrip()}..."
    return text


def _local_name(tag):
    return tag.rsplit("}", 1)[-1]


def _child_text(element, name):
    for child in list(element):
        if _local_name(child.tag) == name:
            return _clean_text(child.text)
    return ""


def _stable_entry_id(guid="", link="", title="", description=""):
    guid = _clean_text(guid, limit=512)
    if guid:
        return guid

    link = _clean_text(link, limit=512)
    if link:
        return link

    basis = " ".join(
        part
        for part in (_clean_text(title, 256), _clean_text(description, 256))
        if part
    )
    if not basis:
        raise FeedError("Feed entry is missing guid, link, title, and description.")
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _format_announce_change(action, channel, feeds):
    names = [callbacks.canonicalName(feed) for feed in feeds]
    if action == "add":
        return f"Now announcing {format('%L', names)} in {channel}."
    if action == "remove":
        return f"Stopped announcing {format('%L', names)} in {channel}."
    raise ValueError(f"Unknown announce action: {action}")


def _looks_like_feed_record(value):
    return isinstance(value, dict) and isinstance(value.get("url"), str)


def parse_rss2_feed(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FeedError(f"Malformed XML: {e}") from e

    if _local_name(root.tag) != "rss":
        raise FeedError("Unsupported feed format: expected an RSS 2.0 document.")

    version = (root.attrib.get("version") or "").strip()
    if not version.startswith("2."):
        raise FeedError("Unsupported RSS version: only RSS 2.0 feeds are supported.")

    channel = None
    for child in list(root):
        if _local_name(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        raise FeedError("RSS feed is missing a channel element.")

    metadata = {
        "title": _child_text(channel, "title") or "Untitled feed",
        "link": _child_text(channel, "link"),
        "description": _child_text(channel, "description"),
        "language": _child_text(channel, "language"),
        "items": [],
    }

    for item in list(channel):
        if _local_name(item.tag) != "item":
            continue
        title = _child_text(item, "title") or "Untitled item"
        link = _child_text(item, "link")
        description = _child_text(item, "description")
        published = _child_text(item, "pubDate")
        guid = _child_text(item, "guid")
        metadata["items"].append(
            {
                "id": _stable_entry_id(guid, link, title, description),
                "title": title,
                "link": link,
                "description": description,
                "published": published,
            }
        )

    return metadata


def fetch_rss_feed(url, timeout_seconds, max_feed_bytes, etag=None, modified=None):
    headers = {"User-Agent": USER_AGENT}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout_seconds)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return {"status": 304, "etag": etag, "modified": modified, "body": b""}
        raise FeedError(f"HTTP error {e.code} while fetching {url}") from e
    except urllib.error.URLError as e:
        raise FeedError(f"Network error while fetching {url}: {e.reason}") from e

    with response:
        body = response.read(max_feed_bytes + 1)
        if len(body) > max_feed_bytes:
            raise FeedError(f"Feed exceeds the maximum size of {max_feed_bytes} bytes.")
        return {
            "status": getattr(response, "status", 200),
            "etag": response.headers.get("ETag", ""),
            "modified": response.headers.get("Last-Modified", ""),
            "body": body,
        }


class Pulse(callbacks.Plugin):
    """
    Poll RSS 2.0 feeds on demand or announce new items to configured channels.
    """

    threaded = True

    def __init__(self, irc):
        self.__parent = super(Pulse, self)
        self.__parent.__init__(irc)
        self._lock = threading.RLock()
        self._feeds = {}
        self._seen = {}
        self._feed_cache = {}
        self._stop_event = threading.Event()
        self._load_feeds()
        self._load_seen()
        world.flushers.append(self._flush_state)
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="PulsePoller", daemon=True
        )
        self._poll_thread.start()

    def die(self):
        self._stop_event.set()
        self._poll_thread.join(timeout=POLL_TICK_SECONDS)
        self._flush_state()
        if self._flush_state in world.flushers:
            world.flushers.remove(self._flush_state)
        self.__parent.die()

    def _load_json_file(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except json.JSONDecodeError as e:
            log.warning(f"Pulse: could not parse {path}: {e}")
            return default
        except OSError as e:
            log.warning(f"Pulse: could not read {path}: {e}")
            return default

    def _write_json_file(self, path, data):
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
        except OSError as e:
            log.warning(f"Pulse: could not write {path}: {e}")

    def _load_feeds(self):
        data = self._load_json_file(FEEDS_FILENAME, {})
        if isinstance(data, dict):
            if builtins.any(_looks_like_feed_record(value) for value in data.values()):
                self._feeds = {LEGACY_FEEDS_NETWORK: data}
            else:
                self._feeds = data

    def _load_seen(self):
        data = self._load_json_file(SEEN_FILENAME, {})
        if isinstance(data, dict):
            self._seen = data

    def _flush_state(self):
        with self._lock:
            self._write_json_file(FEEDS_FILENAME, self._feeds)
            self._write_json_file(SEEN_FILENAME, self._seen)

    def _channel_key(self, network, channel):
        return f"{network}:{channel}"

    def _feed_key(self, network, name):
        return (network, callbacks.canonicalName(name))

    def _network_feeds(self, network):
        if network not in self._feeds and LEGACY_FEEDS_NETWORK in self._feeds:
            self._feeds[network] = {
                name: dict(record)
                for name, record in self._feeds[LEGACY_FEEDS_NETWORK].items()
                if _looks_like_feed_record(record)
            }
        return self._feeds.setdefault(network, {})

    def _get_feed_record(self, network, name):
        return self._network_feeds(network).get(callbacks.canonicalName(name))

    def _set_feed_record(self, network, name, record):
        self._network_feeds(network)[callbacks.canonicalName(name)] = record

    def _delete_feed_record(self, network, name):
        feed_name = callbacks.canonicalName(name)
        network_feeds = self._feeds.get(network, {})
        network_feeds.pop(feed_name, None)
        if not network_feeds and LEGACY_FEEDS_NETWORK not in self._feeds:
            self._feeds.pop(network, None)
        self._feed_cache.pop(self._feed_key(network, feed_name), None)

    def _mark_seen_ids(self, network, channel, feed_name, entry_ids):
        if not entry_ids:
            return

        channel_key = self._channel_key(network, channel)
        with self._lock:
            channel_state = self._seen.setdefault(channel_key, {})
            seen_ids = channel_state.setdefault(feed_name, [])
            existing = set(seen_ids)
            for entry_id in entry_ids:
                if entry_id not in existing:
                    seen_ids.append(entry_id)
                    existing.add(entry_id)
            if len(seen_ids) > SEEN_ID_LIMIT:
                channel_state[feed_name] = seen_ids[-SEEN_ID_LIMIT:]

    def _forget_seen_ids(self, network, channel, feed_name):
        channel_key = self._channel_key(network, channel)
        with self._lock:
            channel_state = self._seen.get(channel_key, {})
            channel_state.pop(feed_name, None)
            if not channel_state:
                self._seen.pop(channel_key, None)

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
                cached = self._feed_cache.get(feed_key)
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
            cached = self._feed_cache.get(feed_key)
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
            return self._feed_cache.get(feed_key, {"items": []})

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
        self._feed_cache[feed_key] = parsed
        return parsed

    def _entry_template(self, channel, network):
        return self.registryValue("headlineFormat", channel, network)

    def _render_entry(self, feed_name, entry, channel=None, network=None):
        template = (
            self._entry_template(channel, network)
            if channel
            else "$feed: $title <$link>"
        )
        rendered = string.Template(template).safe_substitute(
            feed=feed_name,
            title=entry["title"],
            link=entry["link"],
            description=entry["description"],
            published=entry["published"],
        )
        return _clean_text(rendered, limit=380)

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
        channel_key = self._channel_key(network, channel)
        channel_state = self._seen.get(channel_key, {})
        seen_ids = set(channel_state.get(feed_name, []))
        new_entries = [entry for entry in entries if entry["id"] not in seen_ids]
        if not new_entries:
            return []
        self._mark_seen_ids(
            network, channel, feed_name, [entry["id"] for entry in new_entries]
        )
        maximum = self.registryValue("maximumAnnouncements", channel, network)
        return new_entries[:maximum]

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
        self._feed_cache[self._feed_key(irc.network, name)] = parsed
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
        prefix = f"{irc.network}:"
        for channel_key in list(self._seen.keys()):
            if not channel_key.startswith(prefix):
                continue
            self._seen[channel_key].pop(name, None)
            if not self._seen[channel_key]:
                self._seen.pop(channel_key, None)
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
