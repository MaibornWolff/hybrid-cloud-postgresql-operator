from datetime import datetime
from azure.core.exceptions import ResourceNotFoundError
from azure.mgmt.rdbms.postgresql_flexibleservers.models import ServerVersion, Sku, Database, Configuration, FirewallRule, Server, ServerForUpdate, Storage, Backup, Network, HighAvailability, MaintenanceWindow
from azure.mgmt.resource.locks.models import ManagementLockObject
import kopf
from .pgclient import PostgresSQLClient
from ..config import get_one_of, config_get
from ..util.azure import azure_client_locks, azure_client_postgres_flexible, azure_client_network, azure_client_privatedns
from ..util.reconcile_helpers import field_from_spec


def _backend_config(key, default=None, fail_if_missing=False):
    return get_one_of(f"backends.azurepostgresflexible.{key}", f"backends.azure.{key}", default=default, fail_if_missing=fail_if_missing)


PRELOAD_PARAMETER = "shared_preload_libraries"
EXTENSIONS_PARAMETER = "azure.extensions"
PRELOAD_LIST = ["timescaledb", "pg_cron", "pg_partman_bgw", "pg_partman", "pg_prewarm", "pg_stat_statements", "pgaudit", "pglogical", "wal2json"]

IGNORE_RESET_PARAMETERS = [PRELOAD_PARAMETER, EXTENSIONS_PARAMETER, "log_autovacuum_min_duration", "vacuum_cost_page_miss"]


def _calc_name(namespace, name):
    # Allow admins to override names so that existing storage accounts not following the schema can still be managed
    name_overrides = _backend_config("name_overrides", default=[])
    for override in name_overrides:
        if override["namespace"] == namespace and override["name"] == name:
            return override["azure_name"]
    return _backend_config("name_pattern", fail_if_missing=True).format(namespace=namespace, name=name)


