"""Autopilot — Klippo's unattended content engine.

Discover YouTube sources → rank them → pick one → run it through the *existing*
Clip Generator → select clips → schedule them into publishing slots → submit
them through the *existing* Upload-Post integration → record everything →
repeat. No browser required after it is switched on.

Design rules this package holds to:

* **No second pipeline.** Video work goes through the same job queue
  ``/api/process`` uses; publishing goes through the same service the manual
  button uses. Autopilot decides *what*, never *how*.
* **State lives in SQLite, not in Python dicts.** A restart at any point must be
  able to say exactly where each source was.
* **One heavy job at a time**, enforced from Autopilot's own state so a changed
  ``MAX_CONCURRENT_JOBS`` cannot melt a laptop.
* **Importable with the standard library alone.** ``app.py`` registers its
  adapters at runtime (see :mod:`automation.ports`); nothing here imports the
  heavy video stack, so the tests run in CI.

Entry points: :func:`automation.service.get_service` and
:data:`automation.api.router`.
"""

__all__ = ["config", "db", "models", "ports", "service"]
