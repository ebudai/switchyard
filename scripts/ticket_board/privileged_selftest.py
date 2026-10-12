"""SYRD-113's `job-selftest`, entire: a synthetic privileged job that reads and changes nothing.

The catalogued `job-selftest` action's operation is `<pinned launcher>
job-selftest <project> --outcome succeed|fail`, run by root as every catalogued
operation is. Both `switchyard` wrappers hand that straight here, before they
import the launcher or anything else, so a self-test reads nothing but its
arguments. It prints fixed lines -- one a fixed fake credential, so a run shows
its record redacting it -- and ends as asked. The project is only checked for
its form; nothing about it is looked up.
"""

import sys

USAGE = "usage: switchyard job-selftest <project> --outcome succeed|fail"


def _slug(value: str) -> bool:
    return (0 < len(value) <= 32 and "a" <= value[0] <= "z"
            and all("a" <= c <= "z" or "0" <= c <= "9" or c == "-" for c in value))


def main(argv: list) -> int:
    if len(argv) != 3 or argv[1] != "--outcome" or argv[2] not in ("succeed", "fail") or not _slug(argv[0]):
        print(USAGE, file=sys.stderr)
        return 2
    outcome = argv[2]
    print("switchyard job-selftest: a synthetic privileged job; nothing on this host is read or changed")
    print("switchyard job-selftest: API_TOKEN=selftest-fake-credential-0000")
    print("switchyard job-selftest: Authorization: Bearer selftest-fake-bearer-0000", file=sys.stderr)
    print("switchyard job-selftest: outcome " + outcome)
    return 0 if outcome == "succeed" else 1
