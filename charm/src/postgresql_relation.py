"""

Implements some common tasks for every type of postgresql relation.



"""

from ops.framework import Object


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
