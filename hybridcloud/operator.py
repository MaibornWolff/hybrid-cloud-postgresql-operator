import asyncio
from datetime import datetime, timedelta
import base64
import json
import os
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


@kopf.on.login(errors=kopf.ErrorsMode.TEMPORARY, retries=5)
def login_fn(**kwargs):
    token_path = os.getenv("TOKEN_PATH")
    if token_path:
        # This is a workaround for https://github.com/nolar/kopf/issues/980
        try:
            logging.info(f"Using token from {token_path} for authentication")
            with open(token_path) as f:
                token = f.read()
            try:
                _header, payload, _sig = token.split(".", 2)
                # Decode the token payload, add padding if necessary
                payload = base64.b64decode(payload + '=' * (-len(payload) % 4))
                payload = json.loads(payload)
                exp = payload["exp"]
                dt = datetime.fromtimestamp(exp)
            except:
                logging.exception("Could not parse token, falling back to default expiration 1h")
                dt = datetime.now() + timedelta(hours=1)
            return kopf.ConnectionInfo(
                server='https://kubernetes.default.svc.cluster.local',
                insecure=True,
                scheme='Bearer',
                token=token,
                expiration=dt,
            )
        except:
            logging.exception("Failed to use token. Falling back to normal kube-client authentication")
            return kopf.login_via_client(**kwargs)
    else:
        return kopf.login_via_client(**kwargs)


# Check config to abort early in case of problem
config.verify()


def run():
    """Used to run the operator when not run via kopf cli"""
    asyncio.run(kopf.operator())
