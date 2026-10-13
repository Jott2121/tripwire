"""Score a holdout-1 run (amendment A4) against its fixed criteria. Read-only, zero spend.

python scripts/holdout_report.py <out/holdout-1-RUNID> [more dirs] --out reports
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tripwire import holdout
from tripwire import report as r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dirs", type=Path, nargs="+")
    ap.add_argument("--out", type=Path, default=Path("reports"))
    a = ap.parse_args()
    rows, src = r.load_records(a.run_dirs)
    rep = r.analyse(rows, src)
    r.write_report(rep, a.out, "holdout_1")
    res = holdout.evaluate(rep)
    (a.out / "holdout_1_verdict.json").write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
