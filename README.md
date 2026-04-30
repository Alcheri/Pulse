<!-- Pulse: RSS 2.0 feed and announcement plugin for Limnoria. -->

# Pulse

[![Tests](https://github.com/Alcheri/WorldTime/actions/workflows/tests.yml/badge.svg?branch=Limnoria-WorldTime)](https://github.com/Alcheri/WorldTime/actions/workflows/tests.yml)
[![Lint](https://github.com/Alcheri/WorldTime/actions/workflows/lint.yml/badge.svg?branch=Limnoria-WorldTime)](https://github.com/Alcheri/WorldTime/actions/workflows/lint.yml)
[![CodeQL](https://github.com/Alcheri/WorldTime/actions/workflows/codeql.yml/badge.svg?branch=Limnoria-WorldTime)](https://github.com/Alcheri/WorldTime/actions/workflows/codeql.yml)

<p>
  <em>RSS 2.0 feed polling and announcement plugin for Limnoria.</em>
</p>

## Features

- Registers named RSS 2.0 feeds per bot network for later use
- Fetches the latest feed items on demand
- Announces new feed items to configured channels
- Persists registered feeds and seen item IDs across reloads
- Rejects unsupported feed formats instead of pretending they worked

## Install

From your Limnoria plugins directory:

```bash
git clone https://github.com/Alcheri/Pulse.git
```

Install any runtime requirements:

```bash
pip install --upgrade -r requirements.txt
```

Load the plugin:

```text
/msg bot load Pulse
```

## Configuration

Enable Pulse for a channel:

```text
config channel #channel plugins.Pulse.enabled True
```

Add a feed on the current bot network:

```text
@pulse add xkcd https://xkcd.com/rss.xml
```

Note: the current implementation is intentionally **RSS 2.0-first**. Atom feeds are
not part of the supported surface yet.

Subscribe the current channel to announcements:

```text
@pulse announce add limnorianews
```

That command means "announce the registered feed `limnorianews` here". To
target another channel explicitly, use:

```text
@pulse announce add #channel limnorianews
```

Default first-subscribe behaviour is **no backfill**: existing items are marked as
seen without being announced.

Feed names are scoped to the bot network where they are added. Channel
announcement settings and seen item tracking are scoped to each network/channel
pair, so the same channel name on different networks keeps separate state.

### Configuration Variables

Global values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `plugins.Pulse.pollIntervalSeconds` | `300` | Seconds between checks for announced feeds. |
| `plugins.Pulse.requestTimeoutSeconds` | `10` | HTTP timeout for feed requests. |
| `plugins.Pulse.maxFeedBytes` | `1048576` | Maximum feed response size in bytes. |
| `plugins.Pulse.startupDelaySeconds` | `15` | Delay before the first announce poll after startup. |
| `plugins.Pulse.initialBackfillCount` | `0` | Existing items to announce when first subscribing a channel; `0` marks existing items as seen without announcing them. |

Channel values:

| Variable | Default | Purpose |
| --- | --- | --- |
| `plugins.Pulse.enabled` | `False` | Enables announcements in the channel. |
| `plugins.Pulse.announceFeeds` | empty | Feed names announced in the channel. Usually managed with `@pulse announce add/remove`. |
| `plugins.Pulse.maximumAnnouncements` | `3` | Maximum new items announced per feed check. |
| `plugins.Pulse.announceAsNotice` | `False` | Sends feed announcements as notices instead of channel messages. |
| `plugins.Pulse.headlineFormat` | `$feed: $title <$link>` | Template for feed headlines and announcements. |

## Commands

```text
@pulse add <name> <url>
@pulse remove <name>
@pulse list
@pulse show <name>
@pulse latest <name> [count]
@pulse refresh [name]
@pulse announce list [#channel]
@pulse announce add [#channel] <feed> [feed...]
@pulse announce remove [#channel] <feed> [feed...]
```

## Notes

- Feed output uses the channel `headlineFormat` template.
- Pulse stores network-scoped feed state under Limnoria's `data/` directory.
- A later phase can add feed-specific formatting, keyword filters, and richer
  status reporting.

This project is licensed under the BSD 3-Clause Licence. See the [LICENCE](LICENCE.md) file for details.
