from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.network.models import PrivateEndpoint, Subnet, PrivateLinkServiceConnection
from azure.mgmt.rdbms.postgresql.models import ServerForCreate, ServerPropertiesForDefaultCreate, ServerUpdateParameters, ServerVersion, Sku, StorageProfile, Database, Configuration, VirtualNetworkRule, FirewallRule
from azure.mgmt.resource.locks.models import ManagementLockObject
from .pgclient import PostgresSQLClient
from ..config import get_one_of, config_get
from ..util.azure import azure_client_locks, azure_client_postgres, azure_client_network, azure_client_privatedns
from ..util.reconcile_helpers import field_from_spec



def _backend_config(key, default=None, fail_if_missing=False):
    return get_one_of(f"backends.azurepostgres.{key}", f"backends.azure.{key}", default=default, fail_if_missing=fail_if_missing)


EXTENSIONS_PARAMETER = "shared_preload_libraries"


def _calc_name(namespace, name):
    # Allow admins to override names so that existing storage accounts not following the schema can still be managed
    name_overrides = _backend_config("name_overrides", default=[])
    for override in name_overrides:
        if override["namespace"] == namespace and override["name"] == name:
            return override["azure_name"]
    return _backend_config("name_pattern", fail_if_missing=True).format(namespace=namespace, name=name)


