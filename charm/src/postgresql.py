"""

Helper Postgresql methods: connect and read/write data from databases

Based on: https://github.com/stub42/postgresql-charm/blob/7f9eddf32f818e4d035e7bb3757b8a3e82716f67/reactive/postgresql/postgresql.py

""" # noqa

import re
import psycopg2


def role_exists(con, role):
    """True if the database role exists."""
    cur = con.cursor()
    cur.execute("SELECT TRUE FROM pg_roles WHERE rolname=%s", (role,))
    return cur.fetchone() is not None


def pgidentifier(token):
    """Wrap a string for interpolation by psycopg2 as an SQL identifier"""
    return psycopg2.extensions.AsIs(quote_identifier(token))


def ensure_user(con, username, password, superuser=False, replication=False):
    if role_exists(con, username):
        cmd = ["ALTER ROLE"]
    else:
        cmd = ["CREATE ROLE"]
    cmd.append("%s WITH LOGIN")
    cmd.append("SUPERUSER" if superuser else "NOSUPERUSER")
    cmd.append("REPLICATION" if replication else "NOREPLICATION")
    cmd.append("PASSWORD %s")
    cur = con.cursor()
    cur.execute(" ".join(cmd), (pgidentifier(username), password))


def create_replication_user(username, replication_pwd, local_ip="127.0.0.1"):
    con = connect(host=local_ip)
    ensure_user(con, username, replication_pwd, replication=True)
    con.commit()


def connect(user="postgres", database="postgres", host=None, port=5432):
    if not host:
        return None
    return psycopg2.connect(user=user, database=database, host=host, port=port)


def quote_identifier(identifier):
    r'''Quote an identifier, such as a table or role name.
    In SQL, identifiers are quoted using " rather than ' (which is reserved
    for strings).
    >>> print(quote_identifier('hello'))
    "hello"
    Quotes and Unicode are handled if you make use of them in your
    identifiers.
    >>> print(quote_identifier("'"))
    "'"
    >>> print(quote_identifier('"'))
    """"
    >>> print(quote_identifier("\\"))
    "\"
    >>> print(quote_identifier('\\"'))
    "\"""
    >>> print(quote_identifier('\\ aargh \u0441\u043b\u043e\u043d'))
    U&"\\ aargh \0441\043b\043e\043d"
    '''
    try:
        identifier.encode("US-ASCII")
        return '"{}"'.format(identifier.replace('"', '""'))
    except UnicodeEncodeError:
        escaped = []
        for c in identifier:
            if c == "\\":
                escaped.append("\\\\")
            elif c == '"':
                escaped.append('""')
            else:
                c = c.encode("US-ASCII", "backslashreplace").decode("US-ASCII")
                # Note Python only supports 32 bit unicode, so we use
                # the 4 hexdigit PostgreSQL syntax (\1234) rather than
                # the 6 hexdigit format (\+123456).
                if c.startswith("\\u"):
                    c = "\\" + c[2:]
                escaped.append(c)
        return 'U&"%s"' % "".join(escaped)


def addr_to_range(addr):
    """Convert an address to a format suitable for pg_hba.conf.
    IPv4 and IPv6 ranges are passed through unchanged, as are hostnames.
    Individual IPv4 and IPv6 addresses have a hostmask appended.
    """
    if re.search(r"^(?:\d{1,3}\.){3}\d{1,3}$", addr, re.A) is not None:
        addr += "/32"
    elif ":" in addr and "/" not in addr:  # IPv6
        addr += "/128"
    return addr
