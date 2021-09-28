# Postgresql K8s with Pebble

## Description

Running Postgresql with Pebble on top of k8s.

Pebble allows to manage postgresql and, whenever a config change happens, the service can be restarted without losing the entire pod. In this case, the IP of the pod acting as the postgresql leader will announce its own pod IP instead of a service pointing only to it.

In the documentation and the code, "primary database" or only "primary" refers to the unit running the database in Read/Write mode and holding the latest version of the data.

## Usage

To deploy it, run: .........

### Test it locally with microk8s

## Relations

TODO: Provide any relations which are provided or required by your charm


## Replication

There are several scenarios where data can be replicated across units, from volume to node replication.

This charm implements two models of node replication, [as described here](https://www.postgresql.org/docs/12/high-availability.html). These methods are "streaming" and "synchronous".

In a multinode deployment, it is possible to select one of the two replications above, but not disable replication entirely.

To select which option is the most appropriate, toggle ```synchronous_replication```.

### Streaming Replication

Streaming replication is assynchronous by default. Its nodes will always lag behind in replicating primary's WAL for a adjustable bit.

The advantage of streaming replication is the lower latency of not waiting transactions to be ack'ed directly. However, there is always a risk of data loss in case where the primary unit is lost.

### Synchronous Replication

Synchronous replication allows the charm to also manage the ```synchronous_standby_names``` counting with the secondary standby units available.

WARNING: it is considered EXPERIMENTAL to leave ```synchronous_replication=True``` and ```failover=False```. This case triggers the "charm switchover" and "charm management" logic path.

### Synchronous Replication with Failover

If both ```synchronous_replication=True``` and ```failover=True```, then the charm will deploy [pg_auto_failover extension](https://github.com/citusdata/pg_auto_failover). This extension allows to manage database failover by keeping track of WAL replication across primary and secondaries and updates synchronous_standby_names. In the case a secondary falls too behind, it removes the secondary database from synchronous_standby_names.

### What if I do not want replication?

Although out of scope of the present documentation, an user can select to not have replication at all. In this case, it is enough to deploy just one unit of this database.


# Implementation details

## Replication

If set, then the following configurations will be enabled:
1) Every secondary unit gets the a ```standby.signal``` file set in $PGDATA
2) Every secondary unit gets ```primary_conninfo``` configured in juju_recovery.conf and pointing to the primary unit
3) For every secondary, a replica is created with ```pg_basebackup```
4) The ```pg_ctl promote``` command is used to promote the primary unit

## OCI Images

TODO: Include a link to the default image your charm uses

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines 
on enhancements to this charm following best practice guidelines, and
`CONTRIBUTING.md` for developer guidance.

# How does it work?

This charm needs to convert the way configuration is passed in k8s to a traditional application such as postgresql.

The implementation of this application tries to be as close as possible to the [upstream postgresql image](https://github.com/docker-library/postgres). It differs by using Ubuntu as base image and Ubuntu's own postgresql package.

## How this charm differs from postgresql-k8s

Postgresql with pod-spec tracks leader and standby units via service in k8s. Pebble allows to keep the pod (hence its pod IP) even if we restart pod's internal services. The first difference here is the use of the Pod IP directly instead of k8s services.

The second difference is that between-peer information is exchanged on the peer relation. The charm leader sets the information for all the peers.

## Password management

TODO: REVIEW THIS SECTION

The root password can be defined via config options ``` override-root-password: PASSWD ```. That value is converted to a k8s secret and the secret path is passed as environment variable ```ROOT_DB_PASSWORD```. The docker-entrypoint script picks it up and load the file referenced in ROOT_DB_PASSWORD_FILE.

# Building from source

## Build the OCI image

## Build the charm

# TODOs: What this application needs?

Tasks:
* Deprecate kube_api.py once [pebble#37](https://github.com/canonical/pebble/issues/37) is available

Features:
* Clustering: how to deploy postgresql in active/backup and ensure the data is correctly replicated
* Scalability: how to distribute load across the units
* Failover: how to move the postgresql leadership to another unit if needed
* Alerting: how to make sure this service is working?
* Telemetry: get performance-related data to Prometheus
* Backup/Restore: generate full database and PITR 