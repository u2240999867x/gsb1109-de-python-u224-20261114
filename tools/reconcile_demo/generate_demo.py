"""Generate a synthetic two-repository demo dataset for reconcile_inventories.

The scenario covers every evidence rule:

- X exists in both repos; repo A keeps a readable original, so its
  recomputed sha256+sha512 pair links the two algorithms across repos.
- Y only survives as readable bytes mislabeled in repo A (a stale sha256
  claim pointing at Z's digest); quarantining that claim stops it from
  bridging Z's group and Y's group into a false merge.
- Z (repo A) and W (repo B) have equal recorded sizes but only
  cross-algorithm claims and no readable originals: insufficient evidence.
- V is unique to repo A; a stranger digest in repo B is provably different
  from every comparable group.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

HERE = Path(__file__).parent
ARTIFACTS = HERE / "artifacts"

X = b"shared payload: identical in both repos\n"
Y = b"repo-b-only payload, mislabeled in repo a\n"
Z = b"repo-a-only-payload!\n"
W = b"repo-b-lookalike-pad\n"
V = b"verified-unique-to-repo-a\n"
STRANGER = b"never seen anywhere\n"

assert len(Z) == len(W) and Z != W


def digest(algorithm: str, data: bytes) -> str:
    return hashlib.new(algorithm, data).hexdigest()


def write_artifacts() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    (ARTIFACTS / "x.bin").write_bytes(X)
    (ARTIFACTS / "y.bin").write_bytes(Y)
    (ARTIFACTS / "v.bin").write_bytes(V)


def write_inventory(path: Path, rows: list[tuple[str, str, str, int, str]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["artifact", "algorithm", "digest", "size", "path"])
        writer.writerows(rows)


def main() -> None:
    write_artifacts()
    write_inventory(
        HERE / "repo_a.csv",
        [
            ("a_core", "sha256", digest("sha256", X), len(X), "x.bin"),
            ("a_mirror", "sha256", digest("sha256", X), len(X), ""),
            ("a_lonely", "sha256", digest("sha256", Z), len(Z), ""),
            ("a_unique", "sha256", digest("sha256", V), len(V), "v.bin"),
            ("a_stale", "sha256", digest("sha256", Z), len(Z), "y.bin"),
        ],
    )
    write_inventory(
        HERE / "repo_b.csv",
        [
            ("b_core", "sha512", digest("sha512", X), len(X), ""),
            ("b_orphan", "sha512", digest("sha512", Y), len(Y), ""),
            ("b_lookalike", "sha512", digest("sha512", W), len(W), ""),
            ("b_stranger", "sha512", digest("sha512", STRANGER), len(STRANGER), ""),
        ],
    )
    print(f"demo dataset written to {HERE}")


if __name__ == "__main__":
    main()
