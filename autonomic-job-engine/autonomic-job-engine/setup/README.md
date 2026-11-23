# Local Kubernetes: MicroK8s in WSL with an open registry

One command gets you a local cluster for the `kubernetes` backend, plus a Docker registry
that anyone can push to.

```powershell
# Windows (PowerShell), from the repository root
.\setup\setup.ps1                  # default distro: Ubuntu
.\setup\setup.ps1 -BuildWorker     # also build and push the worker image
```

```bash
# inside WSL / any Ubuntu
bash setup/microk8s.sh [--build-worker]
```

## What it does

The script is idempotent: it only does what is missing, so you can run it again at any time
to check the cluster.

1. **systemd.** Snap needs systemd. On WSL the script enables it in `/etc/wsl.conf`. WSL
   must then restart, which stops every distro, Docker in WSL included. `setup.ps1` asks
   first, or does it directly with `-RestartWsl`.
2. **MicroK8s.** Installs snapd and MicroK8s (`--channel`, default `1.31/stable`) if they
   are missing, then waits until the cluster is ready.
3. **Addons.** Enables `dns`, `hostpath-storage` and `registry` (default size `20Gi`).
4. **Engine access.** Applies `deploy/k8s/rbac.yaml` (namespace `aje-jobs`) and writes
   `~/.kube/microk8s.config` for the engine.
5. **Summary.** Prints the registry URL and the `AJE_*` settings to use.

## The registry

| from | URL |
|---|---|
| WSL and Windows | `localhost:32000` |
| other machines on the network | `<WSL IP>:32000`. The script prints the IP. |

No login is needed: anyone who can reach the port can push and pull. Docker trusts
`localhost` registries over plain HTTP. Machines that use the IP address must list it under
`insecure-registries` in their Docker daemon configuration.

```bash
docker build -f Dockerfile.worker -t localhost:32000/aje-worker:latest .
docker push localhost:32000/aje-worker:latest
curl http://localhost:32000/v2/_catalog
```

Pods reference images as `localhost:32000/<name>:<tag>`.

> The registry is open on purpose, for local development. Do not expose port 32000 outside
> a trusted network.
