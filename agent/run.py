"""CLI entrypoint.

  python -m agent.run registries [--app NAME]      deterministic lookups -> results/registry/<slug>.json
  python -m agent.run probes     [--app NAME]      empirical probes     -> results/probes/<slug>.json
  python -m agent.run pass1      [--app NAME]      weak baseline        -> results/pass1/<slug>.json
  python -m agent.run pass2      [--app NAME]      full research        -> results/pass2/<slug>.json
  python -m agent.run judge                        quote grounding      -> results/judge/<slug>.json
  python -m agent.run merge                        vote + score         -> results/final/<slug>.json, results/final.json
  python -m agent.run all        [--app NAME]      everything for one app (or all)

Every stage is resumable: existing outputs are skipped unless --force.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .common import RESULTS_DIR, load_apps, read_json, slug, write_json


def _out(stage: str, app: dict) -> Path:
    return RESULTS_DIR / stage / f"{slug(app['name'])}.json"


def _select(args):
    apps = load_apps()
    if args.app:
        wanted = {a.strip().lower() for a in args.app.split(",")}
        apps = [a for a in apps if a["name"].lower() in wanted or slug(a["name"]) in wanted]
        if not apps:
            sys.exit(f"no app matched {args.app!r}")
    return apps


def _run_stage(stage: str, apps, fn, workers: int, force: bool):
    def _needs(a):
        p = _out(stage, a)
        if force or not p.exists():
            return True
        if args_global and args_global.retry_failed:
            r = read_json(p, {})
            return isinstance(r, dict) and "ok" in r and not r["ok"]
        return False
    todo = [a for a in apps if _needs(a)]
    if args_global and args_global.reverse:
        todo = todo[::-1]
    print(f"[{stage}] {len(todo)} to run, {len(apps) - len(todo)} cached, workers={workers}", flush=True)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        def guarded(a):
            if not force and _out(stage, a).exists() and not (args_global and args_global.retry_failed):
                return read_json(_out(stage, a))
            return fn(a)
        futs = {ex.submit(guarded, a): a for a in todo}
        for f in as_completed(futs):
            a = futs[f]
            try:
                res = f.result()
                write_json(_out(stage, a), res)
                done += 1
                extra = ""
                if isinstance(res, dict) and "ok" in res:
                    extra = f" ok={res['ok']} turns={res.get('turns')} ${res.get('cost_usd') or 0:.3f} {res.get('seconds')}s" + (f" ERR={res.get('error')}" if not res["ok"] else "")
                print(f"  [{stage}] {done}/{len(todo)} {a['name']}{extra}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"  [{stage}] FAILED {a['name']}: {type(e).__name__}: {e}", flush=True)
    print(f"[{stage}] finished in {time.time() - t0:.0f}s", flush=True)


def stage_registries(apps, workers, force):
    from .registries import lookup_all
    _run_stage("registry", apps, lookup_all, workers, force)


def stage_probes(apps, workers, force):
    from .probes import probe
    _run_stage("probes", apps, probe, workers, force)


def stage_pass1(apps, workers, force):
    from .research import research
    _run_stage("pass1", apps, lambda a: research(a, "pass1"), workers, force)


def stage_pass2(apps, workers, force):
    from .research import research

    def fn(a):
        reg = read_json(_out("registry", a), {})
        pr = read_json(_out("probes", a), {})
        return research(a, "pass2", reg, pr)
    _run_stage("pass2", apps, fn, workers, force)


def stage_judge(apps, workers, force):
    from .judge import judge

    def fn(a):
        return judge(a, read_json(_out("pass2", a), {}))
    _run_stage("judge", apps, fn, workers, force)


def stage_merge(apps, workers, force):
    from .merge import merge_app, summarize
    finals = []
    for a in apps:
        m = merge_app(a, read_json(_out("registry", a), {}), read_json(_out("probes", a), {}),
                      read_json(_out("pass1", a), {}), read_json(_out("pass2", a), {}), read_json(_out("judge", a), {}))
        write_json(_out("final", a), m)
        finals.append(m)
    if not args_global.app:
        from .consistency import consistency_pass
        finals = consistency_pass(finals)
        for m in finals:
            write_json(RESULTS_DIR / "final" / f"{slug(m['name'])}.json", m)
        write_json(RESULTS_DIR / "final.json", finals)
        write_json(RESULTS_DIR / "human_queue.json", [
            {"name": m["name"], "confidence": m["confidence"], "reasons": m["needs_human_reasons"]}
            for m in sorted(finals, key=lambda x: x["confidence"]) if m["needs_human"]])
        summarize(finals)
    print(f"[merge] wrote {len(finals)} final rows")


args_global = None


def main():
    global args_global
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["registries", "probes", "pass1", "pass2", "judge", "merge", "all"])
    ap.add_argument("--app", help="comma-separated app names (default: all 100)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reverse", action="store_true", help="process the list from the end (lets two runs share the work)")
    ap.add_argument("--retry-failed", action="store_true", help="re-run apps whose LLM stage returned ok=false")
    args = ap.parse_args()
    args_global = args
    apps = _select(args)
    w = args.workers
    stages = {
        "registries": (stage_registries, w or 6), "probes": (stage_probes, w or 6),
        "pass1": (stage_pass1, w or 4), "pass2": (stage_pass2, w or 4),
        "judge": (stage_judge, w or 6), "merge": (stage_merge, 1),
    }
    order = ["registries", "probes", "pass1", "pass2", "judge", "merge"] if args.stage == "all" else [args.stage]
    for s in order:
        fn, workers = stages[s]
        fn(apps, workers, args.force)


if __name__ == "__main__":
    main()
