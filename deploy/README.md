# Deploying Notes App to k3s + Cloudflare

The app runs as a single replica with a SQLite database on a PersistentVolume,
behind a password, exposed at **https://note.danielteshome.dev** via Traefik
(`traefik-external`) and Cloudflare — the same pattern as `api.danielteshome.dev`.

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

Verify in-cluster before touching DNS:

```bash
kubectl -n noteapp get pods
kubectl -n noteapp port-forward svc/noteapp 8080:80   # then open http://localhost:8080
```

## 4. DNS (the one manual Cloudflare step)

`note.danielteshome.dev` has no record yet (there is no wildcard). In the
Cloudflare dashboard for `danielteshome.dev`, add a record for `note` that
matches how `api` is configured (same origin / tunnel, **Proxied / orange
cloud**). Once it resolves, `https://note.danielteshome.dev` will serve the app
behind the password.

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
