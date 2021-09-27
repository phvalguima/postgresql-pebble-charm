"""

This file implements all the replication-related details, including the
an overriden relation object that models the peer relation.

PEER RELATION DYNAMICS
The charm leader is responsible for updating the data related to the master
unit:
1) Charm leader checks if changes happened to the replication
2) Charm leader updates the app-level relation data
3) Peers recover data from app

"""

from .postgresql_relation import PostgresqlRelation


def peer_username():
    # Leading underscore for 'system' accounts, to avoid an unlikely
    # conflict with a client service named 'repl'.
    return "_juju_repl"


class PostgresqlPeerRelation(PostgresqlRelation):

    def __init__(self, charm, relation_name):
        super().__init__(charm, relation_name, self)
        self._unit = charm.unit
        self._charm = charm
        self._relation_name = relation_name
        self._relation = self.framework.model.get_relation(self._relation_name)

    @property
    def unit(self):
        return self._unit

    @property
    def app(self):
        return self._charm.app

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

    def set_as_primary(self):
        """This method selects this unit to be the primary of the databases
        and announces it to its peers.
        """
        import time
        self.relation.data[self.unit]["primary"] = time.time()

    def get_primary_ip(self):
        """Returns the primary ingress-address, or None if not available."""
        latest_ts = self.relation.data[self.unit].get("primary", -1.0)
        primary = self.unit if latest_ts > 0.0 else None
        for u in self.relation.units:
            if latest_ts < self.relation.data[u].get("primary", -2.0):
                # More recent timestamp, update:
                latest_ts = self.relation.data[u]["primary"]
                primary = u
        return self.relation.data[primary]["ingress-address"] if primary else None

    def is_primary(self):
        """Returns True if this unit holds the latest"""
        latest_ts = self.relation.data[self.unit].get("primary", -1.0)
        primary = self.unit if latest_ts > 0.0 else None
        for u in self.relation.units:
            if latest_ts < self.relation.data[u].get("primary", -2.0):
                # More recent timestamp, update:
                latest_ts = self.relation.data[u]["primary"]
                primary = u
        if primary and primary == self.unit:
            return True
        return False

    def disable_primary_management(self):
        """This method disables the primary management."""
        if "primary" in self.relation.data[self.unit]:
            del self.relation.data[self.unit]["primary"]

    def peer_changed(self, event):
        """Run the peer changed hook.

        1) Check if primary flag has been set and clean this unit's if needed
        """
        # 1) Check if primary flag has been set and clean this unit's if needed
        if "primary" in self.relation.data[self.unit]:
            primary = self.relation.data[self.unit]["primary"]
            # There is a primary key set for this unit. Check the neighbours.
            for u in self.relation.units:
                if u != self.unit and "primary" in self.relation.data[u]:
                    # primary is set on another peer. Check if primary is also set
                    # in this unit. If the value is higher in local unit than the
                    # remote, keep the "primary" key. Otherwise, clean it.
                    if self.relation.data[u]["primary"] > primary:
                        # Remove the key
                        del self.relation.data[self.unit]["primary"]
                    elif self.relation.data[u]["primary"] == primary and \
                            self.unit.name.split("/")[1] < u.name.split("/")[1]:
                        # Corner case, check the unit names and take the biggest
                        del self.relation.data[self.unit]["primary"]
