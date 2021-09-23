# Postgresql K8s with Pebble

## Description

Running Postgresql with Pebble on top of k8s.

Why?

Pebble allows to have deeper control of services within a container. That suits postgresql very well. In k8s, if we do a config change, that almost certainly triggers the delete and redeploy of each unit of the Deployment. Even knowing the data is preserved in PVCs and that we [can do it rather safely](https://www.postgresql.org/docs/12/server-shutdown.html), still means the new pods will have to go through the entire cluster recreation.

That is where Pebble gets interesting: how to restart your container app without losing the pod? Then, just reuse the same pod over and over after each config change.

## Usage

To deploy it, run: ......... (DETAILS ON THE charmstore PATH TO DEPLOY IT, WITH EXAMPLES)

### Build the image

INSTRUCTIONS ON HOW TO BUILD IT

### Test it locally with microk8s

## Relations

TODO: Provide any relations which are provided or required by your charm

## OCI Images

TODO: Include a link to the default image your charm uses

## Contributing

Please see the [Juju SDK docs](https://juju.is/docs/sdk) for guidelines 
on enhancements to this charm following best practice guidelines, and
`CONTRIBUTING.md` for developer guidance.

# How does it work?

This charm needs to convert the way configuration is passed in k8s to a traditional application such as postgresql.

The implementation of this application tries to be as close as possible to the [upstream postgresql image](https://github.com/docker-library/postgres). It differs by using Ubuntu as base image and Ubuntu's own postgresql package.

## What this application needs?

* Clustering: how to deploy postgresql in active/backup and ensure the data is correctly replicated
* Scalability: how to distribute load across the units
* Failover: how to move the postgresql leadership to another unit if needed
* Alerting: how to make sure this service is working?
* Telemetry: get performance-related data to Prometheus
* Backup/Restore: generate full database and PITR 

## Configuration

Similar to the way upstream image is created, we use the docker-entrypoint.sh to translate the environment variables 

### Password management

The root password can be defined via config options ``` override-root-password: PASSWD ```. That value is converted to a k8s secret and the secret path is passed as environment variable ```ROOT_DB_PASSWORD```. The docker-entrypoint script picks it up and load the file referenced in ROOT_DB_PASSWORD_FILE.
