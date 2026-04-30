###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

import hashlib
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

import supybot.ircutils as ircutils
from supybot import callbacks

USER_AGENT = "Limnoria-Pulse/0.1 (+https://github.com/Alcheri/Pulse)"
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


class FeedError(callbacks.Error):
    pass


def clean_text(value, limit=None):
    text = ircutils.stripFormatting(str(value or ""))
    text = CONTROL_CHARS_RE.sub("", text)
    text = " ".join(text.split())
    if limit is not None and len(text) > limit:
        return f"{text[: max(0, limit - 3)].rstrip()}..."
    return text


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(element, name):
    for child in list(element):
        if local_name(child.tag) == name:
            return clean_text(child.text)
    return ""


def stable_entry_id(guid="", link="", title="", description=""):
    guid = clean_text(guid, limit=512)
    if guid:
        return guid

    link = clean_text(link, limit=512)
    if link:
        return link

    basis = " ".join(
        part for part in (clean_text(title, 256), clean_text(description, 256)) if part
    )
    if not basis:
        raise FeedError("Feed entry is missing guid, link, title, and description.")
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def parse_rss2_feed(xml_bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FeedError(f"Malformed XML: {e}") from e

    if local_name(root.tag) != "rss":
        raise FeedError("Unsupported feed format: expected an RSS 2.0 document.")

    version = (root.attrib.get("version") or "").strip()
    if not version.startswith("2."):
        raise FeedError("Unsupported RSS version: only RSS 2.0 feeds are supported.")

    channel = None
    for child in list(root):
        if local_name(child.tag) == "channel":
            channel = child
            break
    if channel is None:
        raise FeedError("RSS feed is missing a channel element.")

    metadata = {
        "title": child_text(channel, "title") or "Untitled feed",
        "link": child_text(channel, "link"),
        "description": child_text(channel, "description"),
        "language": child_text(channel, "language"),
        "items": [],
    }

    for item in list(channel):
        if local_name(item.tag) != "item":
            continue
        title = child_text(item, "title") or "Untitled item"
        link = child_text(item, "link")
        description = child_text(item, "description")
        published = child_text(item, "pubDate")
        guid = child_text(item, "guid")
        metadata["items"].append(
            {
                "id": stable_entry_id(guid, link, title, description),
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


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
