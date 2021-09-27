"""

Implements some common tasks for every type of postgresql relation.



"""

import os
import string

from ops.framework import Object


CHARS_PASSWORD = string.ascii_letters + string.digits


def genRandomPassword(length=48):
    return "".join(CHARS_PASSWORD[c % len(CHARS_PASSWORD)]
                   for c in os.urandom(length))


class PostgresqlRelation(Object):

    def __init__(self, charm, relation_name, peer_rel):
        super().__init__(charm, relation_name)
        self._unit = charm.unit
        self._charm = charm
        self._relation_name = relation_name
        self._relation = self.framework.model.get_relation(self._relation_name)
        # Needed given the leader must update information such as which password
        # has been created for a given client relation
        self.peer_rel = peer_rel

    @property
    def unit(self):
        return self._unit

    @property
    def app(self):
        return self._charm.unit.app

    @property
    def relation(self):
        return self.framework.model.get_relation(self._relation_name)

    @property
    def peer_addresses(self):
        addresses = []
        for u in self.relation.units:
            addresses.append(str(self.relation.data[u]["ingress-address"]))
        return addresses

    @property
    def advertise_addr(self):
        m = self.model
        return str(m.get_binding(self._relation_name).network.ingress_address)

    @property
    def binding_addr(self):
        m = self.model
        return str(m.get_binding(self._relation_name).network.bind_address)

    @property
    def primary_ip(self):
        """Find the owner of the primary IP for the database cluster.

        Updated primary_ip is published in the peer relation and should
        be propagated to every relation when changed.
        """
        for u in self.peer_rel.units:
            if u.is_leader():
                return str(m.get_binding(self.peer_rel).network.bind_address)

    @property
    def db_standby_ip(self):
        """Find the standby IP to be returned. For now, returns the first
        secondary unit IP.
        """

    def create_db_resources(self, event):
        """Create the database resources needed for the relation."""

        # Default password, user, roles and database for the relation
        db_user = "user_" + event.app.replace("-", "_")
        db_name = event.app.replace("-", "_")
        db_pwd = genRandomPassword(12)




        master = self.relation.data[self.app]

        hookenv.log("Ensuring database {!r} and user {!r} exist for {}" "".format(master["database"], master["user"], rel))

        # First create the database, if it isn't already.
        ensure_database(master["database"])

        # Next, connect to the database to create the rest in a transaction.
        con = postgresql.connect(database=master["database"])

        superuser, replication = _credential_types(rel)
        postgresql.ensure_user(
            con,
            master["user"],
            master["password"],
            superuser=superuser,
            replication=replication,
        )
        if not superuser:
            postgresql.ensure_user(con, master["schema_user"], master["schema_password"])

        # Grant specified privileges on the database to the user. This comes
        # from the PostgreSQL service configuration, as allowing the
        # relation to specify how much access it gets is insecure.
        config = hookenv.config()
        privs = set(filter(None, config["relation_database_privileges"].split(",")))
        postgresql.grant_database_privileges(con, master["user"], master["database"], privs)
        if not superuser:
            postgresql.grant_database_privileges(con, master["schema_user"], master["database"], privs)

        # Reset the roles granted to the user as requested.
        if "roles" in master:
            roles = filter(None, master.get("roles", "").split(","))
            postgresql.grant_user_roles(con, master["user"], roles)

        # Create requested extensions. We never drop extensions, as there
        # may be dependent objects.
        if "extensions" in master:
            extensions = list(filter(None, master.get("extensions", "").split(",")))
            # Convert to the (extension, schema) tuple expected by
            # postgresql.ensure_extensions
            for i in range(0, len(extensions)):
                m = re.search(r"^\s*([^(\s]+)\s*(?:\((\w+)\))?", extensions[i])
                if m is None:
                    raise RuntimeError("Invalid extension {}".format(extensions[i]))
                extensions[i] = (m.group(1), m.group(2) or "public")
            postgresql.ensure_extensions(con, extensions)

        con.commit()  # Don't throw away our changes.