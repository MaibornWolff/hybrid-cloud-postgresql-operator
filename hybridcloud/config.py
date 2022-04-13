import os
import logging
import yaml


logger = logging.getLogger()
_config = None


class ConfigurationException(Exception):
    def __init__(self, description):
        super().__init(description)


def _get_config_value_from_env(key):
    return os.environ.get("HYBRIDCLOUD_" + key.replace('.', '_').upper())


def _get_config_value_from_config(config, key):
    ptr = config
    for var in key.split('.'):
        if ptr and var in ptr:
            ptr = ptr[var]
        else:
            return None
    return ptr


class Configuration:
    def __init__(self, configdata):
        self._data = configdata
    
    def get(self, key, default=None, fail_if_missing=False):
        """
        Retrieve a value from the operator config. Dict levels are dot separated in the key.
        :param key: the configuration key.
        :param default: Default value if key not found in config.
        :param fail_if_missing: If true and key is missing in config, log error and exit app.
        :return: the configured value or None.
        """
        value = _get_config_value_from_env(key)
        if not value:
            value = _get_config_value_from_config(self._data, key)
        if not value and fail_if_missing:
            logger.critical(f"Required configuration '{key}' is missing.")
            exit(-1)
        if value is None:
            value = default
        return value


def _load_config() -> Configuration:
    path = os.environ.get("OPERATOR_CONFIG", "config.yaml")
    with open(path) as f:
       configdata = yaml.safe_load(f)
    return Configuration(configdata)


def config() -> Configuration:
    global _config
    if not _config:
        _config = _load_config()
    return _config


def config_get(key, default=None, fail_if_missing=False):
    """Retrieve a value from the operator config. Dict levels are dot separated in the key."""
    return config().get(key, default, fail_if_missing)


def get_one_of(*keys, default=None, fail_if_missing=False):
    """Retrieve a value from the operator config. Keys are tried in order until one is found in the config.
    If none is found the default is returned, or if fail_if_missing is set to true an error is logged and the process exits."""
    for key in keys:
        result = config_get(key)
        if result is not None:
            return result
    if fail_if_missing:
        logger.critical(f"Required configuration '{keys[0]}' is missing. Aborting")
        exit(-1)
    return default


def verify():
    config_get("backend", fail_if_missing=True)
