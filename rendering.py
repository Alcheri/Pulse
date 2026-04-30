###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

import string

from supybot import callbacks
from supybot.utils.str import format as supybot_format

try:
    from .feeds import clean_text
except ImportError:
    from feeds import clean_text

DEFAULT_HEADLINE_FORMAT = "$feed: $title <$link>"


def format_announce_change(action, channel, feeds):
    names = [callbacks.canonicalName(feed) for feed in feeds]
    if action == "add":
        return f"Now announcing {supybot_format('%L', names)} in {channel}."
    if action == "remove":
        return f"Stopped announcing {supybot_format('%L', names)} in {channel}."
    raise ValueError(f"Unknown announce action: {action}")


def render_entry(feed_name, entry, template=DEFAULT_HEADLINE_FORMAT):
    rendered = string.Template(template).safe_substitute(
        feed=feed_name,
        title=entry["title"],
        link=entry["link"],
        description=entry["description"],
        published=entry["published"],
    )
    return clean_text(rendered, limit=380)


# vim:set shiftwidth=4 softtabstop=4 expandtab textwidth=79:
