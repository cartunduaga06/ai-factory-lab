# Remote Control Tower

The Factory status process remains bound to `127.0.0.1:8765`. This Cloudflare
Tunnel config forwards only to that loopback listener; Cloudflare Access
provides browser authentication before requests reach the origin. No origin port
is opened to the network.

## One-time external setup

In the Cloudflare dashboard, create a named tunnel, create a public hostname
(`tower.<your-domain>`) targeting this origin, and create an Access
self-hosted application for that exact hostname. Require an explicit
operator-owned identity rule (for example, an email allowlist). The tunnel
credential, account, zone, hostname and identity policy are external
prerequisites and cannot be provisioned by this repository. Do not use a quick
tunnel or a public hostname without an Access policy.

Install `cloudflared` using the host's approved package process. Copy
`config.yml.example` to `/etc/cloudflared/config.yml`, fill the tunnel UUID and
hostname, and install the dashboard-issued credential JSON at the configured
path with mode `0600`, owned by the service account. These files are secrets or
environment-specific configuration and must not be committed.

Run the factory status process with explicit loopback arguments:

```sh
python -m factory status --serve --host 127.0.0.1 --port 8765
```

Run cloudflared under the host's service manager using that config. A persistent
named tunnel reconnects after process or host restarts. Keep port 8765 closed at
the host firewall as defense in depth. Do not add any other ingress rule or
origin service.

## Smoke checks

With the local status server running, confirm its socket is loopback-only:

```sh
ss -ltnp | grep ':8765'
```

Expected local address: `127.0.0.1:8765` (or `[::1]:8765` only if deliberately
configured). Then check the public hostname from a browser/network outside the
host:

1. An unauthenticated request is redirected to Cloudflare Access login or
   rejected; it must not return the status page.
2. After authenticating with an allowed identity,
   `https://tower.<your-domain>/factory/status` returns HTTP 200.
3. `/factory/trace?task_id=<known-id>` is available under the same policy;
   unknown paths return 404.

The app has only GET handlers for `/factory/status` and `/factory/trace`; it
does not expose write routes. Access is enforced at Cloudflare's edge, not by
the loopback origin, so never publish the origin directly.
