#!/usr/bin/env python3
"""Pre-flight check for a dedicated Klippo Autopilot machine.

Run it inside the backend container:

    docker compose exec backend python ops/healthcheck.py

It answers the question "is this box actually able to run unattended for weeks",
by checking the things that fail quietly: architecture (a Rosetta image is
several times slower), container RAM, free disk, the binaries the pipeline
shells out to, whether the heavy models load at all, and whether each external
service is reachable and configured.

It never publishes anything. The Upload-Post check reads the profile list; it
does not post, because a health check that posts to a real TikTok account is not
a health check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "· "}


class Report:
    def __init__(self):
        self.rows = []

    def add(self, name, status, detail=""):
        self.rows.append({"check": name, "status": status, "detail": detail})
        return status

    @property
    def failed(self):
        return any(r["status"] == FAIL for r in self.rows)

    def render(self):
        width = max(len(r["check"]) for r in self.rows) + 2
        lines = []
        for row in self.rows:
            lines.append(f"{ICON[row['status']]} {row['check'].ljust(width)}{row['detail']}")
        return "\n".join(lines)


def check_architecture(report):
    machine = platform.machine()
    if machine in ("arm64", "aarch64"):
        return report.add("architecture", OK, f"{machine} — native on Apple Silicon")
    if machine in ("x86_64", "amd64"):
        return report.add(
            "architecture", WARN,
            f"{machine} — this is an amd64 image. On an M1 it runs under Rosetta/QEMU "
            "and transcoding will be several times slower. Rebuild without --platform.")
    return report.add("architecture", WARN, machine)


def check_memory(report):
    """RAM the *container* can use — not the Mac's, which is the usual mistake.

    Docker Desktop caps it, and its default (often 2 GB) is not enough for
    Whisper plus FFmpeg plus a YOLO pass.
    """
    limit = None
    for path in ("/sys/fs/cgroup/memory.max",                 # cgroup v2
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):  # cgroup v1
        try:
            with open(path) as handle:
                raw = handle.read().strip()
            if raw and raw != "max":
                limit = int(raw)
                break
        except (OSError, ValueError):
            continue

    if limit is None or limit > 2 ** 62:
        try:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            return report.add("container memory", WARN, "could not determine")
        gb = total / 1024 ** 3
        return report.add("container memory", OK if gb >= 5 else WARN,
                          f"{gb:.1f} GB (no cgroup limit set)")

    gb = limit / 1024 ** 3
    if gb < 4:
        return report.add("container memory", FAIL,
                          f"{gb:.1f} GB — too little. Docker Desktop → Settings → "
                          "Resources → Memory: allow at least 6 GB (8 GB recommended).")
    if gb < 6:
        return report.add("container memory", WARN,
                          f"{gb:.1f} GB — tight. 6–8 GB avoids OOM on long sources.")
    return report.add("container memory", OK, f"{gb:.1f} GB")


def check_disk(report, path="/app/output"):
    target = path if os.path.isdir(path) else "/"
    usage = shutil.disk_usage(target)
    free_gb = usage.free / 1024 ** 3
    total_gb = usage.total / 1024 ** 3
    detail = f"{free_gb:.1f} GB free of {total_gb:.1f} GB at {target}"
    if free_gb < 5:
        return report.add("free disk", FAIL, detail + " — a single long source can exceed this")
    if free_gb < 20:
        return report.add("free disk", WARN, detail + " — thin for continuous operation")
    return report.add("free disk", OK, detail)


def check_binary(report, name, args, label=None):
    label = label or name
    path = shutil.which(name)
    if not path:
        return report.add(label, FAIL, "not on PATH")
    try:
        proc = subprocess.run([name, *args], capture_output=True, timeout=60)
        version = (proc.stdout or proc.stderr).decode(errors="replace").strip().splitlines()
        return report.add(label, OK, version[0][:80] if version else path)
    except Exception as exc:
        return report.add(label, FAIL, f"{path}: {exc}")


def check_whisper(report):
    started = time.time()
    try:
        from faster_whisper import WhisperModel  # noqa: F401
    except Exception as exc:
        return report.add("whisper", FAIL, f"import failed: {exc}")
    model = os.environ.get("WHISPER_MODEL", "small")
    return report.add("whisper", OK,
                      f"faster-whisper importable, model={model} "
                      f"({time.time() - started:.1f}s to import)")


def check_mediapipe(report):
    try:
        import mediapipe as mp
        detector = mp.solutions.face_detection.FaceDetection(model_selection=1,
                                                             min_detection_confidence=0.5)
        detector.close()
        return report.add("mediapipe", OK, f"face detection initialised (v{mp.__version__})")
    except Exception as exc:
        return report.add("mediapipe", FAIL, str(exc)[:120])


def check_yolo(report):
    try:
        from ultralytics import YOLO
        YOLO("yolov8n.pt")
        return report.add("yolo model", OK, "yolov8n.pt loaded from cache")
    except Exception as exc:
        return report.add("yolo model", FAIL, str(exc)[:120])


def check_autopilot_db(report):
    try:
        from automation.db import AutopilotDB
        db = AutopilotDB().connect()
        version = db.query_one("PRAGMA user_version")[0]
        journal = db.query_one("PRAGMA journal_mode")[0]
        sources = db.query_one("SELECT COUNT(*) AS n FROM discovered_source")["n"]
        path = db.path
        db.close()
        status = OK if journal.lower() == "wal" else WARN
        return report.add("autopilot database", status,
                          f"{path} · schema v{version} · journal={journal} · "
                          f"{sources} source(s)")
    except Exception as exc:
        return report.add("autopilot database", FAIL, str(exc)[:160])


def check_autopilot_config(report):
    try:
        from automation.service import get_service
        service = get_service()
        config = service.get_settings()
        state = service.db.load_engine_state()
        return report.add(
            "autopilot config", OK,
            f"enabled={config['enabled']} · tz={config['timezone']} · "
            f"policy={config['rights']['policy']} · engine={state.get('engine_status')}")
    except Exception as exc:
        return report.add("autopilot config", FAIL, str(exc)[:160])


def check_gemini(report):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return report.add("gemini", FAIL,
                          "GEMINI_API_KEY is not set — unattended runs cannot read the browser")
    try:
        import httpx
        resp = httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                         params={"key": key}, timeout=15)
        if resp.status_code == 200:
            count = len(resp.json().get("models", []))
            return report.add("gemini", OK, f"reachable, key valid ({count} models)")
        return report.add("gemini", FAIL, f"HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        return report.add("gemini", FAIL, str(exc)[:120])


def check_youtube_api(report):
    key = os.environ.get("YOUTUBE_DATA_API_KEY")
    if not key:
        return report.add("youtube data api", FAIL,
                          "YOUTUBE_DATA_API_KEY is not set — Autopilot cannot discover sources")

    async def ping():
        from automation.youtube_client import YouTubeClient
        async with YouTubeClient(key) as client:
            await client.ping()

    try:
        asyncio.run(ping())
        return report.add("youtube data api", OK, "reachable, key valid (1 quota unit spent)")
    except Exception as exc:
        return report.add("youtube data api", FAIL, str(exc)[:140])


def check_upload_post(report):
    key = os.environ.get("UPLOAD_POST_API_KEY")
    user = os.environ.get("UPLOAD_POST_USER")
    if not key:
        return report.add("upload-post", FAIL, "UPLOAD_POST_API_KEY is not set")

    async def profiles():
        import publishing_service
        return await publishing_service.list_profiles(key)

    try:
        found = asyncio.run(profiles())
    except Exception as exc:
        return report.add("upload-post", FAIL, str(exc)[:140])

    names = [p["username"] for p in found]
    if not user:
        return report.add("upload-post", FAIL,
                          f"UPLOAD_POST_USER is not set. Profiles on this key: "
                          f"{', '.join(names) or 'none'}")
    if user not in names:
        return report.add("upload-post", FAIL,
                          f"UPLOAD_POST_USER={user!r} is not on this account. "
                          f"Available: {', '.join(names) or 'none'}")
    connected = next((p["connected"] for p in found if p["username"] == user), [])
    if not connected:
        return report.add("upload-post", WARN,
                          f"profile {user!r} exists but has no social account linked yet")
    # Nothing is posted here — only the profile list is read.
    return report.add("upload-post", OK,
                      f"profile {user!r} linked to: {', '.join(connected)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--skip-models", action="store_true",
                        help="skip Whisper/MediaPipe/YOLO (slow to import)")
    parser.add_argument("--skip-network", action="store_true",
                        help="skip Gemini / YouTube / Upload-Post reachability")
    args = parser.parse_args()

    report = Report()
    check_architecture(report)
    check_memory(report)
    check_disk(report)
    check_binary(report, "ffmpeg", ["-version"])
    check_binary(report, "ffprobe", ["-version"])
    check_binary(report, "yt-dlp", ["--version"])

    if args.skip_models:
        for name in ("whisper", "mediapipe", "yolo model"):
            report.add(name, SKIP, "skipped (--skip-models)")
    else:
        check_whisper(report)
        check_mediapipe(report)
        check_yolo(report)

    check_autopilot_db(report)
    check_autopilot_config(report)

    if args.skip_network:
        for name in ("gemini", "youtube data api", "upload-post"):
            report.add(name, SKIP, "skipped (--skip-network)")
    else:
        check_gemini(report)
        check_youtube_api(report)
        check_upload_post(report)

    if args.json:
        print(json.dumps({"ok": not report.failed, "checks": report.rows}, indent=2))
    else:
        print("\nKlippo Autopilot — machine health\n" + "-" * 34)
        print(report.render())
        print()
        print("FAILED — fix the ❌ rows before leaving this machine unattended."
              if report.failed else "All required checks passed.")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
