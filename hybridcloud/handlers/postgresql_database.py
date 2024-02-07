from datetime import datetime, timezone
import kopf
from .routing import postgres_backend
from ..config import config_get
from ..util import env, k8s
from ..util.constants import BACKOFF
from ..util.password import generate_password
from ..util.reconcile_helpers import process_action_label, ignore_control_label_change, determine_resource_password, shorten


def _tmp_secret(namespace, name):
    return shorten(f"pgdb-{namespace}-{name}-tmp")


if config_get("handler_on_resume", default=False):
    @kopf.on.resume(*k8s.PostgreSQLDatabase.kopf_on(), backoff=BACKOFF)
    def postgresql_database_resume(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
        postgresql_database_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs)


@kopf.on.create(*k8s.PostgreSQLDatabase.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.PostgreSQLDatabase.kopf_on(), backoff=BACKOFF)
def postgresql_database_manage(spec, meta, labels, name, namespace, body, status, retry, diff, logger, **kwargs):
    if ignore_control_label_change(diff):
        logger.debug("Only control labels removed. Nothing to do.")
        return

    dbname = name.replace("-", "_")
    username = dbname
    tmp_secret_name = _tmp_secret(namespace, name)
    server_name = spec["serverRef"]["name"]
    server_namespace = namespace
    backend, backend_name, admin_credentials = _wait_for_server(logger, namespace, server_namespace, server_name, retry)

    # Generate or read password
    credentials_secret_name = spec["credentialsSecret"]
    credentials_secret = k8s.get_secret(namespace, credentials_secret_name) 
    password = determine_resource_password(credentials_secret, tmp_secret_name)
    
    logger.info("Generated password. Creating database")
    _status(name, namespace, status, "working", backend=backend_name)
    backend.create_or_update_database(server_namespace, server_name, dbname, spec, admin_credentials=admin_credentials)
    logger.info("Created database. Creating user")

    user_newly_created, credentials = backend.create_or_update_user(server_namespace, server_name, dbname, username, password, admin_credentials=admin_credentials)

    def action_reset_password():
        nonlocal credentials_secret
        nonlocal password
        if credentials_secret:
            # Generate a new password
            password = generate_password(int(config_get("security.password_length", default=16)), bool(config_get("security.special_characters", default=True)))
            k8s.delete_secret(namespace, credentials_secret_name)
            k8s.create_secret(env.OPERATOR_NAMESPACE, tmp_secret_name, {"password": password})
            credentials_secret = None
        backend.update_user_password(server_namespace, server_name, username, password, admin_credentials=admin_credentials)
        return "Password for user reset"
    process_action_label(labels, {
        "reset-password": action_reset_password,
    }, body, k8s.PostgreSQLDatabase)

    if not user_newly_created and not credentials_secret:
        # Secret with credentials was deleted, so need to reset the password as it cannot be extracted from database
        action_reset_password()

    # store credentials in final secret
    credentials["password"] = password
    if not credentials_secret or user_newly_created:
        k8s.create_or_update_secret(namespace, credentials_secret_name, credentials)
    k8s.delete_secret(env.OPERATOR_NAMESPACE, tmp_secret_name)
    # mark success
    _status(name, namespace, status, "finished", "Database created", backend=backend_name)


@kopf.on.delete(*k8s.PostgreSQLDatabase.kopf_on(), backoff=BACKOFF)
def postgresql_database_delete(spec, status, name, namespace, logger, **kwargs):
    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = config_get("backend", fail_if_missing=True)
    backend = postgres_backend(backend_name, logger)

    dbname = name.replace("-", "_")
    server_name = spec["serverRef"]["name"]
    server_namespace = namespace
    server_object = k8s.get_custom_object(k8s.PostgreSQLServer, server_namespace, server_name)
    server_exists = backend.server_exists(server_namespace, server_name)
    admin_secret = k8s.get_secret(namespace, server_object["spec"]["credentialsSecret"]) if server_object else None
    admin_credentials = k8s.decode_secret_data(admin_secret) if admin_secret else None

    if server_exists and backend.database_exists(server_namespace, server_name, dbname, admin_credentials=admin_credentials):
        logger.info("Deleting database")
        backend.delete_database(server_namespace, server_name, dbname, admin_credentials=admin_credentials)
        backend.delete_user(namespace, server_name, dbname, admin_credentials=admin_credentials)
    else:
        logger.info("Database does not exist. Not doing anything")
    k8s.delete_secret(namespace, spec["credentialsSecret"])


def _status(name, namespace, status_obj, status, reason=None, backend=None):
    if status_obj:
        status_obj = dict(backend=status_obj.get("backend", None))
    else:
        status_obj = dict()
    if backend:
        status_obj["backend"] = backend
    status_obj["deployment"] = {
        "status": status,
        "reason": reason,
        "latest-update": datetime.now(tz=timezone.utc).isoformat()
    }
    k8s.patch_custom_object_status(k8s.PostgreSQLDatabase, namespace, name, status_obj)


def _wait_for_server(logger, namespace, server_namespace, server_name, retry):
    server_object = k8s.get_custom_object(k8s.PostgreSQLServer, server_namespace, server_name)
    if not server_object:
        raise kopf.TemporaryError("Waiting for server to be created.", delay=20 if retry < 5 else 30 if retry < 10 else 60)

    backend_name = server_object.get("status", dict()).get("backend", server_object["spec"].get("backend", config_get("backend", fail_if_missing=True)))
    backend = postgres_backend(backend_name, logger)

    server_exists = backend.server_exists(server_namespace, server_name)
    admin_secret = None if not server_object else k8s.get_secret(namespace, server_object["spec"]["credentialsSecret"])
    if not server_object or not server_exists or not admin_secret:
        raise kopf.TemporaryError("Waiting for server to be created.", delay=20 if retry < 5 else 30 if retry < 10 else 60)
    return backend, backend_name, k8s.decode_secret_data(admin_secret)
