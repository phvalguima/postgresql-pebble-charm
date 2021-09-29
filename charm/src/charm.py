#!/usr/bin/env python3
# Copyright 2021 pguimaraes
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Postgresql + Pebble Charm.

This charm allows to deploy a postgresql cluster on top of k8s using
pebble.
"""

import re
import os
import logging
import base64
import datetime

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus, BlockedStatus
from ops.pebble import PathError

from peer import PostgresqlPeerRelation, peer_username
from configfiles import (
    update_pgpass,
    generate_pg_hba_conf,
    update_postgresql_conf
)
from kube_api import PsqlK8sAPI
from postgresql import create_replication_user

logger = logging.getLogger(__name__)

POSTGRESQL_CONF_PATH = "/var/lib/postgresql/data"
TEMPLATE_DIR = "templates/"
PGDATA_PATH = "/var/lib/postgresql/data"


def _render(configs, filepath, template_dir="templates/"):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(template_dir))
    template = env.get_template(filepath)
    config = template.render(**configs)
    return config


class PostgresqlClonePrimaryError(Exception):
    def __init__(self, msg):
        super().__init__(msg)


class PostgresqlCharm(CharmBase):
    """Charm the service."""

    stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.start, self.on_start)
        self.framework.observe(self.on.postgresql_pebble_ready, self.on_postgresql_pebble_ready)
        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.leader_elected, self.on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self.on_config_changed)
        self.framework.observe(self.on["peer"].relation_joined, self.on_peer_relation_joined)
        self.framework.observe(self.on["peer"].relation_departed, self.on_peer_relation_changed)
        # Defining stored default
        self.stored.set_default(pgconf="")
        self.stored.set_default(pg_hba="")
        self.stored.set_default(enable_ssl=False)
        self.stored.set_default(pebble_ready_ran=False)
        self.stored.set_default(primary_ip="")
        self.stored.set_default(db_password="password")
        # Use it as a flag defining that replication user has been created
        self.stored.set_default(repl_user="")
        # Relation setup
        self.peer_rel = PostgresqlPeerRelation(self, "peer")

    @property
    def pg_version(self):
        """Discover the Postgresql version.
        For now, hardcoded to return v12.
        """
        return "12"

    def __get_configs(self):
        """Builds the dictionary containing all the env vars"""

    def on_start(self, event):
        """Module psycopg2 needs to be installed via apt. Install it via apt instead.
        """
        import subprocess
        subprocess.check_call(["apt", "update"], stdout=subprocess.DEVNULL)
        subprocess.check_call(
            ["apt", "install", "-y", "python3-psycopg2"],
            stdout=subprocess.DEVNULL)

    def _stop_app(self):
        """Stop procedure employed by Pebble is too tough for postgresql. This
        method implements a stop routine that is more aligned with postgresql.
        """
        container = self.unit.get_container(self.app.name)
        # Basic check: is postgresql still running? If yes, then we can run
        # the pg_ctl to disable it. If the check within the try/catch below
        # fails for any reason, we can consider that postmaster.pid is not
        # available and return.
        try:
            content = ""
            with container.pull(self.postmaster_pid_path()) as f:
                content = f.read()
            if len(content) == 0:
                return
        except: # noqa
            return
        cmd = [
            "sudo",
            "-u",
            "postgres",
            "/usr/lib/postgresql/12/bin/pg_ctl",
            "stop",
            "-D",
            PGDATA_PATH
        ]
        logger.info("Stopping with {}".format(" ".join(cmd)))
        # Run the command on the other container
        kubeconfig = base64.b64decode(
            self.config["kubeconfig"].encode("utf-8"))
        PsqlK8sAPI(
            container, kubeconfig, self.model
        ).exec_command(cmd)

    def on_peer_relation_joined(self, event):
        """We need to select the primary unit and set the replication password.
        """
        # Needed to add the pod-ip available in the relation
        self.peer_rel.peer_joined(event)
        # Create the replication user if primary:
        if self.peer_rel.is_primary() and \
           len(self.stored.repl_user) == 0:
            self.stored.repl_user = peer_username()
            create_replication_user(
                self.stored.repl_user,
                self.peer_rel.primary_repl_pwd()
            )
        if not self.unit.is_leader():
            return
        # Leader unit, set it as primary
        self.peer_rel.set_as_primary()
        self.peer_rel.set_replication_pwd()

    def on_peer_relation_changed(self, event):
        """The actual setup of the secondary database happens now.
        This gives the primary some time to set it up before.
        """
        self.on_config_changed(event)

    def _read_container_file(self, path):
        container = self.unit.get_container(self.app.name)
        content = None
        try:
            with container.pull(path) as f:
                content = f.read()
        except PathError as e:
            if e.kind == "not-found":
                # Ignored, probably because postgresql has not been initialized yet
                # This should only happen when this method is called before
                # pebble-ready hook, which is unlikely
                pass
        return content or ""

    def on_postgresql_pebble_ready(self, event):
        """Define the workload using Pebble API.
        Start is postponed after postgresql is configured.
        """
        # Get a reference the container attribute on the PebbleReadyEvent
        container = event.workload
        # Define an initial Pebble layer configuration
        pebble_layer = {
            "summary": "postgresql layer",
            "description": "pebble config layer for httpbin",
            "services": {
                "postgresql": {
                    "override": "replace",
                    "summary": "postgresql unit",
                    "command": "/usr/local/bin/docker-entrypoint.sh postgres", # noqa
                    "startup": "enabled",
                    "environment": {"POSTGRES_PASSWORD": self.stored.db_password}
                }
            },
        }
        # Add intial Pebble config layer using the Pebble API
        container.add_layer("postgresql", pebble_layer, combine=True)
        container.autostart()
        self.unit.status = ActiveStatus()
        self.stored.pebble_ready_ran = True

    def on_config_changed(self, event):
        """Just an example to show how to deal with changed configuration.

        1) Validation checks: pebble must be already executed and container is reachable
        2) Generate the configuration files
        3) Restart strategy
        """
        self.model.unit.status = MaintenanceStatus("Starting on_config_changed...")

        # 1) Validation Checks
        if not self.stored.pebble_ready_ran:
            # There is no point going through this logic without having the container
            # in place. We need the config files to kickstart it.
            event.defer()
            return
        # Check container is reachable
        container = self.unit.get_container(self.app.name)
        if not container.can_connect():
            if event:
                event.defer()
            return

        # Seems that pebble-ready hook runs at some point after the
        # initial install and config-changed hooks. It means this method
        # and the search for service running will be run before the actual
        # service has been even configured on pebble.
        # In this case, it causes an exception.
        svc = None
        try:
            svc = container.get_service("postgresql")
        except: # noqa
            # It is possible that pebble-ready hook did not run yet.
            # defer the event and wait for it to be available.
            if event:
                event.defer()
            return
        self.model.unit.status = MaintenanceStatus("Generate configs")
        # 2) Generate the configuration files
        # Following tasks need to be done:
        # 2.1) Check if primary, if not then stop the service
        if self.peer_rel.relation:
            if not self.peer_rel.is_primary() and svc.is_running():
                # Stop service so we can run the replication
                self._stop_app()
                # Run the stop anyway to satisfy pebble internal state
                container.stop("postgresql")
            # 2.2) Save the password on .pgpass
            update_pgpass(
                container, self.peer_rel.primary_repl_pwd(),
                peer_username())
            if not self.peer_rel.is_primary():
                # Clone the repo from upstream
                try:
                    self.clone_primary(container)
                except PostgresqlClonePrimaryError as e:
                    self.model.unit.status = BlockedStatus(str(e))
        # 2.3) Generate the configuration
        self.ssl_config(container)
        self._configure_postgresql_conf(container)
        self._configure_pg_auth_conf(container)
        self._update_pg_ident_conf(container)

        self.model.unit.status = MaintenanceStatus("(re)starting...")

        # 3) Restart strategy:
        # Use ops framework methods to restart services
        self._postgresql_svc_start(restart=svc.is_running())
        self.model.unit.status = ActiveStatus("psql configured")

    def _postgresql_svc_start(self, restart=False):
        """This method calls the container (re)start.

        There are some steps to look after, however:
        1) Due to pebble issue#70, we manually stop the service if restart is asked
        2) Verify that $PGDATA/PG_VERSION exists: that file is used to discover if
           the database has already been created in the docker-entrypoint.sh. Given
           docker-entrypoint.sh has been already ran for the first time at pebble-ready,
           that file must exist or we create it manually.
        3) Run the (re)start
        """
        container = self.unit.get_container(self.app.name)
        # 1) if restart, we need to manually stop it:
        if restart:
            self._stop_app()
        # 2) Check for $PGDATA/PG_VERSION, create it if not available
        container.push(
            self.pg_version_path(),
            self.pg_version,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)
        # Doing step (3)
        if restart:
            logger.debug("Restart call start time: {}".format(datetime.datetime.now()))
            container.restart("postgresql")
            logger.debug("Restart call end time: {}".format(datetime.datetime.now()))
        else:
            container.start("postgresql")

    def _update_pg_ident_conf(self, container):
        """Add the charm's required entry to pg_ident.conf"""
        entries = set([("root", "postgres"), ("postgres", "postgres")])
        path = self.pg_ident_conf_path()
        content = None
        current_pg_ident = self._read_container_file(path)
        for sysuser, pguser in entries:
            if (
                re.search(
                    r"^\s*juju_charm\s+{}\s+{}\s*$".format(sysuser, pguser),
                    current_pg_ident,
                    re.M,
                )
                is None
            ):
                content = current_pg_ident + \
                    "\njuju_charm {} {}".format(sysuser, pguser)
                logger.debug(
                    "{}: Push to path {} content {}".format(
                        datetime.datetime.now(), path, content))
                container.push(
                    path,
                    content,
                    user="postgres", group="postgres",
                    permissions=0o640, make_dirs=True)

    def _configure_pg_auth_conf(self, container):
        """Loads current pg_auth config.
        """
        self.stored.pg_hba = generate_pg_hba_conf(
            self._read_container_file(self.pg_hba_conf_path()),
            self.config, None, self.peer_rel.relation, peer_username(),
            self.stored.enable_ssl
        )
        logger.debug(
            "{}: Push to path {} content {}".format(
                datetime.datetime.now(),
                self.pg_hba_conf_path(), self.stored.pg_hba))
        container.push(
            self.pg_hba_conf_path(),
            self.stored.pg_hba,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)

    def _configure_postgresql_conf(self, container):
        self.stored.pgconf = update_postgresql_conf(
            container, self.config,
            self._read_container_file(self.postgresql_conf_path()),
            self.hot_standby_conf_path(),
            self.pg_version, self.stored.enable_ssl)

        logger.debug(
            "{}: Push to path {} content {}".format(
                datetime.datetime.now(),
                self.postgresql_conf_path(), self.stored.pgconf))
        container.push(
            self.postgresql_conf_path(),
            self.stored.pgconf,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)

    def ssl_config(self, container):
        if len(self.config.get("ssl_cert", "")) == 0 or \
           len(self.config.get("ssl_key", "")) == 0:
            logger.info("ssl_config: crt or key not passed, returning")
            self.stored.enable_ssl = False
            return

        # Breaks the certificate chain into each of its certs.
        def __process_cert(chain):
            begin = "-----BEGIN CERTIFICATE-----"
            list_certs = chain.split(begin)
            # if there is only one element, then return it as server.crt:
            if len(list_certs) == 1:
                return None, chain
            # Returns ca_chain, server.crt
            # Picks the list above and get up to the before last element
            # the last element is the server.crt, attach the begin prefix
            # and return both
            return begin.join(list_certs[0:-1]), begin + list_certs[-1:][0]

        # Root cert is composed of all the certs except last one
        root_crt, server_crt = __process_cert(base64.b64decode(
            self.config["ssl_cert"].encode("utf-8")))
        # Generate ssl_cert
        container.push(
            os.path.join(PGDATA_PATH, "server.crt"),
            server_crt,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)
        # If root crt present, generate it
        if root_crt:
            container.push(
                os.path.join(PGDATA_PATH, "root.crt"),
                root_crt,
                user="postgres", group="postgres",
                permissions=0o640, make_dirs=True)
        # Generate the key
        container.push(
            os.path.join(PGDATA_PATH, "server.key"),
            base64.b64decode(self.config["ssl_key"].encode("utf-8")),
            user="postgres", group="postgres",
            permissions=0o600, make_dirs=True)
        self.stored.enable_ssl = True

    def clone_primary(self, container):
        """Clone the primary database and kickstart the replication process.

        There are some validation checks we must run in order to be sure the database
        will not be accidently compromised. If any of these checks fail, raise
        PostgresqlClonePrimaryError.

        It is expected the postgresql service to be disabled before running this method.
        """
        primary_ip = self.peer_rel.get_primary_ip()

        # Validation checks
        if not primary_ip:
            logger.warning("Validation Check Failed: primary_ip not found")
            raise PostgresqlClonePrimaryError("Primary IP not found")
        if self.peer_rel.is_primary():
            logger.warning("Validation Check Failed: this unit is supposed to be primary")
            raise PostgresqlClonePrimaryError("No Primary unit found")
        if container.get_service("postgresql").is_running():
            logger.warning("Validation Check Failed: service was supposed to be stopped")
            raise PostgresqlClonePrimaryError("Postgresql service still running")

        self.model.unit.status = \
            MaintenanceStatus("Cloning {}".format(primary_ip))

        # Clean up data directory so pg_basebackup can take place
        # Recursive is mandatory to call os.RemoveAll (which empties ot the folder)
        container.remove_path(PGDATA_PATH, recursive=True)
        # Recreate folder with proper rights
        container.make_dir(
            PGDATA_PATH, make_parents=True, permissions=0o750,
            user="postgres", group="postgres")
        # Start cloning, we need to make sure some basic configs are
        # in place before doing the basebackup
        self._configure_pg_auth_conf(container)
        self._update_pg_ident_conf(container)

        # After v10, use --wal-method instead
        if self.pg_version >= "10":
            wal_method = "--wal-method=stream"
        else:
            wal_method = "--xlog-method=stream"
        cmd = [
            "sudo",
            "-H",  # -H needed to locate $HOME/.pgpass
            "-u",
            "postgres",
            "pg_basebackup",
            "-D",
            PGDATA_PATH,
            "-h",
            primary_ip,
            "-p",
            "5432",
            "--checkpoint=fast",
            "--progress",
            wal_method,
            "--no-password",
            "--username={}".format(peer_username()),
        ]
        logger.info("Cloning {} with {}".format(primary_ip, " ".join(cmd)))
        # Run the command on the other container
        kubeconfig = base64.b64decode(
            self.config["kubeconfig"].encode("utf-8"))
        PsqlK8sAPI(
            container, kubeconfig, self.model
        ).exec_command(cmd)

        self.update_recovery_conf(primary_ip, container)
        # Now, save the unit's IP that we are following
        self.stored.primary_ip = primary_ip

    def update_recovery_conf(self, primary_ip, container):
        if primary_ip == self.peer_rel.relation.data[self.unit]["ingress-address"]:
            # This is the primary, abort
            logger.info("update_recovery_conf: should not be run as the primary")
            container.remove_path(self.hot_standby_path())
            container.remove_path(self.standby_signal_path())
            return

        rendered = _render({
            "streaming_replication": True,
            "host": primary_ip,
            "port": 5432,
            "user": peer_username(),
            "password": self.stored.db_password
        }, self.hot_standby_template())
        logger.debug(
            "{}: Push to path {} content {}".format(
                datetime.datetime.now(),
                self.hot_standby_path(), rendered))
        container.push(
            self.hot_standby_path(), rendered,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)

        if self.pg_version >= "12":
            # Also create the standby.signal file
            container.push(
                self.standby_signal_path(), "",
                user="postgres", group="postgres",
                permissions=0o640, make_dirs=True)

    def hot_standby_template(self):
        if self.pg_version >= "12":
            return "hot_standby.conf.j2"
        return "recovery.conf.j2"

    ##################
    #                #
    # Get conf paths #
    #                #
    ##################
    def pg_ident_conf_path(self):
        etc_path = POSTGRESQL_CONF_PATH.format(self.pg_version)
        return os.path.join(etc_path, "pg_ident.conf")

    def postmaster_pid_path(self):
        etc_path = PGDATA_PATH.format(self.pg_version)
        return os.path.join(etc_path, "postmaster.pid")

    def pg_version_path(self):
        etc_path = PGDATA_PATH.format(self.pg_version)
        return os.path.join(etc_path, "PG_VERSION")

    def standby_signal_path(self):
        etc_path = PGDATA_PATH.format(self.pg_version)
        return os.path.join(etc_path, "standby.signal")

    def hot_standby_path(self):
        etc_path = POSTGRESQL_CONF_PATH.format(self.pg_version)
        return os.path.join(etc_path, "juju_recovery.conf")

    def pg_hba_conf_path(self):
        etc_path = POSTGRESQL_CONF_PATH.format(self.pg_version)
        return os.path.join(etc_path, "pg_hba.conf")

    def postgresql_conf_path(self):
        etc_path = POSTGRESQL_CONF_PATH.format(self.pg_version)
        return os.path.join(etc_path, "postgresql.conf")

    def hot_standby_conf_path(self):
        etc_path = POSTGRESQL_CONF_PATH.format(self.pg_version)
        return os.path.join(etc_path, "juju_recovery.conf")


if __name__ == "__main__":
    main(PostgresqlCharm)
