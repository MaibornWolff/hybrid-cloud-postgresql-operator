import os
from .pgclient import PostgresSQLClient
from ..config import config_get
from ..util import helm
from ..util import k8s
from ..util.constants import HELM_BASE_PATH


class HelmPostgreSQLBackend:

    def __init__(self, logger):
        self._logger = logger

    def server_spec_valid(self, namespace, name, spec):
        server_name = f"{name}-postgresql"
        if len(server_name) > 63:
            return (False, f"calculated server name '{server_name}' is longer than 63 characters")
        return (True, "")

    def server_exists(self, namespace: str, name: str):
        return helm.check_installed(namespace, f"{name}-postgresql")

    def create_or_update_server(self, namespace, name, spec, password, admin_password_changed=False):
        server_name = f"{name}-postgresql"
        cpu, mem = _map_size(spec.get("size", dict()))
        disksize = spec.get("size", dict()).get("storageGB", "10")
        admin_username = "postgres"
        values = f"""
fullnameOverride: {server_name}
global:
  postgresql:
    auth:
      postgresPassword: "{password}"
primary:
  resources:
    limits:
    memory: "{mem}"
    cpu: "{cpu}"
    requests:
    memory: "{mem}"
    cpu: "{cpu}"
  persistence:
    size: {disksize}Gi
        """
        helm.install_upgrade(namespace, server_name, os.path.join(HELM_BASE_PATH, "postgresql"), "--wait", values=values)
        return {
            "username": admin_username,
            "password": password,
            "dbname": "postgres",
            "host": f"{server_name}.{namespace}.svc.cluster.local",
            "port": "5432",
            "sslmode": "disable"
        }, []

    def delete_server(self, namespace, name):
        helm.uninstall(namespace, f"{name}-postgresql")
        if config_get("backends.helmbitnami.pvc_cleanup", default=False):
            for pvc in k8s.list_pvcs(namespace, f"data-{name}-postgresql-0"):
                k8s.delete_pvc(namespace, pvc.metadata.name)

    def database_exists(self, namespace, server_name, database_name, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        return pgclient.database_exists(database_name)

    def create_or_update_database(self, namespace, server_name, database_name, spec, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.create_database(database_name)
        pgclient.restrict_database_permissions(database_name)
        return True

    def delete_database(self, namespace, server_name, database_name, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.delete_database(database_name)

    def create_or_update_user(self, namespace, server_name, database_name, username, password, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        newly_created = pgclient.create_or_update_user(username, password, database_name)
        return newly_created, {
            "username": username,
            "password": password,
            "dbname": database_name,
            "host": f"{server_name}-postgresql.{namespace}.svc.cluster.local",
            "port": "5432",
            "sslmode": "disable"
        }

    def delete_user(self, namespace, server_name, username, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.delete_user(username)

    def update_user_password(self, namespace, server_name, username, password, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.update_password(username, password)

    def _pgclient(self, admin_credentials) -> PostgresSQLClient:
        return PostgresSQLClient(admin_credentials)


def _map_size(size_spec):
    size_class = size_spec.get("class")
    default_class = config_get("backends.helmbitnami.default_class")
    if size_class and default_class:
        classes = config_get("backends.helmbitnami.classes", default=[])
        if not size_class in classes:
            size_class = default_class
        selected_class = classes[size_class]
        return (selected_class["cpu"], selected_class["memory"])
    return (str(size_spec.get("cpu", "1")), str(size_spec.get("memoryMB", "256"))+"Mi")
