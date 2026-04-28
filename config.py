###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

from supybot import conf, registry

try:
    from supybot.i18n import PluginInternationalization

    _ = PluginInternationalization("Pulse")
except ImportError:
    _ = lambda x: x


def configure(advanced):
    conf.registerPlugin("Pulse", True)


Pulse = conf.registerPlugin("Pulse")

conf.registerGlobalValue(
    Pulse,
    "pollIntervalSeconds",
    registry.PositiveInteger(
        300, _("""Sets how often Pulse checks announced feeds for updates.""")
    ),
)

conf.registerGlobalValue(
    Pulse,
    "requestTimeoutSeconds",
    registry.PositiveInteger(
        10, _("""Sets the HTTP timeout, in seconds, for feed requests.""")
    ),
)

conf.registerGlobalValue(
    Pulse,
    "maxFeedBytes",
    registry.PositiveInteger(
        1048576, _("""Sets the maximum feed response size, in bytes.""")
    ),
)

conf.registerGlobalValue(
    Pulse,
    "startupDelaySeconds",
    registry.NonNegativeInteger(
        15, _("""Sets how long Pulse waits before its first announce poll.""")
    ),
)

conf.registerGlobalValue(
    Pulse,
    "initialBackfillCount",
    registry.NonNegativeInteger(
        0,
        _(
            """Sets how many existing items Pulse announces when a feed is first added
            to a channel's announce list. A value of 0 marks existing items as seen
            without announcing them."""
        ),
    ),
)

conf.registerChannelValue(
    Pulse,
    "enabled",
    registry.Boolean(False, _("""Determines whether Pulse announces in this channel.""")),
)

conf.registerChannelValue(
    Pulse,
    "announceFeeds",
    registry.SpaceSeparatedSetOfStrings(
        [], _("""Lists the feed names that Pulse announces in this channel.""")
    ),
)

conf.registerChannelValue(
    Pulse,
    "maximumAnnouncements",
    registry.PositiveInteger(
        3, _("""Sets the maximum number of new feed items announced at once.""")
    ),
)

conf.registerChannelValue(
    Pulse,
    "announceAsNotice",
    registry.Boolean(
        False, _("""Determines whether feed announcements are sent as notices.""")
    ),
)

conf.registerChannelValue(
    Pulse,
    "headlineFormat",
    registry.String(
        "$feed: $title <$link>",
        _("""Sets the template used for feed headlines and announcements."""),
    ),
)


# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
