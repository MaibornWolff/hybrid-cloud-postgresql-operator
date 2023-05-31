import time
import kopf
from .pgclient import PostgresSQLClient
from ..util.aws import aws_client_rds
from ..util.reconcile_helpers import field_from_spec


class AwsBackendBase:
    """
        Common methods used by both AWS backends
    """
    def __init__(self, logger):
        self._rds_client = aws_client_rds()
        self._logger = logger

    def database_exists(self, namespace, server_name, database_name, admin_credentials=None):
        if not admin_credentials:
            return None
        pgclient = self._pgclient(admin_credentials)
        return pgclient.database_exists(database_name)

    def delete_database(self, namespace, server_name, database_name, admin_credentials=None):
        if not admin_credentials:
            self._logger.warn("No admin credentials. Skipping deletion of database")
            return
        pgclient = self._pgclient(admin_credentials)
        pgclient.delete_database(database_name)

    def create_or_update_user(self, namespace, server_name, database_name, username, password, admin_credentials=None):
        pgclient = self._pgclient(admin_credentials, dbname=database_name)
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
        return PostgresSQLClient(admin_credentials, dbname=dbname)


weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def calculate_maintenance_window(spec):
    # ddd:hh24:mi-ddd:hh24:mi
    window = field_from_spec(spec, "maintenance.window")
    weekday = field_from_spec(spec, "maintenance.window.weekday")
    starttime = field_from_spec(spec, "maintenance.window.starttime")
    if not window or not weekday or not starttime:
        return None
    start = f"{weekday}:{starttime}"
    start_day = weekday.lower()
    start_hour, minute = starttime.split(":")
    end_hour = (start_hour + 1) % 24
    end_day = start_day
    if end_hour < start_hour:
        end_day = weekdays[(weekdays.index(start_day)+1) % weekdays.len()]
    return f"start-{end_day}:{end_hour}:{minute}"
