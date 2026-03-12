# pubsub-runner

Small Python webhook listener for Google Cloud Pub/Sub push messages.

Its job is simple:

1. Receive a push request from Pub/Sub.
2. Verify a shared secret token.
3. Return `200 OK` immediately.
4. If no job is already pending, wait for a delay.
5. Run an OpenClaw command inside Docker.

This is a lightweight "doorbell" service. It does not process Pub/Sub
message contents. The push request is only used as a signal to trigger a
later action.

The delay is important. It acts as a simple debounce window: if several
Pub/Sub push requests arrive close together, the script schedules only
one OpenClaw run instead of triggering the same action many times.
The actual triage and processing of emails is handled later by the
OpenClaw agent inside the container.

## How It Works

`pubsub-runner.py` runs a local HTTP server on:

`http://127.0.0.1:PORT`

The public internet should not call this port directly. In production,
an HTTPS reverse proxy such as Traefik should expose a public URL such
as:

`https://your-domain.example/pubsub?token=...`

and forward that request to:

`http://127.0.0.1:PORT/pubsub?token=...`

When a valid push arrives:

- the script checks the token in the URL query string
- it returns `200 OK` right away so Pub/Sub knows the push was accepted
  and does not need to retry it
- if no run is pending, it creates a `pending` marker file
- the `pending` file is a simple flag that means "a delayed run is
  already scheduled"
- it starts a detached background worker
- the worker sleeps for the configured delay
- after the delay, it runs the Docker command
- it removes the `pending` marker

If another push arrives while `pending` exists, the script still returns
`200 OK`, but it does not schedule a second run.

## Files

Everything lives in `/root/.pubsub-runner/`.

- `pubsub-runner.py`: the listener and delayed worker
- `pending`: marker file showing a delayed run is already scheduled
- `lock`: lock file used to prevent two requests at the same time from
  scheduling duplicate runs
- `openclaw-doorbell.log`: log file for accepted requests, rejections,
  worker activity, and Docker output

## Current Defaults

These are the current built-in defaults from the script:

- Host: `127.0.0.1`
- Port: `PORT`
- Endpoint path: `/pubsub`
- Shared token env var: `SHARED_TOKEN`
- Default token if unset: `SECRET`
- Delay env var: `DELAY_SECONDS`
- Default delay: `1800` seconds

The Docker command currently executed after the delay is:

```bash
docker exec openclaw-container-name bash -lc "openclaw agent --agent gog-main --message 'retrieve latest mail using gog skill and process it'"
```
(Dont use container ID as it could change)

## Requirements

- Python 3
- Docker available on the host
- A running OpenClaw container
- A reverse proxy or other public HTTPS endpoint that forwards requests
  to `127.0.0.1:PORT`
- A Google Cloud Pub/Sub push subscription pointing at the public URL

## Starting The Service

From the project folder:

```bash
cd /root/.pubsub-runner
SHARED_TOKEN='your-token-here' python3 pubsub-runner.py
```

You should see startup lines showing the host, port, log path, and
delay.

Note: You should include it in your systemctl daemon so it automaticly restarts if VPS reboots.

## Pub/Sub Endpoint

The Pub/Sub push subscription should send requests to a public HTTPS
URL such as:

```text
https://your-domain.example/pubsub?token=your-token-here
```

That token must exactly match the `SHARED_TOKEN` used to start the
script.


## Testing

Test the script directly on the VPS:

```bash
curl -i -X POST "http://127.0.0.1:PORT/pubsub?token=your-token-here"
```

Test the public HTTPS route:

```bash
curl -i -X POST "https://your-domain.example/pubsub?token=your-token-here"
```

Useful checks:

```bash
curl http://127.0.0.1:PORT/healthz
tail -f /root/.pubsub-runner/openclaw-doorbell.log
ls -l /root/.pubsub-runner/
```

## What Success Looks Like

For a valid first push:

- the request returns `200 OK`
- `200 OK` is the acknowledgment Pub/Sub expects for a successful push
- `pending` is created
- the log shows `accepted request ...: scheduled`
- the log shows `pending worker started`

For extra pushes during the delay:

- the request still returns `200 OK`
- the log shows `accepted request ...: already-pending`

After the delay:

- the Docker command runs
- the log shows the Docker exit code
- `pending` is removed

## Troubleshooting

- `403 invalid token`: the token in the Pub/Sub URL does not match
  `SHARED_TOKEN`
- `404 page not found` on the public URL: the reverse proxy is not
  routing `/pubsub` to the script
- no response on `127.0.0.1:PORT`: the Python script is not running
- `pending` never clears: the Docker command may be hanging or taking
  much longer than expected

## Limitations

This service is intentionally minimal:

- it does not parse Pub/Sub payload contents
- it does not deduplicate messages beyond the simple `pending` marker
- it does not survive host restarts during the delay
- it assumes missing one trigger is acceptable
- to mitigate skipped pub/sub pushes, make sure triggered process in openclaw runs thru all un-read emails. 
