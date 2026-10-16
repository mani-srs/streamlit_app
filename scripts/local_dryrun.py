"""Local dry run: sample DDL through all 7 agents, auto-approving every review gate.

  .venv\\Scripts\\python scripts\\local_dryrun.py            # no LLM (zero model calls)
  .venv\\Scripts\\python scripts\\local_dryrun.py --llm      # Cortex via the default connection
  ... --out out\\bundle                                     # write the generated files
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agents import master  # noqa: E402
from core.schemas import HumanFeedback  # noqa: E402
from core.state import RunStatus, SliceSpec  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ddl", default=os.path.join(ROOT, "samples", "sample_slice.sql"))
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--connection", default=None)
    ap.add_argument("--catalog", default="")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    db = None
    if a.llm:
        from core.db import connect
        db = connect(a.connection)
    backend = master.Backend(db=db)
    st = master.new_run(SliceSpec(pasted_ddl=open(a.ddl, encoding="utf-8").read(), target_catalog=a.catalog),
                        use_llm=a.llm)
    st = master.run(backend, st, progress=lambda m: print("  ..", m))
    while st.status == RunStatus.AWAITING_HUMAN:
        p = st.pending
        print(f"[gate] {p['stage']} iter {p['iteration']}: {p['eval']['verdict']} - {p['eval']['summary']} "
              f"({len(p['eval']['issues'])} issues) -> auto-accept")
        for i in p["eval"]["issues"]:
            if i["severity"] != "info":
                print(f"     [{i['severity']}] {i['id']} {i['ref']}: {i['message'][:160]}")
        st = master.resume(backend, st, HumanFeedback(accepted=True))
    print("status:", st.status, st.error[:2000])
    if st.status != RunStatus.DONE:
        return 1
    files = st.outputs["classifier"]["files"]
    val = st.outputs["validation"]["report"]
    print("files:", len(files))
    for n in sorted(files):
        print(f"  {n} ({len(files[n])} bytes)")
    print("judge counts:", json.dumps(val["counts"]))
    for i in val["issues"]:
        if i["severity"] != "info":
            print(f"  [{i['severity']}] {i['id']} {i['ref']}: {i['message']}")
    print("llm calls:", len(backend.obs.llm_calls(st.run_id)))
    if a.out:
        for n, t in files.items():
            p = os.path.join(a.out, n)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
        print("written to", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
