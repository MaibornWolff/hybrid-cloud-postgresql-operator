import kopf
from .routing import postgres_backend
from ..config import config_get
from ..util.password import generate_password
from ..util.reconcile_helpers import ignore_control_label_change, process_action_label, determine_resource_password, shorten
from ..util import k8s
from ..util import env
from ..util.constants import BACKOFF


def _tmp_secret(namespace, name):
    return shorten(f"pg-{namespace}-{name}-tmp")


if config_get("handler_on_resume", default=False):
    @kopf.on.resume(*k8s.PostgreSQLServer.kopf_on(), backoff=BACKOFF)
    def postgresql_server_resume(body, spec, status, meta, labels, name, namespace, diff, logger, **kwargs):
        postgresql_server_handler(body, spec, status, meta, labels, name, namespace, diff, logger, **kwargs)


@kopf.on.create(*k8s.PostgreSQLServer.kopf_on(), backoff=BACKOFF)
@kopf.on.update(*k8s.PostgreSQLServer.kopf_on(), backoff=BACKOFF)
def postgresql_server_handler(body, spec, status, meta, labels, name, namespace, diff, logger, **kwargs):
    if ignore_control_label_change(diff):
        logger.debug("Only control labels removed. Nothing to do.")
        return

    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = spec.get("backend", config_get("backend", fail_if_missing=True))
    backend = postgres_backend(backend_name, logger)

    valid, reason = backend.server_spec_valid(namespace, name, spec)
    if not valid:
        _status_server(name, namespace, status, "failed", f"Validation failed: {reason}")
        raise kopf.PermanentError("Spec is invalid, check status for details")

    tmp_secret_name = _tmp_secret(namespace, name)
    # generate and store credentials
    credentials_secret = k8s.get_secret(namespace, spec["credentialsSecret"])
    password = determine_resource_password(credentials_secret, tmp_secret_name)

    def action_reset_password():
        nonlocal credentials_secret
        nonlocal password
        if credentials_secret:
            # Generate a new password
            password = generate_password(int(config_get("security.password_length", default=16)))
            k8s.delete_secret(namespace, spec["credentialsSecret"])
            k8s.create_or_update_secret(env.OPERATOR_NAMESPACE, tmp_secret_name, {"password": password})
            credentials_secret = None
        return "Admin password reset"
    process_action_label(labels, {
        "reset-password": action_reset_password,
    }, body, k8s.PostgreSQLServer)

    logger.info("Generated password. Creating/updating server")
    _status_server(name, namespace, status, "working", backend=backend_name)
    # create server
    connection_data, warnings = backend.create_or_update_server(namespace, name, spec, password, admin_password_changed=not credentials_secret)
    for warning in warnings:
        kopf.warn(body, reason="AzureWarning", message=warning)
    logger.info("Created/updated server. Creating credentials secret")

    # store credentials in final secret
    if not credentials_secret:
        k8s.create_or_update_secret(namespace, spec["credentialsSecret"], connection_data)
    k8s.delete_secret(env.OPERATOR_NAMESPACE, tmp_secret_name)
    # mark success
    _status_server(name, namespace, status, "finished", "Database server created", backend=backend_name)


@kopf.on.delete(*k8s.PostgreSQLServer.kopf_on(), backoff=BACKOFF)
def postgresql_server_delete(spec, status, name, namespace, logger, **kwargs):
    if status and "backend" in status:
        backend_name = status["backend"]
    else:
        backend_name = spec.get("backend", config_get("backend", fail_if_missing=True))
    backend = postgres_backend(backend_name, logger)
    if backend.server_exists(namespace, name):
        logger.info("Deleting server")
        backend.delete_server(namespace, name)
    else:
        logger.info("Server does not exist. Not doing anything")
    k8s.delete_secret(namespace, spec["credentialsSecret"])


def _status_server(name, namespace, status_obj, status, reason=None, backend=None):
    if status_obj:
        status_obj = dict(backend=status_obj.get("backend", None))
    else:
        status_obj = dict()
    if backend:
        status_obj["backend"] = backend
    status_obj["deployment"] = {
        "status": status,
        "reason": reason
    }
    k8s.patch_custom_object_status(k8s.PostgreSQLServer, namespace, name, status_obj)
