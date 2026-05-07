###
# Copyright (c) 2026, Barry Suridge
# All rights reserved.
#
#
###


def pytest_sessionfinish(session, exitstatus):
    """Detach Limnoria's stdout handler before pytest closes captured streams."""
    try:
        import supybot.log as supybot_log
    except ImportError:
        return

    logger = getattr(supybot_log, "_logger", None)
    if logger is None:
        return

    for handler in list(logger.handlers):
        if handler.__class__.__name__ == "StdoutStreamHandler":
            logger.removeHandler(handler)


# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
