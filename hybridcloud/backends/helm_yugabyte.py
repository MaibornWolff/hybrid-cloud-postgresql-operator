import os
from .pgclient import PostgresSQLClient
from ..config import config_get
from ..util import helm
from ..util import k8s
from ..util.constants import HELM_BASE_PATH


class HelmYugabyteBackend:

    def __init__(self, logger):
        self._logger = logger

    def server_spec_valid(self, namespace, name, spec):
        server_name = f"{name}-yugabyte"
        if len(server_name) > 63:
            return (False, f"calculated server name '{server_name}' is longer than 63 characters")
        return (True, "")

    def server_exists(self, namespace: str, name: str):
        return helm.check_installed(namespace, f"{name}-yugabyte")

    def create_or_update_server(self, namespace, name, spec, password, admin_password_changed=False):
        server_name = f"{name}-yugabyte"
        replicas_master = config_get("backends.helmyugabyte.replicas_master", default=1)
        replicas_tserver = config_get("backends.helmyugabyte.replicas_tserver", default=1)
        partitions_master = config_get("backends.helmyugabyte.partitions_master", default=1)
        partitions_tserver = config_get("backends.helmyugabyte.partitions_tserver", default=1)
        storage_class = config_get("backends.helmyugabyte.storage_class", default="")
        master_cpu, master_mem, tserver_cpu, tserver_mem = _map_size(spec.get("size", dict()))
        disksize = spec.get("size", dict()).get("storageGB", "10")
        values = f"""
storage:
  ephemeral: false
  master:
    count: 1
    size: {disksize}Gi
    storageClass: {storage_class}
  tserver:
    count: 1
    size: {disksize}Gi
    storageClass: {storage_class}

resource:
  master:
    requests:
      cpu: "{master_cpu}"
      memory: "{master_mem}"
    limits:
      cpu: "{master_cpu}"
      memory: "{master_mem}"
  tserver:
    requests:
      cpu: "{tserver_cpu}"
      memory: "{tserver_mem}"
    limits:
      cpu: "{tserver_cpu}"
      memory: "{tserver_mem}"

replicas:
  master: {replicas_master}
  tserver: {replicas_tserver}

partition:
  master: {partitions_master}
  tserver: {partitions_tserver}

authCredentials:
  ysql:
    password: "{password}"
Component: {server_name}
serviceEndpoints: []
        """
        helm.install_upgrade(namespace, server_name, os.path.join(HELM_BASE_PATH, "yugabyte"), "--wait", values=values)
        return {
            "username": "yugabyte",
            "password": password,
            "dbname": "postgres",
            "host": f"yb-tservers.{namespace}.svc.cluster.local",
            "port": "5433",
            "sslmode": "disable"
        }, []

    def delete_server(self, namespace, name):
        helm.uninstall(namespace, f"{name}-yugabyte")
        if config_get("backends.helmyugabyte.pvc_cleanup", default=False):
            for pvc in k8s.list_pvcs(namespace, r"datadir.*-yb-.*"):
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
            "host": admin_credentials["host"],
            "port": "5433",
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
    default_class = config_get("backends.helmyugabyte.default_class")
    if size_class and default_class:
        classes = config_get("backends.helmyugabyte.classes", default=[])
        if not size_class in classes:
            size_class = default_class
        selected_class = classes[size_class]
        master = selected_class["master"]
        tserver = selected_class["tserver"]
        return master["cpu"], master["memory"], tserver["cpu"], tserver["memory"]
    cpu = str(size_spec.get("cpu", "1"))
    mem = str(size_spec.get("memoryMB", "256"))+"Mi"
    return cpu, mem, cpu, mem
