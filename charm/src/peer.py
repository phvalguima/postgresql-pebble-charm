"""

This file implements all the replication-related details, including the
an overriden relation object that models the peer relation.

PEER RELATION DYNAMICS
The peer relation is responsible to support the primary DB election.
That means this relation is used to publish which unit holds the primary DB
with a timestamp. The timestamp is used in case 2x units post as leaders.

It also should be used for replication-related tasks, such as presenting the
replication password to be used. Each replica must use that password +
peer_username to

"""
from ops.framework import StoredState

from configfiles import genRandomPassword
from postgresql_relation import PostgresqlRelation


def peer_username():
    # Leading underscore for 'system' accounts, to avoid an unlikely
    # conflict with a client service named 'repl'.
    return "_juju_repl"


class PostgresqlPeerRelation(PostgresqlRelation):

    stored = StoredState()

    def __init__(self, charm, relation_name):
        super().__init__(charm, relation_name, self)
        self._unit = charm.unit
        self._charm = charm
        self._relation_name = relation_name
        self._relation = self.framework.model.get_relation(self._relation_name)
        self.stored.set_default(replication_pwd=genRandomPassword(24))

    @property
    def unit(self):
        return self._unit

    @property
    def app(self):
        return self._charm.app

    @property
    def replication_pwd(self):
        if self.is_primary():
            return self.stored.replication_pwd
        return self.primary_repl_pwd()

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

    def primary_repl_pwd(self):
        """Returns the primary ingress-address, or None if not available."""
        if not self.relation or not self.get_primary_unit():
            return self.stored.replication_pwd
        return self.relation.data[self.get_primary_unit()]["replication_pwd"]

    def set_replication_pwd(self, repl_pwd=None):
        if not self.is_primary():
            return None
        if repl_pwd:
            self.relation.data[self.unit]["replication_pwd"] = \
                repl_pwd
            return
        self.relation.data[self.unit]["replication_pwd"] = \
            self.stored.replication_pwd

    def set_as_primary(self):
        """This method selects this unit to be the primary of the databases
        and announces it to its peers.
        """
        import time
        self.relation.data[self.unit]["primary"] = str(time.time())

    def get_primary_unit(self):
        """Returns the unit holding the primary DB"""
        latest_ts = float(self.relation.data[self.unit].get("primary", -1.0))
        primary = self.unit if latest_ts > 0.0 else None
        for u in self.relation.units:
            if latest_ts < float(self.relation.data[u].get("primary", -2.0)):
                # More recent timestamp, update:
                latest_ts = float(self.relation.data[u]["primary"])
                primary = u
            elif latest_ts == float(self.relation.data[u].get("primary", -2.0)) and \
                    u.name.split("/")[1] > primary.name.split("/")[1]:
                # Corner case: both units registered as primary exactly at
                # the same time. Select the one with the highest unit number
                primary = u
        return primary

    def get_primary_ip(self):
        """Returns the primary ingress-address, or None if not available."""
        primary = self.get_primary_unit()
        return self.relation.data[primary]["ingress-address"] if primary else None

    def is_primary(self):
        """Returns True if this unit holds the latest"""
        if not self.relation:
            return False
        primary = self.get_primary_unit()
        if primary and primary == self.unit:
            return True
        # This unit is not primary, for the sake of clean relation data,
        # remove the "primary" key if present:
        if "primary" in self.relation.data[self.unit]:
            del self.relation.data[self.unit]["primary"]
        return False

    def disable_primary_management(self):
        """This method disables the primary management."""
        if "primary" in self.relation.data[self.unit]:
            del self.relation.data[self.unit]["primary"]

    def peer_changed(self, event):
        """Run the peer changed hook.
        """
        # Nothing to do right now
        pass
