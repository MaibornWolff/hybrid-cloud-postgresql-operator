import asyncio
import logging
import random
import kopf
from . import config
# Import the handlers so kopf sees them
from .handlers import postgresql_server, postgresql_database


logger = logging.getLogger('azure')
logger.setLevel(logging.WARNING)
# Supress unneeded error about missing gi module (https://github.com/AzureAD/microsoft-authentication-extensions-for-python/wiki/Encryption-on-Linux)
logger = logging.getLogger('msal_extensions.libsecret')
logger.setLevel(logging.CRITICAL)
logger = logging.getLogger('aiohttp.access')
logger.setLevel(logging.WARNING)


class InfiniteBackoffsWithJitter:
    def __iter__(self):
        while True:
            yield 10 + random.randint(-5, +5)


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_):
    # We don't want normal log messages in the events of the objects
    settings.posting.level = logging.CRITICAL
    # Infinite Backoffs so the operator never stops working in case of kubernetes errors
    settings.networking.error_backoffs = InfiniteBackoffsWithJitter()
    settings.batching.error_delays = InfiniteBackoffsWithJitter()
    settings.watching.server_timeout = 60
    settings.watching.connect_timeout = 60
    settings.watching.client_timeout = 120
    settings.networking.request_timeout = 120


# Check config to abort early in case of problem
config.verify()


def run():
    """Used to run the operator when not run via kopf cli"""
    asyncio.run(kopf.operator())
