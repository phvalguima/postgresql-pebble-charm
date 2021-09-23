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

import itertools
from collections import OrderedDict

from ops.charm import CharmBase
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus

logger = logging.getLogger(__name__)

POSTGRESQL_ETC_PATH = "/etc/postgresql/{}/main"
TEMPLATE_DIR = "templates/"


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
        self.framework.observe(self.on.start, self.on_config_changed)
        self.framework.observe(self.on.postgresql_pebble_ready, self.on_postgresql_pebble_ready)
        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.leader_elected, self.on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self.on_config_changed)
        self.framework.observe(self.on["peer"].relation_joined, self.on_config_changed)
        self.framework.observe(self.on["peer"].relation_departed, self.on_config_changed)
        # Defining stored default
        self.stored.set_default(pgconf="")

    @property
    def pg_version(self):
        """Discover the Postgresql version.
        For now, hardcoded to return v12.
        """
        return "12"

    def __get_configs(self):
        """Builds the dictionary containing all the env vars"""

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
                    "command": "/usr/local/bin/docker-entrypoint.sh postgres",
                    "startup": "enabled",
                    "environment": {"POSTGRES_PASSWORD": "password"}
                }
            },
        }
        # Add intial Pebble config layer using the Pebble API
        container.add_layer("postgresql", pebble_layer, combine=True)
        # For some reason, entrypoint.sh is coming up with non-executable permissions.
        # Running a read-then-write on the same file but changing the permissions
        # to fix it.
        container.push(
            "/usr/local/bin/docker-entrypoint.sh",
            container.pull("/usr/local/bin/docker-entrypoint.sh"),
            make_dirs=True, permissions=0o755,
        )
        container.autostart()
        self.unit.status = ActiveStatus()
        # self._configure_postgresql(event)

    def on_config_changed(self, event):
        """Just an example to show how to deal with changed configuration.
        """
        self._configure_postgresql(event)

    def _configure_postgresql(self, event):
        container = self.unit.get_container(self.app.name)
        if not container.can_connect():
            if event:
                event.defer()
            return
        container.push(
            self.postgresql_conf_path,
            self.update_postgresql_conf(),
            make_dirs=True)
        # container.start()
        svc = container.get_service("postgresql")
        if svc.is_running():
            container.restart("postgresql")
        else:
            container.start("postgresql")

    ###################
    #                 #
    # CONF generation #
    #                 #
    ###################

    def config_yaml(self):
        with open("config.yaml", "r") as f:
            return yaml.safe_load(f)

    def postgresql_conf_path(self):
        etc_path = POSTGRESQL_ETC_PATH.format(self.pg_version)
        return os.path.join(etc_path, "postgresql.conf")

    def hot_standby_conf_path(self):
        etc_path = POSTGRESQL_ETC_PATH.format(self.pg_version)
        return os.path.join(etc_path, "juju_recovery.conf")

    def _parse_config(self, unparsed_config, fatal=True):
        """Borrowed from:
        https://github.com/stub42/postgresql-charm/blob/ \
            7f9eddf32f818e4d035e7bb3757b8a3e82716f67/reactive/postgresql/postgresql.py#L607

        Parse a postgresql.conf style string, returning a dictionary.
        This is a simple key=value format, per section 18.1.2 at
        http://www.postgresql.org/docs/9.4/static/config-setting.html
        """
        scanner = re.compile(
            r"""^\s*
                            (                       # key=value (1)
                            (?:
                                (\w+)              # key (2)
                                (?:\s*=\s*|\s+)    # separator
                            )?
                            (?:
                                ([-.\w]+) |        # simple value (3) or
                                '(                 # quoted value (4)
                                    (?:[^']|''|\\')*
                                )(?<!\\)'(?!')
                            )?
                            \s* ([^\#\s].*?)?     # badly quoted value (5)
                            )?
                            (?:\s*\#.*)?$           # comment
                            """,
            re.X,
        )
        parsed = OrderedDict()
        for lineno, line in zip(itertools.count(1), unparsed_config.splitlines()):
            try:
                m = scanner.search(line)
                if m is None:
                    raise SyntaxError("Invalid line")
                keqv, key, value, q_value, bad_value = m.groups()
                if not keqv:
                    continue
                if key is None:
                    raise SyntaxError("Missing key {!r}".format(keqv))
                if bad_value is not None:
                    raise SyntaxError("Badly quoted value {!r}".format(bad_value))
                assert value is None or q_value is None
                if q_value is not None:
                    value = re.sub(r"''|\\'", "'", q_value)
                if value is not None:
                    parsed[key.lower()] = value
                else:
                    raise SyntaxError("Missing value")
            except SyntaxError as x:
                if fatal:
                    x.lineno = lineno
                    x.text = line
                    raise x
                self.unit.status = BlockedStatus("{} line {}: {}".format(x, lineno, line))
                raise SystemExit(0)
        return parsed

    def postgresql_conf_defaults(self):
        """Return the postgresql.conf defaults, which we parse from config.yaml"""
        # We load defaults from the extra_pg_conf default in config.yaml,
        # which ensures that they never get out of sync.
        raw = self.config_yaml()["options"]["extra_pg_conf"]["default"]
        defaults = self.parse_config(raw)
        """
        TODO: REVIEW!!

        # And recalculate some defaults, which could get out of sync.
        # Settings with mandatory minimums like wal_senders are handled
        # later, in ensure_viable_postgresql_conf().
        ram = int(host.get_total_ram() / (1024 * 1024))  # Working in megabytes.

        # Default shared_buffers to 25% of ram, minimum 16MB, maximum 8GB,
        # per current best practice rules of thumb. Rest is cache.
        shared_buffers = max(min(math.ceil(ram * 0.25), 8192), 16)
        effective_cache_size = max(1, ram - shared_buffers)
        defaults["shared_buffers"] = "{} MB".format(shared_buffers)
        defaults["effective_cache_size"] = "{} MB".format(effective_cache_size)
        """
        # PostgreSQL 10 introduces multiple password encryption methods.
        if self.pg_version == "10":
            # Change this to scram-sha-256 next LTS release, when we can
            # start assuming clients have libpq 10. The setting can of
            # course still be overridden in the config.
            defaults["password_encryption"] = "md5"
        else:
            defaults["password_encryption"] = True
        return defaults

    def postgresql_conf_overrides(self):
        """User postgresql.conf overrides, from service configuration."""
        return self.parse_config(self.config["extra_pg_conf"])

    def assemble_postgresql_conf(self):
        """Assemble postgresql.conf settings and return them as a dictionary."""
        conf = {}
        # Start with charm defaults.
        conf.update(self.postgresql_conf_defaults())
        # User overrides from service config.
        conf.update(self.postgresql_conf_overrides())
        return conf

    def update_postgresql_conf(self):
        """Generate postgresql.conf

        Borrowed from:
        https://github.com/stub42/postgresql-charm/blob/ \
            7f9eddf32f818e4d035e7bb3757b8a3e82716f67/ \
            reactive/postgresql/service.py#L820
        """
        settings = self.assemble_postgresql_conf()
        path = self.postgresql_conf_path()

        with container.pull(path) as f:
            pg_conf = f.read()

        start_mark = "### BEGIN JUJU SETTINGS ###"
        end_mark = "### END JUJU SETTINGS ###"

        # Strip the existing settings section, including the markers.
        pg_conf = re.sub(
            r"^\s*{}.*^\s*{}\s*$".format(re.escape(start_mark), re.escape(end_mark)),
            "",
            pg_conf,
            flags=re.I | re.M | re.DOTALL,
        )

        for k in settings:
            # Comment out conflicting options. We could just allow later
            # options to override earlier ones, but this is less surprising.
            pg_conf = re.sub(
                r"^\s*({}[\s=].*)$".format(re.escape(k)),
                r"# juju # \1",
                pg_conf,
                flags=re.M | re.I,
            )

        # Store the updated charm options. This is compared with the
        # live config to detect if a restart is required.
        self.stored.pgconf = settings

        # Generate the charm config section, adding it to the end of the
        # config file.
        simple_re = re.compile(r"^[-.\w]+$")
        override_section = [start_mark]
        for k, v in settings.items():
            v = str(v)
            assert "\n" not in v, "Invalid config value {!r}".format(v)
            if simple_re.search(v) is None:
                v = "'{}'".format(v.replace("'", "''"))
            override_section.append("{} = {}".format(k, v))
        if self.pg_version == "12":
            override_section.append("include_if_exists '{}'".format(self.hot_standby_conf_path()))
        override_section.append(end_mark)
        pg_conf += "\n" + "\n".join(override_section)

        return pg_conf


if __name__ == "__main__":
    main(PostgresqlCharm)
