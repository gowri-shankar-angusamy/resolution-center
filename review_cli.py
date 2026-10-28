"""Human reviewer console (command line). The only way to approve, override, reject or execute.

  python review_cli.py queue [--status APPROVED]
  python review_cli.py show   CASE_ID
  python review_cli.py approve  CASE_ID --reviewer NAME [--role senior] [--reason TEXT]
  python review_cli.py override CASE_ID --reviewer NAME --outcome DENY --reason TEXT [--role senior]
  python review_cli.py reject   CASE_ID --reviewer NAME --reason TEXT
  python review_cli.py execute  CASE_ID --reviewer NAME [--role senior]
  python review_cli.py verify

Set RC_AUDIT_LOG to use a different audit file. Execution is a mock and moves no money.
"""
import argparse
import json
import sys

from approvals import REVIEWER, ApprovalError, ApprovalService


def main(argv=None, svc=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("queue"); q.add_argument("--status")
    sub.add_parser("show").add_argument("case_id")
    sub.add_parser("verify")
    for name in ("approve", "override", "reject", "execute"):
        s = sub.add_parser(name)
        s.add_argument("case_id")
        s.add_argument("--reviewer", required=True)
        s.add_argument("--role", default=REVIEWER, choices=("reviewer", "senior"))
        s.add_argument("--reason", default="")
        if name == "override":
            s.add_argument("--outcome", required=True)
    a = p.parse_args(argv)
    svc = svc or ApprovalService()

    try:
        if a.cmd == "queue":
            items = svc.queue(a.status)
            for i in items:
                print(f"{i['case_id']:<10} {i['status']:<24} {i['recommended_outcome']:<17} "
                      f"{i['rule_id']:<8} amount={i['amount']}")
            print(f"{len(items)} item(s)")
        elif a.cmd == "show":
            st = svc.state(a.case_id)
            if st is None:
                print(f"no record for {a.case_id}")
                return 1
            print(json.dumps(st, indent=2, ensure_ascii=False))
        elif a.cmd == "verify":
            ok, bad = svc.verify_log()
            print("audit log OK" if ok else f"audit log TAMPERED at entry {bad}")
            return 0 if ok else 2
        elif a.cmd == "execute":
            print(json.dumps(svc.execute(a.case_id, a.reviewer, a.role), indent=2))
        else:
            decision = a.cmd
            st = svc.review(a.case_id, a.reviewer, a.role, decision, a.reason,
                            getattr(a, "outcome", None))
            print(f"{a.case_id}: {st['status']} (final outcome {st['final_outcome']})")
    except ApprovalError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