class AzurePostgreSQLFlexibleBackend:

    def __init__(self, logger):
        self._db_client = azure_client_postgres_flexible()
        self._dns_client = azure_client_privatedns()
        self._network_client = azure_client_network()
        self._lock_client = azure_client_locks()
        self._subscription_id = _backend_config("subscription_id", fail_if_missing=True)
        self._location = _backend_config("location", fail_if_missing=True)
        self._resource_group = _backend_config("resource_group", fail_if_missing=True)
        self._virtual_network = _backend_config("virtual_network")
        self._subnet = _backend_config("subnet")
        self._private_dns_zone = _backend_config("dns_zone.name", default=_backend_config("dns_zone"))
        self._private_dns_zone_resource_group = _backend_config("dns_zone.resource_group", default=self._resource_group)
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
        if size.get("storageGB", 32) < 32:
            return (False, f"size.storageGB must be at least 32 GB")
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
        public_access = _backend_config("network.public_access", default=True)
        if public_access:
            network = None
        else:
            subnet_id = f"/subscriptions/{self._subscription_id}/resourceGroups/{self._resource_group}/providers/Microsoft.Network/virtualNetworks/{self._virtual_network}/subnets/{self._subnet}"
            zone_id = f"/subscriptions/{self._subscription_id}/resourceGroups/{self._private_dns_zone_resource_group}/providers/Microsoft.Network/privateDnsZones/{self._private_dns_zone}"
            network = Network(delegated_subnet_resource_id=subnet_id, private_dns_zone_arm_resource_id=zone_id)
        backup_retention_days = field_from_spec(spec, "backup.retentionDays", default=_backend_config("parameters.backup_retention_days", default=7))
        backup = Backup(backup_retention_days=backup_retention_days, geo_redundant_backup=geo_redundant_backup)
        storage_gb = int(spec.get("size", dict()).get("storageGB", 32))
        admin_username = _backend_config("admin_username", default="postgres")
        maintenance_window = _parse_maintenance_window(spec)
        ha_enabled = "ZoneRedundant" if field_from_spec(spec, "highavailability.enabled", default=False) else "Disabled"
        standby_availability_zone = config_get("backends.azurepostgresflexible.standby_availability_zone", default="2")
        high_availability = HighAvailability(mode=ha_enabled, standby_availability_zone=standby_availability_zone if ha_enabled=="ZoneRedundant" else None)
        tags = {"hybridcloud-postgresql-operator:namespace": namespace, "hybridcloud-postgresql-operator:name": name}
        server_parameters = field_from_spec(spec, "serverParameters", default=dict())
        for k, v in _backend_config("tags", default={}).items():
            tags[k] = v.format(namespace=namespace, name=name)

        try:
            server = self._db_client.servers.get(self._resource_group, server_name)
            def compare():
                if server.sku != sku:
                    return True
                if server.storage.storage_size_gb != storage_gb:
                    return True
                if server.backup.backup_retention_days != backup.backup_retention_days or server.backup.geo_redundant_backup != backup.geo_redundant_backup:
                    return True
                if server.high_availability.mode != high_availability.mode or server.high_availability.standby_availability_zone != high_availability.standby_availability_zone:
                    return True
                if server.maintenance_window != maintenance_window:
                    return True
                if server.tags != tags:
                    return True
                return False
            changed = compare() or admin_password_changed
        except:
            server = None
            changed = True

        if not server:
            parameters = Server(
                location=self._location,
                sku=sku,
                administrator_login=admin_username,
                administrator_login_password=password,
                version=_map_version(spec.get("version")),
                storage=Storage(storage_size_gb=storage_gb),
                backup=backup,
                network=network,
                high_availability=high_availability,
                maintenance_window=maintenance_window,
                availability_zone=config_get("backends.azurepostgresflexible.availability_zone", default="1"),
                tags=tags
            )
            poller = self._db_client.servers.begin_create(self._resource_group,
                server_name, 
                parameters=parameters
            )
            server = poller.result()
        elif changed:
            parameters = ServerForUpdate(
                location=self._location,
                sku=sku,
                administrator_login_password=password,
                storage=Storage(storage_size_gb=storage_gb),
                backup=backup,
                high_availability=high_availability,
                maintenance_window=maintenance_window,
                tags=tags
            )
            poller = self._db_client.servers.begin_update(self._resource_group, server_name, parameters)
            server = poller.result()

        if _backend_config("lock_from_deletion", default=False):
            self._lock_client.management_locks.create_or_update_at_resource_level(self._resource_group, "Microsoft.DBforPostgreSQL", "", "flexibleServers", server_name, "DoNotDeleteLock", parameters=ManagementLockObject(level="CanNotDelete", notes="Protection from accidental deletion"))

        if public_access:
            self._logger.info("Setting firewall rules")
            # firewall rules
            existing_rules = dict()
            for rule in self._db_client.firewall_rules.list_by_server(self._resource_group, server_name):
                existing_rules[rule.name] = rule
            extra_rules = []
            config_rules = _backend_config("parameters.network.firewall_rules", default=[])
            spec_rules = field_from_spec(spec, "network.firewallRules", [])
            if _backend_config("network.allow_azure_services", default=False):
                # There is no extra option to allow access for azure services, instead a special firewall rule is added
                extra_rules.append(dict(name="AllowAllWindowsAzureIps", startIp="0.0.0.0", endIp="0.0.0.0"))
            for rule in config_rules + spec_rules + extra_rules:
                if rule["name"] in existing_rules:
                    existing = existing_rules.pop(rule["name"])
                    # Rule is the same, skip update
                    if existing.start_ip_address == rule["startIp"] and existing.end_ip_address == rule["endIp"]:
                        continue
                poller = self._db_client.firewall_rules.begin_create_or_update(self._resource_group, server_name, rule["name"], FirewallRule(start_ip_address=rule["startIp"], end_ip_address=rule["endIp"]))
                poller.result()
            for rule in existing_rules.keys():
                self._db_client.firewall_rules.begin_delete(self._resource_group, server_name, rule).result()

        # Handle extensions
        self._logger.info("Handling extensions")
        extensions = spec.get("extensions", [])
        extensions.extend(["pg_cron", "pg_stat_statements"])
        extensions.sort()
        preload_extensions = list(filter(lambda el: el in PRELOAD_LIST, extensions))
        try:
            configuration = self._db_client.configurations.get(self._resource_group, server_name, PRELOAD_PARAMETER)
            if configuration.value:
                applied_preload_extensions = configuration.value.split(",")
                applied_preload_extensions.sort()
            else:
                applied_preload_extensions = []
        except ResourceNotFoundError:
            applied_preload_extensions = []
        try:
            configuration = self._db_client.configurations.get(self._resource_group, server_name, EXTENSIONS_PARAMETER)
            if configuration.value:
                applied_allowed_extenions = configuration.value.split(",")
                applied_allowed_extenions.sort()
            else:
                applied_allowed_extenions = []
        except ResourceNotFoundError:
            applied_allowed_extenions = []
        
        # Keeps track of whether a restart is needed by changes to server configurations
        should_restart = False

        if preload_extensions != applied_preload_extensions:
            self._logger.info(f"Updating list of extensions from {','.join(applied_preload_extensions)} to {','.join(extensions)}")
            # Update configuration
            poller = self._db_client.configurations.begin_put(self._resource_group, server_name, PRELOAD_PARAMETER, Configuration(value=",".join(extensions), source="user-override"))
            poller.result()
            should_restart = True
        
        if extensions != applied_allowed_extenions:
            # Update configuration
            poller = self._db_client.configurations.begin_put(self._resource_group, server_name, EXTENSIONS_PARAMETER, Configuration(value=",".join(extensions), source="user-override"))
            poller.result()
            should_restart = True
        
        # Iterate through the server properties that are currently set on the server
        for parameter in self._db_client.configurations.list_by_server(self._resource_group, server_name):

            if parameter.is_read_only:
                continue

            # Extensions which are set above are part of the server properties and shouldn't be reset
            if parameter.name in IGNORE_RESET_PARAMETERS:
                continue

            changed = False
            value = ""

            # Comparing target server properties to current ones
            if parameter.name in server_parameters:
                # Update configuration if parameter changed
                if parameter.value != server_parameters[parameter.name]:
                    self._logger.info(f"Updating parameter {parameter.name} to {server_parameters[parameter.name]}")
                    value = server_parameters[parameter.name]
                    changed = True
            else:
                # Reset parameter if it got removed from config-file
                if parameter.value != parameter.default_value:
                    self._logger.info(f"Resetting parameter {parameter.name} to {parameter.default_value}")
                    value = parameter.default_value
                    changed = True

            if changed:
                poller = self._db_client.configurations.begin_put(self._resource_group, server_name, parameter.name, Configuration(value=value, source="user-override"))
                poller.result()
                should_restart = True

        if should_restart:
            # Restart server
            self._logger.info("Restarting server due to changed server parameters")
            poller = self._db_client.servers.begin_restart(self._resource_group, server_name)
            poller.result()
            self._logger.info("Initiated server restart")

        # Prepare credentials
        data = {
            "username": admin_username,
            "password": password,
            "dbname": "postgres",
            "host": server.fully_qualified_domain_name,
            "port": "5432",
            "sslmode": "require"
        }
        return data, warnings

    def create_or_update_database(self, namespace, server_name, database_name, spec, admin_credentials=None):
        server_name = _calc_name(namespace, server_name)
        try:
            database = self._db_client.databases.get(self._resource_group, server_name, database_name)
            changed = database.charset != field_from_spec(spec, "database.charset", default="UTF8") or database.collation != field_from_spec(spec, "database.collation", default="en_US.utf8") 
        except:
            changed = True
        if changed:
            parameters = Database(charset=field_from_spec(spec, "database.charset", default="UTF8"), collation=field_from_spec(spec, "database.collation", default="en_US.utf8") )
            poller = self._db_client.databases.begin_create(self._resource_group, server_name, database_name, parameters)
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
        if _backend_config("server_delete_fake", default=False):
            # Set tag to mark server as deleted
            parameters = ServerForUpdate(
                tags={"hybridcloud-postgresql-operator:namespace": namespace, "hybridcloud-postgresql-operator:name": name, "hybridcloud-postgresql-operator:marked-for-deletion": "yes"}
            )
            poller = self._db_client.servers.begin_update(self._resource_group, server_name, parameters)
            poller.result()
            return
        poller = self._db_client.servers.begin_delete(self._resource_group, server_name)
        poller.result()

    def delete_database(self, namespace, server_name, database_name, admin_credentials=None):
        server_name = _calc_name(namespace, server_name)
        if _backend_config("database_delete_fake", default=False):
            # Do nothing
            return
        poller = self._db_client.databases.begin_delete(self._resource_group, server_name, database_name)
        poller.result()
    
    def create_or_update_user(self, namespace, server_name, database_name, username, password, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials)
        newly_created = pgclient.create_or_update_user(username, password, database_name)
        return newly_created, {
            "username": username,
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
    default_class = config_get("backends.azurepostgresflexible.default_class")
    if size_class and default_class:
        classes = config_get("backends.azurepostgresflexible.classes", default=[])
        if not size_class in classes:
            warnings.append(f"selected class {size_class} is not allowed. Falling back to default {default_class}")
            size_class = default_class
        selected_class = classes[size_class]
        return Sku(name=selected_class["name"], tier=selected_class["tier"]), warnings

    cpu = int(size_spec.get("cpu", 2))
    if cpu > 64:
        warnings.append(f"Selected numbers of cpus ({cpu}) is more than is allowed with Azure. Setting to maximum allowed 64")
    size = 64
    for step in [2, 4, 8, 16, 32, 48, 64]:
        if cpu <= step:
            size = step
            break
    return Sku(name=f"Standard_D{size}ds_v4", tier="GeneralPurpose"), warnings


def _map_version(version: str):
    if not version:
        return ServerVersion.THIRTEEN
    elif version.startswith("11"):
        return ServerVersion.ELEVEN
    elif version.startswith("12"):
        return ServerVersion.TWELVE
    elif version.startswith("13"):
        return ServerVersion.THIRTEEN
    # versions 14-16 are not directly exposed in the API but as strings are still accepted
    elif version.startswith("14"):
        return "14"
    elif version.startswith("15"):
        return "15"
    elif version.startswith("16"):
        return "16"
    else:
        return ServerVersion.THIRTEEN


def _parse_maintenance_window(spec):
    maintenance_window = field_from_spec(spec, "maintenance.window", default=None)
    if maintenance_window:
        weekday = maintenance_window["weekday"]
        try:
            weekday = int(weekday)
        except:
            try:
                weekday = datetime.strptime(weekday, "%a").weekday()
            except:
                raise kopf.PermanentError("Could not parse maintenance.window.weekday")
        starttime = maintenance_window["starttime"]
        try:
            starttime = datetime.strptime(starttime, "%H:%M")
        except:
            raise kopf.PermanentError("Could not parse maintenance.window.starttime")
        
        maintenance_window = MaintenanceWindow(custom_window="Enabled", day_of_week=weekday, start_hour=starttime.hour, start_minute=starttime.minute)
    else:
        maintenance_window = MaintenanceWindow(custom_window="Disabled", day_of_week=0, start_hour=0, start_minute=0)
    return maintenance_window