class AzurePostgreSQLBackend:

    def __init__(self, logger):
        self._db_client = azure_client_postgres()
        self._dns_client = azure_client_privatedns()
        self._network_client = azure_client_network()
        self._lock_client = azure_client_locks()
        self._subscription_id = _backend_config("subscription_id", fail_if_missing=True)
        self._location = _backend_config("location", fail_if_missing=True)
        self._resource_group = _backend_config("resource_group", fail_if_missing=True)
        self._virtual_network = _backend_config("virtual_network")
        self._subnet = _backend_config("subnet")
        self._logger = logger

    def server_spec_valid(self, namespace, name, spec):
        server_name = _calc_name(namespace, name)
        if len(server_name) > 63:
            return (False, f"calculated server name '{server_name}' is longer than 63 characters")
        size = spec.get("size", dict())
        cpu_limit = _backend_config("cpu_limit", 0)
        storage_limit = _backend_config("storage_limit_gb", 0)
        if cpu_limit and size.get("cpu", 1) > cpu_limit:
            return (False, f"size.cpu is limited to {cpu_limit}")
        if storage_limit and size.get("storageGB", 1) > storage_limit:
            return (False, f"size.storageGB is limited to {storage_limit} GB")
        if size.get("storageGB", 100) < 100:
            return (False, f"size.storageGB must be at least 100 GB")
        return (True, "")

    def server_exists(self, namespace, name):
        server_name = _calc_name(namespace, name)
        try:
            return self._db_client.servers.get(self._resource_group, server_name)
        except ResourceNotFoundError:
            return False

    def database_exists(self, namespace, server_name, database_name, admin_credentials=None):
        server_name = _calc_name(namespace, server_name)
        try:
            self._db_client.databases.get(self._resource_group, server_name, database_name)
            return True
        except ResourceNotFoundError:
            return False

    def create_or_update_server(self, namespace, name, spec, password, admin_password_changed=False):
        warnings = []
        server_name = _calc_name(namespace, name)
        sku, sku_warnings = _determine_sku(spec.get("size", {}))
        warnings.extend(sku_warnings)
        geo_redundant_backup = _backend_config("parameters.geo_redundant_backup")
        geo_redundant_backup = field_from_spec(spec, "backup.geoRedundant", default=geo_redundant_backup)
        if geo_redundant_backup:
            geo_redundant_backup = "Enabled"
        else:
            geo_redundant_backup = "Disabled"
        create_private_endpoint = config_get("backends.azurepostgres.network.create_private_endpoint")
        public_access = _backend_config("network.public_access")
        if public_access is None:
            public_network_access = "Disabled" if config_get("backends.azurepostgres.network.create_private_endpoint", default=False) else "Enabled"
        else:
            public_network_access = "Enabled" if public_access else "Disabled"
        storage_autogrow = "Enabled" if spec.get("size", dict()).get("storageAutoGrow", False) else "Disabled"
        infrastructure_encryption = config_get("backends.azurepostgres.parameters.infrastructure_encryption", default="Disabled")
        backup_retention_days = field_from_spec(spec, "backup.retentionDays", default=_backend_config("parameters.backup_retention_days", default=7))
        storage_mb=int(spec.get("size", dict()).get("storageGB", 10))*1024
        admin_username = _backend_config("admin_username", default="postgres")
        version = _map_version(spec.get("version"))
        tags = {"hybridcloud-postgresql-operator:namespace": namespace, "hybridcloud-postgresql-operator:name": name}
        for k, v in _backend_config("tags", default={}).items():
            tags[k] = v.format(namespace=namespace, name=name)

        try:
            server = self._db_client.servers.get(self._resource_group, server_name)
            def compare():
                if server.sku != sku:
                    return True
                if server.public_network_access != public_network_access:
                    return True
                if server.version != version:
                    return True
                if server.storage_profile.storage_autogrow != storage_autogrow or server.storage_profile.storage_mb != storage_mb:
                    return True
                if server.storage_profile.backup_retention_days != backup_retention_days or server.storage_profile.geo_redundant_backup != geo_redundant_backup:
                    return True
                if server.tags != tags:
                    return True
                return False
            changed = compare() or admin_password_changed
        except:
            server = None
            changed = True

        if not server:
            poller = self._db_client.servers.begin_create(self._resource_group,
                server_name, 
                ServerForCreate(
                    location=self._location,
                    sku=sku,
                    properties=ServerPropertiesForDefaultCreate(
                        ssl_enforcement="Enabled",
                        infrastructure_encryption=infrastructure_encryption,
                        administrator_login=admin_username,
                        administrator_login_password=password,
                        version=version,
                        public_network_access=public_network_access,
                        storage_profile=StorageProfile(
                            backup_retention_days=backup_retention_days,
                            geo_redundant_backup=geo_redundant_backup,
                            storage_autogrow=storage_autogrow,
                            storage_mb=storage_mb
                        )
                    ),
                    tags=tags
                )
            )
            server = poller.result()
        elif changed:
            parameters = ServerUpdateParameters(
                sku=sku,
                public_network_access=public_network_access,
                storage_profile=StorageProfile(
                    backup_retention_days=backup_retention_days,
                    geo_redundant_backup=geo_redundant_backup,
                    storage_autogrow=storage_autogrow,
                    storage_mb=storage_mb
                ),
                administrator_login_password=password,
                version=version,
                tags=tags
            )
            poller = self._db_client.servers.begin_update(self._resource_group, server_name, parameters)
            server = poller.result()

        if _backend_config("lock_from_deletion", default=False):
            self._lock_client.management_locks.create_or_update_at_resource_level(self._resource_group, "Microsoft.DBforPostgreSQL", "", "servers", server_name, "DoNotDeleteLock", parameters=ManagementLockObject(level="CanNotDelete", notes="Protection from accidental deletion"))

        if public_network_access == "Enabled":
            # vnets
            existing_vnet_rules = dict()
            for vnet in self._db_client.virtual_network_rules.list_by_server(self._resource_group, server_name):
                existing_vnet_rules[vnet.name] = vnet
            for config in config_get("backends.azurepostgres.network.vnets", default=[]):
                vnet = config["vnet"]
                subnet = config["subnet"]
                rule_name = f"{vnet}-{subnet}"
                subnet_id = f"/subscriptions/{self._subscription_id}/resourceGroups/{self._resource_group}/providers/Microsoft.Network/virtualNetworks/{vnet}/subnets/{subnet}"
                if rule_name in existing_vnet_rules:
                    existing = existing_vnet_rules.pop(rule_name)
                    if existing.virtual_network_subnet_id == subnet_id:
                        # Rule is the same, skip update
                        continue
                poller = self._db_client.virtual_network_rules.begin_create_or_update(self._resource_group, server_name, rule_name, VirtualNetworkRule(virtual_network_subnet_id=subnet_id, ignore_missing_vnet_service_endpoint=True))
                poller.result()
            for rule in existing_vnet_rules.keys():
                self._db_client.virtual_network_rules.begin_delete(self._resource_group, server_name, rule).result()

            # firewall rules
            existing_rules = dict()
            for rule in self._db_client.firewall_rules.list_by_server(self._resource_group, server_name):
                existing_rules[rule.name] = rule
            extra_rules = []
            config_rules = _backend_config("parameters.network.firewall_rules", default=[])
            spec_rules = field_from_spec(spec, "network.firewallRules", [])
            if _backend_config("network.allow_azure_services", default=False):
                # There is no extra option to allow access for azure services, instead a special firewall rule is added
                extra_rules.append(dict(name="AllowAllWindowsAzureIps", start_ip="0.0.0.0", end_ip="0.0.0.0"))
            for rule in config_rules + spec_rules + extra_rules:
                if rule["name"] in existing_rules:
                    existing = existing_rules.pop(rule["name"])
                    # Rule is the same, skip update
                    if existing.start_ip_address == rule["start_ip"] and existing.end_ip_address == rule["end_ip"]:
                        continue
                poller = self._db_client.firewall_rules.begin_create_or_update(self._resource_group, server_name, rule["name"], FirewallRule(start_ip_address=rule["start_ip"], end_ip_address=rule["end_ip"]))
                poller.result()
            for rule in existing_rules.keys():
                self._db_client.firewall_rules.begin_delete(self._resource_group, server_name, rule).result()

        if create_private_endpoint:
            self._logger.info("Creating private endpoint for server")
            # Create private endpoint
            poller = self._network_client.private_endpoints.begin_create_or_update(
                self._resource_group,            
                server_name,
                parameters=PrivateEndpoint(
                    location=self._location, 
                    subnet=Subnet(id=f"/subscriptions/{self._subscription_id}/resourceGroups/{self._resource_group}/providers/Microsoft.Network/virtualNetworks/{self._virtual_network}/subnets/{self._subnet}"),
                    private_link_service_connections=[PrivateLinkServiceConnection(
                        name=f"link-{server_name}",
                        private_link_service_id=server.id,
                        group_ids=["postgresqlServer"]
                    )],
                )
            )
            private_endpoint = poller.result()
            # Create private DNS record
            self._dns_client.record_sets.create_or_update(        
                self._resource_group,
                'postgres.database.azure.com',
                'A',
                server_name,
                {
                    "ttl": 30,
                    "arecords": [{"ipv4_address": private_endpoint.custom_dns_configs[0].ip_addresses[0]}]
                }
            )

        # Handle extensions
        extensions = spec.get("extensions", [])
        extensions.sort()
        try:
            configuration = self._db_client.configurations.get(self._resource_group, server_name, EXTENSIONS_PARAMETER)
            if configuration.value:
                applied_extensions = configuration.value.split(",")
                applied_extensions.sort()
            else:
                applied_extensions = []
        except ResourceNotFoundError:
            applied_extensions = []
        
        if extensions != applied_extensions:
            self._logger.info(f"Updating list of extensions from {','.join(applied_extensions)} to {','.join(extensions)}")
            # Update configuration
            poller = self._db_client.configurations.begin_create_or_update(self._resource_group, server_name, EXTENSIONS_PARAMETER, Configuration(value=",".join(extensions), source="user-override"))
            poller.result()
            # Restart server
            self._logger.info("Restarting server due to changed extensions preload configuration")
            poller = self._db_client.servers.begin_restart(self._resource_group, server_name)
            poller.result()
        
        # Prepare credentials
        data = {
            "username": f"{admin_username}@{server_name}",
            "password": password,
            "dbname": "postgres",
            "host": server.fully_qualified_domain_name,
            "port": "5432",
            "sslmode": "require"
        }
        return data, warnings

    def create_or_update_database(self, namespace, server_name, database_name, spec, admin_credentials=None):
        server_name = _calc_name(namespace, server_name)
        parameters = Database(charset=field_from_spec(spec, "database.charset", default="UTF8"), collation=field_from_spec(spec, "database.collation", default="English_United States.1252"))
        poller = self._db_client.databases.begin_create_or_update(self._resource_group, server_name, database_name, parameters)
        poller.result()
        pgclient = self._pgclient(admin_credentials, database_name)
        pgclient.restrict_database_permissions(database_name)
        extensions = field_from_spec(spec, "database.extensions", default=[])
        if not extensions:
            return
        for extension in extensions:
            self._logger.info(f"Enabling extension {extension}")
            pgclient.create_extension(extension)

    def delete_server(self, namespace, name):
        server_name = _calc_name(namespace, name)
        delete_fake = _backend_config("server_delete_fake", default=False)
        if delete_fake:
            # Set tag to mark server as deleted
            parameters = ServerUpdateParameters(
                tags={"hybridcloud-postgresql-operator:namespace": namespace, "hybridcloud-postgresql-operator:name": name, "hybridcloud-postgresql-operator:marked-for-deletion": "yes"}
            )
            poller = self._db_client.servers.begin_update(self._resource_group, server_name, parameters)
            poller.result()
            return
        if config_get("backends.azurepostgres.create_private_endpoint", default=False):
            poller = self._network_client.private_endpoints.begin_delete(self._resource_group, server_name)
            poller.result()
            self._dns_client.record_sets.delete(self._resource_group, 'postgres.database.azure.com', 'A', server_name)
        poller = self._db_client.servers.begin_delete(self._resource_group, server_name)
        poller.result()

    def delete_database(self, namespace, server_name, database_name, admin_credentials=None):
        server_name = _calc_name(namespace, server_name)
        delete_fake = _backend_config("database_delete_fake", default=False)
        if delete_fake:
            # Do nothing
            return
        poller = self._db_client.databases.begin_delete(self._resource_group, server_name, database_name)
        poller.result()
    
    def create_or_update_user(self, namespace, server_name, database_name, username, password, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        newly_created = pgclient.create_or_update_user(username, password, database_name)
        server_host = admin_credentials["host"]
        return newly_created, {
            "username": f"{username}@{server_host}",
            "password": password,
            "dbname": database_name,
            "host": admin_credentials["host"],
            "port": "5432",
            "sslmode": "require"
        }

    def delete_user(self, namespace, server_name, username, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.delete_user(username)

    def update_user_password(self, namespace, server_name, username, password, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        pgclient.update_password(username, password)

    def _pgclient(self, admin_credentials, dbname=None) -> PostgresSQLClient:
        return PostgresSQLClient(admin_credentials, dbname)


def _determine_sku(size_spec):
    warnings = []
    size_class = size_spec.get("class")
    default_class = config_get("backends.azurepostgres.default_class")
    if size_class and default_class:
        classes = config_get("backends.azurepostgres.classes", default=[])
        if not size_class in classes:
            warnings.append(f"selected class {size_class} is not allowed. Falling back to default {default_class}")
            size_class = default_class
        selected_class = classes[size_class]
        return Sku(name=selected_class["name"], tier=selected_class["tier"], family=selected_class["family"], capacity=selected_class["capacity"]), warnings

    cpu = int(size_spec.get("cpu", 1))
    if cpu > 64:
        warnings.append(f"Selected numbers of cpus ({cpu}) is more than is allowed with Azure. Setting to maximum allowed 64")
    capacity = 64
    for step in [2, 4, 8, 16, 32, 64]:
        if cpu <= step:
            capacity = step
            break
    return Sku(name=f"GP_Gen5_{capacity}", tier="GeneralPurpose", family="Gen5", capacity=capacity), warnings


def _map_version(version: str):
    if not version:
        return ServerVersion.ELEVEN
    elif version.startswith("10"):
        return ServerVersion.TEN2
    else:
        return ServerVersion.ELEVEN
