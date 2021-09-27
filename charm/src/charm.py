#!/usr/bin/env python3
# Copyright 2021 pguimaraes
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Postgresql + Pebble Charm.

This charm allows to deploy a postgresql cluster on top of k8s using
pebble.
"""

import os
import re
import yaml
import logging
import base64
import datetime

import itertools
from collections import OrderedDict

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus
from ops.pebble import PathError

from .peer import PostgresqlPeerRelation

logger = logging.getLogger(__name__)

POSTGRESQL_CONF_PATH = "/var/lib/postgresql/data"
TEMPLATE_DIR = "templates/"
PGDATA_PATH = "/var/lib/postgresql/data"


def _render(configs, pgversion="12"):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader("templates/"))
    template = env.get_template(os.path.join(TEMPLATE_DIR, "postgresql.conf.j2"))
    config = template.render(**configs)
    return config


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
        self.framework.observe(self.on["peer"].relation_joined, self.on_config_changed)
        self.framework.observe(self.on["peer"].relation_departed, self.on_config_changed)
        # Defining stored default
        self.stored.set_default(pgconf="")
        self.stored.set_default(pg_hba="")
        self.stored.set_default(enable_ssl=False)
        self.stored.set_default(pebble_ready_ran=False)
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
        pass

    def _read_container_file(self, path):
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
        return content

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
                    "command": "/usr/local/bin/docker-entrypoint.sh postgres -D /var/lib/postgresql/data",
                    "startup": "enabled",
                    "environment": {"POSTGRES_PASSWORD": "password"}
#                },
#                "pg_autoctl": {
#                    "override": "replace",
#                    "summary": "pg auto failover mechanism"
#                    "command": "/usr/lib/postgresql/12/bin/pg_autoctl --pgdata /var/lib/postgresql/monitor --pgport 5000",
#                    "user": "postgres",
#                    "group": "postgres",
#                    "environment": {}
                }
            },
        }
        # Add intial Pebble config layer using the Pebble API
        container.add_layer("postgresql", pebble_layer, combine=True)
        container.autostart()
        self.unit.status = ActiveStatus()
        self.stored.pebble_ready_ran = True
        self._configure_postgresql(event)

    def on_config_changed(self, event):
        """Just an example to show how to deal with changed configuration.

        1) Validation checks: pebble must be already executed and container is reachable
        2) Generate the configuration files
        3) Restart strategy
        """

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

        # 2) Generate the configuration files
        self.ssl_config(container)
        self._configure_postgresql_conf(container, self.stored.enable_ssl)
        self._configure_pg_auth_conf(container, self.stored.enable_ssl)

        # 3) Restart strategy:
        # Use ops framework methods to restart services

        # Seems that pebble-ready hook runs at some point after the
        # initial install and config-changed hooks. It means this method
        # and the search for service running will be run before the actual
        # service has been even configured on pebble.
        # In this case, it causes an exception.
        svc = None
        try:
            svc = container.get_service("postgresql")
        except:
            # It is possible that pebble-ready hook did not run yet.
            # defer the event and wait for it to be available.
            if event:
                event.defer()
            return
        if svc.is_running():
            logger.debug("Restart call start time: {}".format(datetime.datetime.now()))
            container.restart("postgresql")
            logger.debug("Restart call end time: {}".format(datetime.datetime.now()))
        else:
            container.start("postgresql")

    def _configure_pg_auth_conf(self, container):
        """Loads current pg_auth config.
        """
        self.stored.pg_hba = generate_pg_hba_conf(
            self._read_container_file(self.pg_hba_conf_path()),
            self.config, None, None)
        container.push(
            self.pg_hba_conf_path(),
            self.stored.pg_hba,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)

    def _configure_postgresql_conf(self, container):
        self.stored.pgconf = update_postgresql_conf(
            container, self.config,
            self._read_container_file(self.postgresql_conf_path()),
            self.pg_version, self.stored.enable_ssl)
        container.push(
            self.postgresql_conf_path(),
            self.stored.pgconf, 
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)

    def ssl_config(self, container):
        if len(self.config.get("ssl_cert", "")) > 0 or
           len(self.config.get("ssl_key", "")) > 0:
            logger.info("ssl_config: crt or key not passed, returning")
            self.stored.enable_ssl = False
            return

        # Breaks the certificate chain into each of its certs.
        def __process_cert(chain):
            begin = "-----BEGIN CERTIFICATE-----"
            end = "-----END CERTIFICATE-----"
            list_certs = chain.split(begin)
            # if there is only one element, then return it as server.crt:
            if len(list_certs) == 1:
                return None, chain
            # Returns ca_chain, server.crt
            # Picks the list above and get up to the before last element
            # the last element is the server.crt, attach the begin prefix
            # and return both
            return begin.join(list_certs[0:-1]),
                begin + list_certs[-1:][0]

        # Root cert is composed of all the 
        root_crt, server_crt = __process_cert(base64.b64.decode(
            self.config["ssl_cert"].encode("utf-8")))
        # Generate ssl_cert
        container.push(
            os.path.join(PGDATA_PATH, "server.crt"),
            server_crt,
            user="postgres", group="postgres",
            permissions=0o640, make_dirs=True)
        # If root crt present, generate it:
        if root_crt:
            container.push(
                os.path.join(PGDATA_PATH, "root.crt"),
                root_crt,
                user="postgres", group="postgres",
                permissions=0o640, make_dirs=True)
        # Generate the key
        container.push(
            os.path.join(PGDATA_PATH, "server.key"),
            base64.b64.decode(self.config["ssl_key"].encode("utf-8")),
            user="postgres", group="postgres",
            permissions=0o600, make_dirs=True)
        self.stored.enable_ssl = True

    def clone_master(self, container):
        primary_ip = self.peer_rel.get_primary_ip()
        master_relinfo = peer_rel[master]
        # Validation checks
        if not peer_rel.is_primary():
            logger.warning("Validation Check Failed: this unit is supposed to be primary")
            return
        if container.get_service("postgresql").is_running():
            logger.warning("Validation Check Failed: service was supposed to be stopped")
            return

        # Clean up data directory so pg_basebackup can take place
        container.remove_path(PGDATA_PATH, recursive=True)
        # Recreate folder with proper rights
        container.make_dir(
            PGDATA_PATH, make_parents=True, permissions=)

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
            "--username=_juju_repl",
        ]
        logger.info("Cloning {} with {}".format(master, " ".join(cmd)))
        status_set("maintenance", "Cloning {}".format(master))
        try:
            # Switch to a directory the postgres user can access.
            with helpers.switch_cwd("/tmp"):
                subprocess.check_call(cmd, universal_newlines=True)
        except subprocess.CalledProcessError as x:
            hookenv.log("Clone failed with {}".format(x), ERROR)
            # We failed, and the local cluster is broken.
            status_set("blocked", "Failed to clone {}".format(master))
            postgresql.drop_cluster()
            reactive.remove_state("postgresql.cluster.configured")
            reactive.remove_state("postgresql.cluster.created")
            # Terminate. We need this hook to exit, rather than enter a loop.
            raise SystemExit(0)

        update_recovery_conf(follow=master)

        reactive.set_state("postgresql.replication.cloned")
        update_replication_states()    

    ##################
    #                #
    # Get conf paths #
    #                #
    ##################

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
