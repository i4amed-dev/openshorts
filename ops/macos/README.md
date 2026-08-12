# Running Klippo Autopilot on a dedicated MacBook Pro (M1, 2020)

Autopilot is designed to run for weeks with nobody watching. That is a macOS
problem as much as a Klippo one: a Mac that goes to sleep, or that reboots into
a login window, stops being a server. This directory documents what to change,
what the application deliberately does *not* do for you, and what actually
happens after a power cut.

Nothing here is run by the app. Klippo never executes `pmset`, `caffeinate`,
`sudo`, or any other privileged power-management command on your behalf —
silently changing a machine's power policy is not a video tool's business, and
the settings below are ones you should make consciously and be able to undo.

---

## 1. Power and sleep

**Plug it in and leave it plugged in.** On battery, macOS aggressively throttles
and sleeps regardless of anything below.

Then, in **System Settings → Battery → Options**:

| Setting | Value | Why |
|---|---|---|
| Prevent automatic sleeping on power adapter when the display is off | **On** | The single most important switch. Without it the Mac sleeps mid-render and the job resumes only when you wake it. |
| Wake for network access | On | Lets you SSH/screen-share in without walking to the machine. |
| Enable Power Nap | Off | Irrelevant here and it wakes the disk for no benefit. |

In **System Settings → Lock Screen**, set *Turn display off on power adapter when
inactive* to something short (2 minutes). Turning the **display** off is fine
and saves power; it is *system* sleep you are preventing.

### About `caffeinate`

`keepawake.sh` in this directory wraps `caffeinate`. It is a **belt-and-braces
measure, not a substitute** for the Battery setting above:

- `caffeinate -dimsu` asserts that the display, disk, system and user are busy.
- It does **not** override clamshell sleep. **Closing the lid on a MacBook still
  sleeps the machine** unless an external display, keyboard and power adapter are
  all connected — that is macOS behaviour Apple does not expose a supported knob
  for, and no amount of `caffeinate` changes it.

So: if you want the lid closed, you need clamshell mode (power + external
display + external input). If you cannot do that, **leave the lid open** with
the display timeout short. That is the reliable configuration.

### Thermals

An M1 MacBook Pro transcoding for hours will get hot and will throttle. It will
not damage itself, it will just get slower — which for Autopilot means a source
takes longer, not that anything breaks. Still:

- Put it on something hard and flat. Not a bed, not a closed drawer, not stacked
  under anything.
- Keep the vents at the back and the underside clear.
- `MAX_CONCURRENT_JOBS=1` (the default) matters here too: two concurrent
  pipelines heat-soak the chassis far faster than one does.

---

## 2. Docker Desktop

**Settings → General → Start Docker Desktop when you sign in: on.**

**Settings → Resources:**

| Resource | Minimum | Recommended |
|---|---|---|
| Memory | 6 GB | 8 GB |
| CPU | 4 | 6–8 |
| Disk image size | 60 GB | 100 GB+ |

The default allocation is *not* enough. Whisper, MediaPipe/YOLO and FFmpeg run
in the same container, and a long source with a 2 GB limit gets OOM-killed
part-way through rendering. `ops/healthcheck.py` reports the limit the container
actually sees, which is the number that matters — not the Mac's total RAM.

Then bring the stack up once by hand so images are built and cached:

```bash
cd /path/to/klippo
docker compose up -d --build
docker compose exec backend python ops/healthcheck.py
```

---

## 3. What happens after a reboot

This is the part people get wrong, so it is worth being blunt about it.

**Docker Desktop on macOS starts after a user logs in.** It is not a system
daemon. So:

| Scenario | Does Klippo come back? |
|---|---|
| `docker compose restart`, or the backend container crashes | **Yes, immediately.** `restart: unless-stopped` handles it, and Autopilot reconciles its state on boot. |
| Mac wakes from sleep | **Yes.** Containers were never stopped. |
| Mac reboots *and the user session is restored* (no FileVault, or you unlock it) | **Yes**, once Docker Desktop finishes starting — typically 1–2 minutes. |
| Mac cold-boots and sits at the FileVault / login screen | **No.** Nothing runs until someone logs in. |

If unattended recovery from a cold boot matters to you, you must decide about
FileVault and auto-login. **This documentation does not change either for you,
and the scripts here do not either** — disabling full-disk encryption or enabling
passwordless auto-login is a real security trade-off on a machine holding your
API keys, and it is yours to make deliberately. The options are:

1. **Leave FileVault on (recommended).** Accept that a cold boot needs someone
   to unlock the disk once. In practice this is rare — a Mac on a UPS or a
   stable outlet reboots only for updates you initiate.
2. **Turn FileVault off and enable auto-login.** The Mac returns to a logged-in
   session unattended, and anyone with physical access has your `.env`.

Either way, keep **System Settings → Energy → Start up automatically after a
power failure** on.

---

## 4. The LaunchAgent (optional)

`com.klippo.autopilot.plist` is a *user* LaunchAgent. It does two small things
after you log in: waits for the Docker daemon, then runs `docker compose up -d`.
It exists because Docker Desktop's own restart policy only revives containers
it was managing when it last exited — if the compose project was never started
in that session, nothing comes back.

It runs as your user. It is not a `LaunchDaemon`, does not need `sudo`, and
touches nothing outside the project directory.

```bash
# Point it at your checkout, then install it:
sed -i '' "s#__KLIPPO_DIR__#$PWD#g" ops/macos/com.klippo.autopilot.plist
cp ops/macos/com.klippo.autopilot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.klippo.autopilot.plist

# Check it:
launchctl list | grep klippo
tail -f /tmp/klippo-autopilot.log

# Remove it:
launchctl unload ~/Library/LaunchAgents/com.klippo.autopilot.plist
rm ~/Library/LaunchAgents/com.klippo.autopilot.plist
```

---

## 5. Keeping it awake

```bash
# Foreground, Ctrl-C to stop:
ops/macos/keepawake.sh

# Or as a LaunchAgent alongside the compose one — see the header of the script.
```

Read section 1 first. This script is the *second* line of defence, and it cannot
keep a closed lid awake.

---

## 6. Checking in on it

```bash
# Everything at once
docker compose exec backend python ops/healthcheck.py

# Just the machine, no network calls or model loads (fast)
docker compose exec backend python ops/healthcheck.py --skip-network --skip-models

# What Autopilot itself thinks
curl -s localhost:8001/health/detail | python3 -m json.tool

# Cost of one real source on this machine
docker compose exec backend python ops/benchmark.py --url "https://youtu.be/..." -v
```

The dashboard's **Autopilot** tab is the everyday view: what it found, what it
chose and why, what is scheduled, and what failed.

---

## 7. Turning it off in a hurry

In order of escalation:

1. **Dashboard → Autopilot → Pause.** Finishes the current job, starts nothing new.
2. **Dashboard → Autopilot → Disable.** Stops the engine entirely. Posts already
   accepted by Upload-Post remain on their calendar.
3. **Dashboard → Autopilot → Emergency stop.** Disables, cancels every post not
   yet sent, and drops the candidate queue. Again: posts Upload-Post has already
   accepted can only be cancelled in Upload-Post.
4. **`AUTOPILOT_ENABLED=0` in `.env` + `docker compose up -d backend`.** Removes
   the routes and the scheduler loop from the process. Manual mode is unaffected.
5. **`docker compose down`.** Everything stops. State survives in the
   `autopilot-state` volume, and Autopilot reconciles when you bring it back.
