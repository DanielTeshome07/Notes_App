# Deploying Notes App to k3s + Cloudflare

The app runs as a single replica with a SQLite database on a PersistentVolume,
behind a password, exposed at **https://note.danielteshome.dev**.

**How public routing works on this cluster:** the host-level `cloudflared`
tunnel (`/etc/cloudflared/config.yml`) maps each `*.danielteshome.dev` hostname
straight to a Service **NodePort** on `localhost` — it does *not* use a Traefik
Ingress for public traffic (the `traefik-external` ingress class doesn't exist
here). So `noteapp`'s Service is a NodePort (pinned to `30568`), and going live
means adding one entry to that tunnel config plus a DNS record.

## 1. Build & push the image

```bash
# Docker Hub (default in noteapp.yaml). Use an immutable tag, not just :latest.
TAG=$(git rev-parse --short HEAD)
docker build -t danieltesh/noteapp:$TAG -t danieltesh/noteapp:latest .
docker push danieltesh/noteapp:$TAG
docker push danieltesh/noteapp:latest
```

> Prefer GitLab? Build/push to
> `registry.gitlab.com/danielteshome07/notes-app:$TAG`, set that image in
> `noteapp.yaml`, and add `imagePullSecrets: [{name: gitlab-registry}]` to the
> pod spec (copy the `gitlab-registry` secret into the `noteapp` namespace).

## 2. Create the namespace + secret

```bash
kubectl apply -f deploy/noteapp.yaml      # creates the namespace (among other things)

kubectl -n noteapp create secret generic noteapp-secret \
  --from-literal=app-password='CHOOSE-A-STRONG-PASSWORD' \
  --from-literal=flask-secret-key="$(openssl rand -hex 32)"
```

(The Deployment won't start cleanly until the secret exists, since it reads both
keys. If you applied before creating the secret, just create it and the pod will
come up on its next retry — or `kubectl -n noteapp rollout restart deploy/noteapp`.)

## 3. Apply / update

```bash
# If you used an immutable tag, point the deployment at it:
kubectl -n noteapp set image deploy/noteapp noteapp=danieltesh/noteapp:$TAG
kubectl -n noteapp rollout status deploy/noteapp
```

Verify the actual public path (the NodePort the tunnel will hit) before going live:

```bash
kubectl -n noteapp get pods
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:30568/healthz   # expect 200
```

## 4. Expose it (host cloudflared tunnel + DNS)

These steps touch the **live tunnel that serves all your danielteshome.dev
sites**, so they need root and care. Add this block to
`/etc/cloudflared/config.yml`, immediately **before** the final
`- service: http_status:404` catch-all:

```yaml
  - hostname: note.danielteshome.dev
    service: http://localhost:30568
    originRequest:
      httpHostHeader: note.danielteshome.dev
      noTLSVerify: true
```

Then create the DNS record and reload the tunnel:

```bash
# Adds the proxied CNAME for note. -> the tunnel automatically.
# Run WITHOUT sudo: route dns needs the account cert (~/.cloudflared/cert.pem),
# and sudo would look in /root/.cloudflared instead. (Alternatively, under sudo:
# sudo cloudflared --origincert /home/daniel/.cloudflared/cert.pem tunnel route dns ...)
cloudflared tunnel route dns d2d9aa1c-74d2-4945-9973-72c35104561e note.danielteshome.dev

sudo systemctl restart cloudflared   # systemctl needs root
```

Verify:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://note.danielteshome.dev/healthz   # expect 200
```

Then open **https://note.danielteshome.dev** — you'll get the password page.

## CI (GitHub Actions) + manual deploy

CI is split from CD because the k3s API is **not** exposed publicly, so a
GitHub-hosted runner can't reach the cluster:

- **CI builds + pushes the image** — [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml)
  runs on a GitHub-hosted runner (`ubuntu-latest`) on every push to `main` (and
  via **Run workflow**), building and pushing
  `danieltesh/noteapp:<short-sha>` and `:latest` to Docker Hub. This is step 1
  above, automated. No self-hosted runner needed.
- **Deploy stays manual on the cluster box** — because only this machine can
  reach the cluster, run step 3 here to roll the new tag out:

  ```bash
  TAG=$(git rev-parse --short origin/main)
  kubectl -n noteapp set image deploy/noteapp noteapp=danieltesh/noteapp:$TAG
  kubectl -n noteapp rollout status deploy/noteapp
  curl -s -o /dev/null -w '%{http_code}\n' https://note.danielteshome.dev/healthz   # expect 200
  ```

Steps 2 (secret) and 4 (tunnel + DNS) are one-time and stay manual.

### One-time CI setup

Add the Docker Hub secrets (GitHub → repo **Settings → Secrets and variables →
Actions**) so the build can push:

- `DOCKERHUB_USERNAME` — your Docker Hub user (`danieltesh`)
- `DOCKERHUB_TOKEN` — a Docker Hub access token (Account Settings → Security)

> **Want CI to deploy too?** Install a self-hosted runner on `pop-os` (it has
> local `docker` + `kubectl`), then add a second job to the workflow that runs
> `kubectl set image` on `runs-on: self-hosted`. That removes the manual deploy
> step but requires keeping a runner registered.

## Operations

```bash
# Logs
kubectl -n noteapp logs deploy/noteapp -f

# Change the password
kubectl -n noteapp delete secret noteapp-secret
kubectl -n noteapp create secret generic noteapp-secret \
  --from-literal=app-password='NEW' \
  --from-literal=flask-secret-key="$(openssl rand -hex 32)"
kubectl -n noteapp rollout restart deploy/noteapp

# Back up the database
kubectl -n noteapp exec deploy/noteapp -- cat /data/notes.db > notes-backup.db
```

## Notes

- **One replica only.** SQLite + FTS5 is single-writer on one ReadWriteOnce
  volume; the Deployment uses `strategy: Recreate` so a rollout never runs two
  pods against the same volume.
- **Data persists** across restarts/redeploys on the `noteapp-data` PVC
  (`local-path`, on the `pop-os` node). The `tags` migration runs at startup, so
  upgrading an existing DB is safe.
- **Encryption:** HTTPS in transit (Cloudflare) + the password gate. The DB is
  stored as plain SQLite on the volume — no at-rest disk encryption.
