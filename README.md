# Postgresql K8s with Pebble

## Description

Running Postgresql with Pebble on top of k8s.

Pebble allows to manage postgresql and, whenever a config change happens, the service can be restarted without losing the entire pod. In this case, the IP of the pod acting as the postgresql leader will announce its own pod IP instead of a service pointing only to it.

In the documentation and the code, "primary database" or only "primary" refers to the unit running the database in Read/Write mode and holding the latest version of the data.

## Usage

### Pass kubeconfig as an option

Kubernetes API is used to execute commands on 

### Test it locally with microk8s

Follow microk8s.io documentation to set it up.

Deploy Juju on top of microk8s.

Run the following commands:
```
$ juju add-model postgresql # or your name of choice
$ juju deploy ch:postgresql
```


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

Docker is needed to create the base image.

Run the following commands to prepare the OCI image:

```
$ cd docker/<postgresql-version-of-choice>/
$ docker build . -t <your tag>
```

Then push it to the registry to start using.

## Build the charm

Charmcraft is necessary to prepare the package.

Run the following steps to prepare it:
```
$ cd charm/
$ charmcraft -v pack
```

## Test your charm

Tox and flake8 is available. Run the following tests to validate your changes:
```
$ cd charm/
$ tox -e pep8
```

# TODOs: What this application needs?

Tasks:
* Deprecate kube_api.py once [pebble#37](https://github.com/canonical/pebble/issues/37) is available
* Remove _stop_app charm logic once it is fixed [pebble#70](https://github.com/canonical/pebble/issues/70)
* Add replication and failover documentation alongside the code

Features:
* Clustering: how to deploy postgresql in active/backup and ensure the data is correctly replicated
* Scalability: how to distribute load across the units
* Failover: how to move the postgresql leadership to another unit if needed
* Alerting: how to make sure this service is working?
* Telemetry: get performance-related data to Prometheus
* Backup/Restore: generate full database and PITR 