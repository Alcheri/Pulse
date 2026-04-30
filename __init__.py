###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###

"""
Pulse: RSS 2.0 feed polling and announcement plugin for Limnoria.
"""

import supybot
from supybot import world

__version__ = "0.2.0"

__author__ = supybot.Author("Barry Suridge", "Alcheri", "barry.suridge@gmail.com")

__contributors__ = {}

__url__ = "https://github.com/Alcheri/Pulse"

from . import config
from . import feeds
from . import plugin
from . import rendering
from . import storage
from importlib import reload

reload(config)
reload(feeds)
reload(storage)
reload(rendering)
reload(plugin)

if world.testing:
    from . import test

Class = plugin.Class
configure = config.configure


# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
