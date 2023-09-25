from ..backends.aws_aurora import AwsAuroraBackend
from ..backends.aws_rds import AwsRdsBackend
from ..backends.azure_postgresql import AzurePostgreSQLBackend
from ..backends.azure_postgresqlflexible import AzurePostgreSQLFlexibleBackend
from ..backends.helm_postgres import HelmPostgreSQLBackend
from ..backends.helm_yugabyte import HelmYugabyteBackend
from ..config import config_get, ConfigurationException


_backends = {
    "awsaurora": AwsAuroraBackend,
    "awsrds": AwsRdsBackend,
    "azurepostgres": AzurePostgreSQLBackend,
    "azurepostgresflexible": AzurePostgreSQLFlexibleBackend,
    "helmbitnami": HelmPostgreSQLBackend,
    "helmyugabyte": HelmYugabyteBackend,
}


def postgres_backend(selected_backend, logger) -> AzurePostgreSQLBackend:
    backend = config_get("backend", fail_if_missing=True)
    if backend not in _backends.keys():
        raise ConfigurationException(f"Unknown backend: {backend}")
    if selected_backend:
        if selected_backend not in _backends.keys():
            logger.warn(f"Selected backend {selected_backend} is unknown. Defaulting to {backend}")
            selected_backend = backend
    else:
        selected_backend = backend
    return _backends[selected_backend](logger)
