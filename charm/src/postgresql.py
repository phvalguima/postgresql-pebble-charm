"""

Helper Postgresql methods: connect and read/write data from databases

Based on: https://github.com/stub42/postgresql-charm/blob/7f9eddf32f818e4d035e7bb3757b8a3e82716f67/reactive/postgresql/postgresql.py

""" # noqa

import re


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
