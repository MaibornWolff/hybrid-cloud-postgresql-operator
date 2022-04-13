from azure.identity import DefaultAzureCredential
from azure.mgmt.rdbms.postgresql import PostgreSQLManagementClient
from azure.mgmt.rdbms.postgresql_flexibleservers import PostgreSQLManagementClient as PostgreSQLFlexibleManagementClient
from azure.mgmt.privatedns import PrivateDnsManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ManagementLockClient
from ..config import get_one_of


def _subscription_id():
    return get_one_of("backends.azurepostgresflexible.subscription_id", "backends.azurepostgres.subscription_id", "backends.azure.subscription_id", fail_if_missing=True)


def _credentials():
    return DefaultAzureCredential()


def azure_client_postgres():
    return PostgreSQLManagementClient(_credentials(), _subscription_id())


def azure_client_postgres_flexible():
    return PostgreSQLFlexibleManagementClient(_credentials(), _subscription_id())


def azure_client_privatedns():
    return PrivateDnsManagementClient(_credentials(), _subscription_id())


def azure_client_network():
    return NetworkManagementClient(_credentials(), _subscription_id())


def azure_client_locks():
    return ManagementLockClient(_credentials(), _subscription_id())
