# OpenClaw Pub/Sub Doorbell (Minimal Debounce Setup)

## Purpose

Create a very small host-side webhook listener on the VPS that receives
Google Pub/Sub push notifications and triggers the OpenClaw `gog` email
check once after a 30-minute delay.

The Pub/Sub push is used only as a doorbell signal. No Pub/Sub payload
needs to be passed into the container.

This system intentionally prioritizes simplicity over reliability.
Missing events is acceptable because OpenClaw can catch up later or be
manually triggered.

---

## High-Level Behavior

When Pub/Sub sends push requests:

1. Listener receives HTTP `POST` on `/pubsub`.
2. Listener validates a shared secret token in the query string.
3. Listener immediately returns `200 OK`.
4. If no timer is currently active:
5. Create a `pending` marker file.
6. Start a detached background job.
7. The background job waits 30 minutes.
8. After the delay, it runs the OpenClaw command inside Docker.
9. It removes the `pending` marker.

Additional pushes during the 30-minute window still return `200 OK` but
do not schedule another timer.

---

## Design Goals

Keep the system intentionally minimal.

Allowed:

- host-based listener
- simple state files
- file locking for concurrency safety
- detached background process
- simple shared secret authentication
- basic logging

Not required:

- database
- Redis
- queues
- Pub/Sub payload parsing
- deduplication
- retry logic
- durable scheduling

If the VPS reboots during the delay, losing the trigger is acceptable.

---

## Host Layout

Everything lives in one folder:

`/root/.pubsub-runner/`

Files:

- `/root/.pubsub-runner/pubsub-runner.py`
- `/root/.pubsub-runner/pending`
- `/root/.pubsub-runner/lock`
- `/root/.pubsub-runner/openclaw-doorbell.log`

---

## Docker Command

When the delay expires, the host runs:

`docker exec db5da794965f bash -lc "openclaw agent --agent main --message 'check if there is new mail, use gog skill'"`

---

## Listener Logic

1. Receive `POST` on `/pubsub`
2. Validate `?token=SECRET`
3. Acquire the lock file
4. If `pending` exists:
5. Return `200`
6. If `pending` does not exist:
7. Create `pending`
8. Spawn the detached background worker
9. Return `200`

Marker example:

`date -Is > /root/.pubsub-runner/pending`

---

## Delayed Job

The detached worker should:

1. `sleep 1800`
2. Run the Docker command
3. Remove `/root/.pubsub-runner/pending`
4. Append output to `/root/.pubsub-runner/openclaw-doorbell.log`

---

## Concurrency Safety

Use a lock file to prevent two simultaneous requests from scheduling
multiple timers.

Example concept:

`flock -n /root/.pubsub-runner/lock`

---

## Timeline Example

10:00 first push -> schedule timer  
10:05 second push -> ignored  
10:30 docker exec runs  
10:30 pending removed  
10:40 new push -> new timer starts

---

## Default Configuration

- Host: `0.0.0.0`
- Port: `8788`
- Endpoint: `/pubsub`
- Token transport: query string, `?token=SECRET`
- Delay: `1800` seconds

---

## Start Command

From `/root/.pubsub-runner/`:

`python3 pubsub-runner.py`

Pub/Sub push endpoint:

`http://SERVER:8788/pubsub?token=SECRET`

---

## Testing

Trigger locally:

`curl -X POST "http://localhost:8788/pubsub?token=SECRET"`

Expected:

- returns `200`
- `pending` file created
- delayed job scheduled

Check:

- `ls /root/.pubsub-runner/`
- `tail -f /root/.pubsub-runner/openclaw-doorbell.log`

After 30 minutes:

- Docker command executed
- `pending` removed

---

## Acceptance Criteria

- first push schedules delayed trigger
- further pushes ignored during delay
- exactly one Docker command runs
- system resets after execution
