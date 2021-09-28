"""

This file contains helpers to process existing config files and render
them according to the configs passed.

Several methods have been created for the original postgresql charm:
https://github.com/stub42/postgresql-charm/

"""

import os
import re
import logging
import yaml
import OrderedDict
import string
import itertools

from .postgresql import quote_identifier, addr_to_range
from .peer import peer_username

logger = logging.getLogger(__name__)


CHARS_PASSWORD = string.ascii_letters + string.digits


def genRandomPassword(length=48):
    return "".join(CHARS_PASSWORD[c % len(CHARS_PASSWORD)]
                   for c in os.urandom(length))

###################
#                 #
# CONF generation #
#                 #
###################


# This is a list of option keys that cannot be injected to postgresql.conf
# via extra_pg_conf.
DO_NOT_TOUCH_KEYS = [
    # SSL is managed separated
    "ssl",
    # Used for sync / assync replication methods
    "synchronous_commit",
    "synchronous_standby_names"
]


def update_pgpass(container, replication_password):
    accounts = ["root", "postgres", "ubuntu"]
    for account in accounts:
        path = os.path.expanduser(
            os.path.join("~{}".format(account), ".pgpass"))
        content = \
            "# Managed by Juju\n*:*:*:{}:{}".format(
                peer_username(), replication_password)
        container.push(
            path, content, user=account, group=account,
            permissions=0o600, make_dirs=True
        )


