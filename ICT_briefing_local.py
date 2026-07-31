"""ICT_briefing_local.py — generate + publish the trading briefing LOCALLY.

Why this exists: GitHub Actions' scheduled runs on this repo fire 1h45m-2h40m
late (measured 2026-07-30 and 2026-07-31), and because daily_briefing.py picks
its session from the wall-clock hour at execution time, that lateness makes the
session drift — on 2026-07-31 the London run executed at 06:07 ET and labelled
itself "us", so no London briefing was ever produced. The CI ANTHROPIC_API_KEY
secret is also stale (401), so every CI narrative fails.

This runs the same generator on the local box, where the key is valid and the
Windows scheduler fires on time, then commits and pushes docs/ and dispatches
the workflow so GitHub Pages redeploys immediately (workflow_dispatch is not
subject to the schedule delay).

The session is passed EXPLICITLY, never auto-detected, so a late run still
produces the briefing it was meant to produce.

Usage:  pythonw ICT_briefing_local.py --session london|us|nyopen
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
DOCS = REPO / "docs"
LOG = REPO / "ICT_briefing_local_log.txt"
PY = r"C:\Users\Anwender\AppData\Local\Programs\Python\Python314\python.exe"
NO_WINDOW = 0x08000000


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def run(cmd: list[str], cwd: Path = REPO, timeout: int = 900):
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          timeout=timeout, creationflags=NO_WINDOW)


def main() -> int:
    session = "us"
    if "--session" in sys.argv:
        i = sys.argv.index("--session") + 1
        if i < len(sys.argv):
            session = sys.argv[i]
    if session not in ("london", "us", "nyopen"):
        log(f"FATAL: bad session {session!r}")
        return 2

    log(f"=== {session} run start ===")

    # Windows scheduled tasks do NOT inherit a shell-session env var, so the key
    # is loaded from a secrets file kept OUTSIDE any git repo (it can therefore
    # never be committed). Env var still wins if one is already present.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        secrets = Path.home() / ".ict_secrets.env"
        if secrets.exists():
            for ln in secrets.read_text(encoding="utf-8").splitlines():
                if "=" in ln and not ln.lstrip().startswith("#"):
                    k, v = ln.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            log(f"loaded secrets from {secrets.name}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("FATAL: ANTHROPIC_API_KEY unavailable — the narrative would fail and "
            "publish a DEGRADED page. Aborting instead.")
        return 3

    # ---- refresh MenthorQ levels first so the gamma panel isn't a day stale ----
    # The fetcher drives dashboard.menthorq.io through the Chrome DevTools
    # session on :9222 and occasionally drops a symbol (SPX was missing from the
    # 14:51 pull on 2026-07-31), so verify coverage and retry once. Levels are a
    # nice-to-have: a miss degrades the panel, it does not block the briefing.
    NOKEPA = Path(r"C:\Users\Anwender\Code\nokepa")
    MQ_JSON = NOKEPA / "data" / "mq_levels.json"
    WANT = {"SPX", "NDX", "SPY", "QQQ", "ES", "NQ"}

    def _mq_syms() -> set[str]:
        try:
            import json
            return set(json.loads(MQ_JSON.read_text(encoding="utf-8")).get("levels", {}))
        except Exception:
            return set()

    try:
        import urllib.request as _u
        _u.urlopen("http://localhost:9222/json/list", timeout=5).read()
        cdp = True
    except Exception:
        cdp = False
        log("WARN: Chrome CDP :9222 unreachable — MenthorQ levels will be stale")

    if cdp:
        for attempt in (1, 2):
            r = run([PY, str(NOKEPA / "scripts" / "ICT_mq_levels_fetch.py")],
                    cwd=NOKEPA, timeout=600)
            got = _mq_syms()
            missing = WANT - got
            log(f"mq levels attempt {attempt}: {len(got)}/6"
                + (f" — missing {sorted(missing)}" if missing else " — complete"))
            if not missing:
                break
    else:
        log(f"mq levels: using cached {sorted(_mq_syms())}")

    # ---- generate straight into docs/ (same as CI does via TRADING_DIR) ----
    env = dict(os.environ, TRADING_DIR=str(DOCS))
    DOCS.mkdir(exist_ok=True)
    r = subprocess.run([PY, str(REPO / "daily_briefing.py"), "--session", session, "--force"],
                       cwd=str(REPO), capture_output=True, text=True, env=env,
                       timeout=1200, creationflags=NO_WINDOW)
    out = (r.stdout or "") + (r.stderr or "")
    for ln in out.splitlines():
        if ln.strip():
            log("  gen| " + ln.strip())
    if r.returncode != 0:
        log(f"FATAL: generator exited {r.returncode}")
        return 4

    # Refuse to publish a briefing whose analytical half is dead.
    if "NARRATIVE FAILED" in out or "NARRATIVE EMPTY" in out:
        log("ABORT: narrative failed — not committing a DEGRADED page. "
            "Local docs/ left dirty for inspection.")
        return 5
    log("narrative OK")

    # ---- commit + push ----
    run(["git", "add", "docs/"])
    st = run(["git", "diff", "--staged", "--quiet"])
    if st.returncode == 0:
        log("nothing to commit (identical output)")
    else:
        stamp = datetime.now().strftime("%Y-%m-%d")
        c = run(["git", "commit", "-m", f"briefing: {stamp} ({session}) [local]"])
        log(f"commit rc={c.returncode} {c.stdout.strip()[:120]}")
        # The CI bot can commit its own (degraded) briefing while we generate,
        # which makes the push non-fast-forward. Rebase onto origin and keep OUR
        # file — ours has a live narrative, the bot's does not. During a rebase
        # the replayed commit is "--theirs".
        p = run(["git", "push", "origin", "main"], timeout=300)
        if p.returncode != 0:
            log("push rejected — rebasing onto origin/main and retrying")
            run(["git", "fetch", "origin"], timeout=300)
            rb = run(["git", "-c", "core.editor=true", "rebase", "origin/main"], timeout=300)
            if rb.returncode != 0:
                conflicts = run(["git", "diff", "--name-only", "--diff-filter=U"])
                files = [f for f in conflicts.stdout.split("\n") if f.strip()]
                for f in files:
                    run(["git", "checkout", "--theirs", "--", f])
                    run(["git", "add", f])
                log(f"resolved {len(files)} conflict(s) in favour of the local build")
                run(["git", "-c", "core.editor=true", "rebase", "--continue"], timeout=300)
            p = run(["git", "push", "origin", "main"], timeout=300)
        log(f"push rc={p.returncode} {(p.stdout + p.stderr).strip()[:160]}")
        if p.returncode != 0:
            log("push FAILED — page will not update")
            return 6

    # ---- dispatch the workflow so Pages redeploys now (not on the late cron) ----
    d = run(["gh", "workflow", "run", "daily_briefing.yml",
             "-f", f"session={session}", "-f", "force=false"], timeout=180)
    log(f"pages dispatch rc={d.returncode} {(d.stdout + d.stderr).strip()[:160]}")

    log(f"=== {session} run done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
