"""Test the hard filters against the existing jobs.json data."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.filters import apply_filters


def main():
    data_file = Path(__file__).resolve().parent / "data" / "jobs.json"
    jobs = json.load(open(data_file))

    passed = []
    rejected = []
    pending = []

    for job in jobs:
        result = apply_filters(job)

        entry = {
            "title": job["title"],
            "company": job.get("company"),
            "status": result.status,
            "score": result.score,
            "seniority": result.seniority,
            "work_model": result.work_model,
            "tech_stack": result.tech_stack,
            "reasons": result.reasons,
            "requires_german": result.requires_german,
        }

        if result.status == "passed":
            passed.append(entry)
        elif result.status == "rejected":
            rejected.append(entry)
        else:
            pending.append(entry)

    print("=" * 70)
    print(f"TOTAL: {len(jobs)} jobs")
    print(f"PASSED: {len(passed)}")
    print(f"REJECTED: {len(rejected)}")
    print(f"PENDING: {len(pending)}")
    print("=" * 70)

    print(f"\n{'─' * 70}")
    print("TOP PASSED (by score)")
    print(f"{'─' * 70}")

    for j in sorted(passed, key=lambda x: -x["score"])[:15]:
        print(
            f"  {j['score']:4.1f}  "
            f"{j['seniority']:<8s}  "
            f"{j['work_model']:<8s}  "
            f"{j['title'][:50]}"
        )

    print(f"\n{'─' * 70}")
    print("REJECTED")
    print(f"{'─' * 70}")

    for j in rejected:
        print(
            f"  {', '.join(j['reasons']):<30s}  "
            f"{j['title'][:50]}"
        )

    print(f"\n{'─' * 70}")
    print("PENDING (no description)")
    print(f"{'─' * 70}")

    for j in sorted(pending, key=lambda x: -x["score"])[:10]:
        print(
            f"  {j['score']:4.1f}  "
            f"{j['title'][:55]}"
        )


if __name__ == "__main__":
    main()
