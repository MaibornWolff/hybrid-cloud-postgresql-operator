import psycopg2
from psycopg2.extensions import AsIs


class PostgresSQLClient:

    def __init__(self, credentials, dbname=None):
        if not dbname:
            dbname = credentials["dbname"]
        self._con = psycopg2.connect(host=credentials["host"], port=credentials["port"], dbname=dbname, user=credentials["username"], password=credentials["password"], sslmode=credentials["sslmode"])
        self._con.set_session(autocommit=True)       

    def database_exists(self, name):
        cursor = self._con.cursor()
        cursor.execute("SELECT datname FROM pg_database WHERE datname=%s", (name,))
        exists = len(cursor.fetchall()) > 0
        cursor.close()
        return exists

    def create_database(self, name):
        cursor = self._con.cursor()
        cursor.execute("SELECT datname FROM pg_database WHERE datname=%s", (name,))
        if len(cursor.fetchall()) == 0:
            cursor.execute("CREATE DATABASE %s", (AsIs(name),))
        cursor.close()

    def delete_database(self, name):
        cursor = self._con.cursor()
        cursor.execute("DROP DATABASE IF EXISTS %s", (AsIs(name),))
        cursor.close()

    def restrict_database_permissions(self, name):
        cursor = self._con.cursor()
        # Make sure only explicitly allowed users have access to this database
        cursor.execute("REVOKE ALL PRIVILEGES ON DATABASE %s FROM PUBLIC", (AsIs(name),))
        cursor.close()

    def create_or_update_user(self, name, password, database):
        cursor = self._con.cursor()
        cursor.execute("SELECT * FROM pg_catalog.pg_user WHERE usename=%s", (name, ))
        user_missing = len(cursor.fetchall()) == 0
        if user_missing:
            cursor.execute("CREATE ROLE %s WITH LOGIN ENCRYPTED PASSWORD %s", (AsIs(name), password))
        cursor.execute("GRANT ALL PRIVILEGES ON DATABASE %s TO %s", (AsIs(database), AsIs(name)))
        cursor.close()
        return user_missing
    
    def delete_user(self, name):
        cursor = self._con.cursor()
        cursor.execute("DROP ROLE IF EXISTS %s", (AsIs(name),))
        cursor.close()

    def update_password(self, name, password):
        cursor = self._con.cursor()
        cursor.execute("ALTER USER %s WITH ENCRYPTED PASSWORD %s", (AsIs(name), password))
        cursor.close()

    def create_extension(self, name):
        cursor = self._con.cursor()
        cursor.execute("CREATE EXTENSION IF NOT EXISTS %s CASCADE", (AsIs(name), ))
        cursor.close()
