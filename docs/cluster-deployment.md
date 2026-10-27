# Cluster Deployment

This guide describes the architecture and configuration concepts for deploying WaveflowDB in a multi-node environment. Exact behavior, configuration options, and administrative endpoints depend on the cluster implementation you deploy.

## Overview

A cluster controller can act as a reverse proxy between the WaveflowDB client and backend nodes. The client sends requests to the controller, which routes them to the appropriate nodes.

The cluster architecture may support the following behavior:

- **Upload routing:** Direct upload requests to a selected backend node.
- **Distributed queries:** Send queries to nodes that contain the requested namespace.
- **Result aggregation:** Combine query results from multiple nodes, sort them by relevance score, and remove duplicates.

Verify these behaviors against your deployed implementation before relying on them in production.

## Configuration

The following examples illustrate common configuration patterns. Environment variable names, configuration files, ports, and startup commands must match the cluster version you are deploying.

### Single-node development

```bash
export DEFAULT_NODE_HOST="http://localhost:9000"
export DEFAULT_NODE_API_KEY="replace-with-dev-key"

python cluster_api.py
```

This example configures a default backend node for local development.

### Multi-node configuration

A multi-node deployment may use configuration files to define backend nodes and track namespace placement.

```bash
export NODES_FILE="nodes.json"
export NS_INDEX_PATH="namespace_index.json"

python cluster_api.py
```

Confirm that these environment variables and file paths are supported by your cluster implementation.

## Node Configuration

A node configuration may contain a node identifier, endpoint, API key, and optional weight.

Example `nodes.json`:

```json
[
  {
    "id": "node-1",
    "host": "https://node-1.example.com",
    "api_key": "replace-with-secret",
    "weight": 2
  },
  {
    "id": "node-2",
    "host": "https://node-2.example.com",
    "api_key": "replace-with-secret",
    "weight": 1
  }
]
```

This is an illustrative configuration, not a guaranteed schema. Confirm the required fields and the meaning of options such as `weight` in your implementation.

**Security:** Do not commit production API keys or other secrets to version control. Use a secret manager or environment-specific secret configuration.

## Configure the SDK to Use the Controller

When the cluster controller exposes a compatible API, configure the SDK to use the controller endpoint rather than connecting directly to an individual backend node.

```python
from waveflowdb_client import Config, VectorLakeClient

config = Config(
    api_key="YOUR_API_KEY",
    host="http://controller:8080",
)

client = VectorLakeClient(config)
```

Replace the example endpoint and credentials with the values for your deployment. Ensure the controller supports the API routes and authentication expected by the installed SDK version.

## Administrative Endpoints

The following endpoints are examples of cluster-management operations. Their availability and behavior must be verified against the deployed service.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/admin/status` | Retrieve cluster status |
| `GET` | `/admin/nodes` | List registered nodes |
| `GET` | `/admin/namespaces` | Inspect namespace-to-node mappings |
| `POST` | `/admin/nodes/register` | Register a backend node |
| `DELETE` | `/admin/nodes/{node_id}` | Deregister a backend node |

Before using administrative endpoints, confirm their authentication and authorization requirements. Restrict access to trusted operators and networks.

## Production Checklist

Before deploying a cluster:

- [ ] Secure controller and backend endpoints with appropriate authentication and TLS.
- [ ] Store API keys and other secrets securely.
- [ ] Verify upload, query, and namespace routing behavior.
- [ ] Confirm how query results are aggregated, sorted, and deduplicated.
- [ ] Configure health monitoring, structured logging, and failure alerts.
- [ ] Test partial node outages, timeouts, and recovery behavior.
- [ ] Validate namespace placement and consistency across nodes.
- [ ] Document network rules, ports, access requirements, and startup procedures.
- [ ] Test the deployment with the exact SDK and cluster versions intended for production.

For production deployments, use the configuration and operational procedures maintained alongside the specific cluster implementation.