def config_yaml():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def _parse_config(unparsed_config, fatal=True):
    """Parse a postgresql.conf style string, returning a dictionary.
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
            logger.debug("{} line {}: {}".format(x, lineno, line))
            raise SystemExit(0)
    return parsed


def postgresql_conf_defaults(pg_version="12"):
    """Return the postgresql.conf defaults, which we parse from config.yaml"""
    # We load defaults from the extra_pg_conf default in config.yaml,
    # which ensures that they never get out of sync.
    raw = config_yaml()["options"]["extra_pg_conf"]["default"]
    defaults = _parse_config(raw)
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
    if pg_version == "10":
        # Change this to scram-sha-256 next LTS release, when we can
        # start assuming clients have libpq 10. The setting can of
        # course still be overridden in the config.
        defaults["password_encryption"] = "md5"
    else:
        defaults["password_encryption"] = True
    return defaults


def postgresql_conf_overrides(config):
    """User postgresql.conf overrides, from service configuration.

    config: reference pointer to self.config options of the charm.
    """

    return _parse_config(config["extra_pg_conf"])


def assemble_postgresql_conf(config, pg_version="12"):
    """Assemble postgresql.conf settings and return them as a dictionary.

    config: reference pointer to self.config options of the charm.
    """

    conf = {}
    # Start with charm defaults.
    conf.update(postgresql_conf_defaults(pg_version))
    # User overrides from service config.
    conf.update(postgresql_conf_overrides(config))
    return conf


def update_postgresql_conf(
        container, config, pg_conf, hot_standby_conf_path, pg_version="12", enable_ssl=False):
    """Generate postgresql.conf

    config: reference pointer to self.config options of the charm.
    """
    settings = assemble_postgresql_conf(config, pg_version)

    start_mark = "### BEGIN JUJU SETTINGS ###"
    end_mark = "### END JUJU SETTINGS ###"

    if len(pg_conf) > 0:
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

    # Generate the charm config section, adding it to the end of the
    # config file.
    simple_re = re.compile(r"^[-.\w]+$")
    override_section = [start_mark]
    for k, v in settings.items():
        if k.lower() in DO_NOT_TOUCH_KEYS:
            # keys that should not be defined by the user
            logger.debug(
                "Operator requested to add key {} "
                "to postgresql.conf, which "
                "is not allowed. Ignoring it".format(k))
            continue
        v = str(v)
        assert "\n" not in v, "Invalid config value {!r}".format(v)
        if simple_re.search(v) is None:
            v = "'{}'".format(v.replace("'", "''"))
        override_section.append("{} = {}".format(k, v))
    if pg_version == "12":
        override_section.append("include_if_exists '{}'".format(hot_standby_conf_path))

    # Last thing: check if ssl is enabled:
    override_section.append("ssl = true" if enable_ssl else "ssl = false")

    override_section.append(end_mark)
    pg_conf += "\n" + "\n".join(override_section)

    return pg_conf


def incoming_addresses(relinfo):
    """Return the incoming address range(s) if present in relinfo.
    Address ranges are in CIDR format. eg. 192.168.1.0/24 or 2001::F00F/128.
    We look for information as provided by recent versions of Juju, and
    fall back to private-address if needed.
    Returns an empty list if no address information is present. An
    error is logged if this occurs, as something has gone seriously
    wrong.
    """
    # This helper could return a set, but a list with stable ordering is
    # easier to use without causing flapping.
    if "egress-subnets" in relinfo:
        return [n.strip() for n in relinfo["egress-subnets"].split(",") if n.strip()]
    if "ingress-address" in relinfo:
        return [addr_to_range(relinfo["ingress-address"])]
    if "private-address" in relinfo:
        return [addr_to_range(relinfo["private-address"])]
    return []


def split_extra_pg_auth(raw_extra_pg_auth):
    """Yield the extra_pg_auth stanza line by line.
    Uses the input as a multi-line string if valid, or falls
    back to comma separated for backwards compatibility.
    """
    # Lines in a pg_hba.conf file must be comments, whitespace, or begin
    # with 'local' or 'host'.
    valid_re = re.compile(r"^\s*(host.*|local.*|#.*)?\s*$")

    def valid_line(ln):
        return valid_re.search(ln) is not None

    lines = list(raw_extra_pg_auth.split(","))
    if len(lines) > 1 and all(valid_line(ln) for ln in lines):
        return lines
    else:
        return raw_extra_pg_auth.splitlines()


def generate_pg_hba_conf(pg_hba, config, rels, _peer_rel, enable_ssl):
    """Update the pg_hba.conf file (host based authentication)."""
    rules = []  # The ordered list, as tuples.

    # local      database  user  auth-method  [auth-options]
    # host       database  user  address  auth-method  [auth-options]
    # hostssl    database  user  address  auth-method  [auth-options]
    # hostnossl  database  user  address  auth-method  [auth-options]
    # host       database  user  IP-address  IP-mask  auth-method  [auth-opts]
    # hostssl    database  user  IP-address  IP-mask  auth-method  [auth-opts]
    # hostnossl  database  user  IP-address  IP-mask  auth-method  [auth-opts]
    def add(*record):
        rules.append(tuple(record))

    def host(enable_ssl):
        return "hostssl" if enable_ssl else "host"

    # The charm is running as the root user, and needs to be able to
    # connect as the postgres user to all databases.
    add("local", "all", "postgres", "peer", "map=juju_charm")

    """
    TODO: DO WE NEED NAGIOS??

    # The local unit needs access to its own database. Let every local
    # user connect to their matching PostgreSQL user, if it exists, and
    # nagios with a password.
    add("local", "all", nagios.nagios_username(), "password")
    add("local", "all", "all", "peer")
    """

    # Peers need replication access as the charm replication user.
    if _peer_rel:
        for peer, relinfo in _peer_rel.items():
            for addr in incoming_addresses(relinfo):
                qaddr = quote_identifier(addr)
                # Magic replication database, for replication.
                add(
                    host(enable_ssl),
                    "replication",
                    peer_username(),
                    qaddr,
                    "md5",
                    "# {}".format(relinfo),
                )
                # postgres db, so leader can query replication status.
                add(
                    host(enable_ssl),
                    "postgres",
                    peer_username(),
                    qaddr,
                    "md5",
                    "# {}".format(relinfo),
                )
    """

    TODO: ADD THE METHODS BELOW AS NEW RELATIONS ARE ADDED

    # Clients need access to the relation database as the relation users.
    for rel in rels["db"].values():
        if "user" in rel.local:
            for relinfo in rel.values():
                for addr in incoming_addresses(relinfo):
                    # Quote everything, including the address, to disenchant
                    # magic tokens like 'all'.
                    add(
                        host(enable_ssl),
                        quote_identifier(rel.local["database"]),
                        quote_identifier(rel.local["user"]),
                        quote_identifier(addr),
                        "md5",
                        "# {}".format(relinfo),
                    )
                    add(
                        host(enable_ssl),
                        quote_identifier(rel.local["database"]),
                        quote_identifier(rel.local["schema_user"]),
                        quote_identifier(addr),
                        "md5",
                        "# {}".format(relinfo),
                    )

    # Admin clients need access to all databases as any user, not just the
    # relation user. Most clients will just use the user provided them,
    # but proxies such as pgbouncer need to open connections as the accounts
    # it creates.
    for rel in rels["db-admin"].values():
        if "user" in rel.local:
            for relinfo in rel.values():
                for addr in incoming_addresses(relinfo):
                    add(
                        host(enable_ssl),
                        "all",
                        "all",
                        quote_identifier(addr),
                        "md5",
                        "# {}".format(relinfo),
                    )

    # External replication connections. Somewhat different than before
    # as the relation gets its own user to avoid sharing credentials,
    # and logical replication connections will want to specify the
    # database name.
    for rel in rels["master"].values():
        for relinfo in rel.values():
            for addr in incoming_addresses(relinfo):
                add(
                    host(enable_ssl),
                    "replication",
                    quote_identifier(rel.local["user"]),
                    quote_identifier(addr),
                    "md5",
                    "# {}".format(relinfo),
                )
                if "database" in rel.local:
                    add(
                        host(enable_ssl),
                        quote_identifier(rel.local["database"]),
                        quote_identifier(rel.local["user"]),
                        quote_identifier(addr),
                        "md5",
                        "# {}".format(relinfo),
                    )
    """ # noqa

    # External administrative addresses, if specified by the operator.
    for addr in config["admin_addresses"].split(","):
        if addr:
            add(
                host(enable_ssl),
                "all",
                "all",
                quote_identifier(addr_to_range(addr)),
                "md5",
                "# admin_addresses config",
            )

    # And anything-goes rules, if specified by the operator.
    for line in split_extra_pg_auth(config["extra_pg_auth"]):
        add(line + " # extra_pg_auth config")

    # Deny everything else
    add("local", "all", "all", "reject", "# Refuse by default")
    add("host", "all", "all", "all", "reject", "# Refuse by default")

    # Strip out the existing juju managed section
    start_mark = "### BEGIN JUJU SETTINGS ###"
    end_mark = "### END JUJU SETTINGS ###"
    pg_hba = re.sub(
        r"^\s*{}.*^\s*{}\s*$".format(re.escape(start_mark), re.escape(end_mark)),
        "",
        pg_hba,
        flags=re.I | re.M | re.DOTALL,
    )

    # Comment out any uncommented lines
    pg_hba = re.sub(r"^\s*([^#\s].*)$", r"# juju # \1", pg_hba, flags=re.M)

    # Spit out the updated file
    rules.insert(0, (start_mark,))
    rules.append((end_mark,))
    pg_hba += "\n" + "\n".join(" ".join(rule) for rule in rules)
    return pg_hba
