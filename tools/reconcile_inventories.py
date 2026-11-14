"""Reconcile digest inventories from two artifact repositories.

Each inventory is a CSV with one digest claim per artifact row:

    artifact,algorithm,digest,size,path

- ``algorithm`` is ``sha256`` or ``sha512``.
- ``digest`` is the lowercase hex digest the repository recorded.
- ``size`` is the recorded size in bytes (may be empty).
- ``path`` locates the artifact bytes relative to ``--artifacts-root``;
  an empty value means the original bytes are no longer readable.

Evidence rules
--------------
1. Same algorithm and same digest proves identical content.
2. Same algorithm and different digests proves different content.
3. A cross-algorithm relation (sha256 vs sha512) is established only
   through a record whose original bytes are still readable: recomputing
   both digests of the same bytes links the two algorithms' tokens.
4. Equal sizes plus two digest strings from different algorithms is NOT
   evidence of equivalence.
5. A stored claim that contradicts the actual bytes is quarantined: the
   artifact keeps its recomputed (verified) identity, but the stale claim
   is excluded from the equivalence graph so it cannot act as a bridge
   that wrongly merges the two inventories.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_ALGORITHMS = ("sha256", "sha512")
DIGEST_LENGTHS = {"sha256": 64, "sha512": 128}


@dataclass(frozen=True)
class Claim:
    repo: str
    artifact: str
    algorithm: str
    digest: str
    size: int | None
    path: Path | None

    @property
    def label(self) -> str:
        return f"{self.repo}/{self.artifact}"


@dataclass(frozen=True)
class Quarantine:
    claim: Claim
    actual_digest: str
    actual_size: int


class TokenSets:
    """Union-find over (algorithm, digest) tokens."""

    def __init__(self) -> None:
        self._parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(self, token: tuple[str, str]) -> tuple[str, str]:
        parent = self._parent
        root = token
        while parent.get(root, root) != root:
            root = parent[root]
        while parent.get(token, token) != token:
            parent[token], token = root, parent[token]
        return root

    def union(self, first: tuple[str, str], second: tuple[str, str]) -> None:
        first_root = self.find(first)
        second_root = self.find(second)
        if first_root != second_root:
            self._parent[second_root] = first_root


@dataclass
class Reconciliation:
    claims: list[Claim]
    verified: dict[str, dict[str, str]]
    quarantined: list[Quarantine]
    artifact_tokens: dict[str, set[tuple[str, str]]]
    token_sets: TokenSets
    component_tokens: dict[tuple[str, str], dict[str, set[str]]]
    groups: dict[tuple[str, str], set[str]]
    warnings: list[str] = field(default_factory=list)

    def root_of(self, label: str) -> tuple[str, str]:
        token = next(iter(self.artifact_tokens[label]))
        return self.token_sets.find(token)


def short(digest: str) -> str:
    return f"{digest[:12]}…"


def load_inventory(csv_path: Path, artifacts_root: Path) -> list[Claim]:
    claims = []
    with csv_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"artifact", "algorithm", "digest", "size", "path"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{csv_path}: missing columns: {sorted(missing)}")
        for row_number, row in enumerate(reader, start=2):
            algorithm = (row["algorithm"] or "").strip().lower()
            if algorithm not in SUPPORTED_ALGORITHMS:
                raise SystemExit(
                    f"{csv_path}:{row_number}: unsupported algorithm {algorithm!r}"
                )
            digest = (row["digest"] or "").strip().lower()
            if len(digest) != DIGEST_LENGTHS[algorithm] or any(
                char not in "0123456789abcdef" for char in digest
            ):
                raise SystemExit(
                    f"{csv_path}:{row_number}: malformed {algorithm} digest {digest!r}"
                )
            size_text = (row["size"] or "").strip()
            path_text = (row["path"] or "").strip()
            claims.append(
                Claim(
                    repo=csv_path.stem,
                    artifact=(row["artifact"] or "").strip(),
                    algorithm=algorithm,
                    digest=digest,
                    size=int(size_text) if size_text else None,
                    path=(artifacts_root / path_text) if path_text else None,
                )
            )
    return claims


def reconcile(claims: list[Claim]) -> Reconciliation:
    token_sets = TokenSets()
    verified: dict[str, dict[str, str]] = {}
    quarantined: list[Quarantine] = []
    warnings: list[str] = []

    for claim in claims:
        if claim.path is None:
            continue
        data = claim.path.read_bytes()
        digests = {
            algo: hashlib.new(algo, data).hexdigest() for algo in SUPPORTED_ALGORITHMS
        }
        verified[claim.label] = digests
        if claim.digest != digests[claim.algorithm]:
            quarantined.append(Quarantine(claim, digests[claim.algorithm], len(data)))
        if claim.size is not None and claim.size != len(data):
            warnings.append(
                f"{claim.label}: recorded size {claim.size} does not match"
                f" actual size {len(data)}"
            )

    for digests in verified.values():
        token_sets.union(
            ("sha256", digests["sha256"]), ("sha512", digests["sha512"])
        )

    quarantined_labels = {entry.claim.label for entry in quarantined}
    artifact_tokens: dict[str, set[tuple[str, str]]] = {}
    for claim in claims:
        tokens = artifact_tokens.setdefault(claim.label, set())
        if claim.label in verified:
            tokens.update((algo, digest) for algo, digest in verified[claim.label].items())
        if claim.label not in quarantined_labels:
            tokens.add((claim.algorithm, claim.digest))

    groups: dict[tuple[str, str], set[str]] = {}
    component_tokens: dict[tuple[str, str], dict[str, set[str]]] = {}
    for label, tokens in artifact_tokens.items():
        roots = {token_sets.find(token) for token in tokens}
        if len(roots) != 1:
            warnings.append(f"{label}: tokens span multiple components: {sorted(roots)}")
        root = next(iter(roots))
        groups.setdefault(root, set()).add(label)
        bucket = component_tokens.setdefault(
            root, {algo: set() for algo in SUPPORTED_ALGORITHMS}
        )
        for algo, digest in tokens:
            bucket[algo].add(digest)

    for root, bucket in component_tokens.items():
        for algo, digests in bucket.items():
            if len(digests) > 1:
                warnings.append(
                    f"component {sorted(groups[root])}: conflicting {algo}"
                    f" digests merged: {sorted(digests)}"
                )

    return Reconciliation(
        claims=claims,
        verified=verified,
        quarantined=quarantined,
        artifact_tokens=artifact_tokens,
        token_sets=token_sets,
        component_tokens=component_tokens,
        groups=groups,
        warnings=warnings,
    )


def classify_cross_repo_pairs(
    recon: Reconciliation, repos: tuple[str, str]
) -> dict[str, list]:
    labels_by_repo = {
        repo: sorted(
            label for label in recon.artifact_tokens if label.startswith(f"{repo}/")
        )
        for repo in repos
    }
    result: dict[str, list] = {"same": [], "different": [], "insufficient": []}
    for label_a in labels_by_repo[repos[0]]:
        for label_b in labels_by_repo[repos[1]]:
            root_a = recon.root_of(label_a)
            root_b = recon.root_of(label_b)
            if root_a == root_b:
                result["same"].append((label_a, label_b))
                continue
            common = [
                algo
                for algo in SUPPORTED_ALGORITHMS
                if recon.component_tokens[root_a][algo]
                and recon.component_tokens[root_b][algo]
            ]
            if common:
                algo = common[0]
                digest_a = next(iter(recon.component_tokens[root_a][algo]))
                digest_b = next(iter(recon.component_tokens[root_b][algo]))
                result["different"].append((label_a, label_b, algo, digest_a, digest_b))
            else:
                result["insufficient"].append((label_a, label_b))
    return result


def member_note(recon: Reconciliation, claim: Claim) -> str:
    quarantined_labels = {entry.claim.label for entry in recon.quarantined}
    if claim.label in quarantined_labels:
        return f"verified bytes; stale {claim.algorithm} claim quarantined"
    if claim.label in recon.verified:
        return "verified bytes (sha256+sha512 recomputed, claim consistent)"
    return f"{claim.algorithm} claim only (original bytes unreadable)"


def render(recon: Reconciliation, repos: tuple[str, str]) -> str:
    claims_by_label = {claim.label: claim for claim in recon.claims}
    lines: list[str] = []

    readable = len(recon.verified)
    lines.append(
        f"Loaded {len(recon.claims)} claims from {repos[0]} and {repos[1]};"
        f" {readable} artifacts still readable, {len(recon.quarantined)} claims quarantined."
    )

    lines.append("")
    lines.append("== Quarantined claims (conflict with actual bytes) ==")
    if not recon.quarantined:
        lines.append("  none")
    for entry in recon.quarantined:
        claim = entry.claim
        lines.append(
            f"  {claim.label}: recorded {claim.algorithm}={short(claim.digest)}"
            f" but actual bytes give {claim.algorithm}={short(entry.actual_digest)}"
            " — claim excluded from equivalence reasoning"
        )
        stale_token = (claim.algorithm, claim.digest)
        others = sorted(
            label
            for label, tokens in recon.artifact_tokens.items()
            if label != claim.label and stale_token in tokens
        )
        if others:
            own_group = sorted(recon.groups[recon.root_of(claim.label)])
            lines.append(
                f"    prevented bridge: the stale token is also claimed by {others};"
                f" trusting it would have merged {own_group} with those artifacts"
            )

    lines.append("")
    lines.append("== Provably identical content ==")
    multi = [members for members in recon.groups.values() if len(members) > 1]
    singles = [next(iter(m)) for m in recon.groups.values() if len(m) == 1]
    if not multi:
        lines.append("  none")
    for index, members in enumerate(sorted(multi, key=sorted), start=1):
        anchors = sorted(label for label in members if label in recon.verified)
        basis = (
            f"anchored by verified bytes of {', '.join(anchors)}"
            if anchors
            else "matching claims only, no readable original"
        )
        lines.append(f"  group {index} ({basis}):")
        for label in sorted(members):
            lines.append(f"    {label} — {member_note(recon, claims_by_label[label])}")
    if singles:
        lines.append(f"  singletons (no proven partner): {', '.join(sorted(singles))}")

    pairs = classify_cross_repo_pairs(recon, repos)

    lines.append("")
    lines.append("== Provably different (cross-repository) ==")
    if not pairs["different"]:
        lines.append("  none")
    for label_a, label_b, algo, digest_a, digest_b in pairs["different"]:
        lines.append(
            f"  {label_a} != {label_b} — {algo} differs:"
            f" {short(digest_a)} vs {short(digest_b)}"
        )

    lines.append("")
    lines.append("== Insufficient evidence (cross-repository) ==")
    if not pairs["insufficient"]:
        lines.append("  none")
    for label_a, label_b in pairs["insufficient"]:
        claim_a = claims_by_label[label_a]
        claim_b = claims_by_label[label_b]
        note = (
            f"only cross-algorithm claims ({claim_a.algorithm} vs"
            f" {claim_b.algorithm}) and no verified original links them"
        )
        if claim_a.size is not None and claim_b.size is not None:
            if claim_a.size == claim_b.size:
                note += (
                    f"; recorded sizes are equal ({claim_a.size} bytes) but size"
                    " equality is not evidence of identical content"
                )
            else:
                note += (
                    f"; recorded sizes differ ({claim_a.size} vs {claim_b.size})"
                )
        lines.append(f"  {label_a} ? {label_b} — {note}")

    if recon.warnings:
        lines.append("")
        lines.append("== Warnings ==")
        for warning in recon.warnings:
            lines.append(f"  {warning}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile sha256/sha512 digest inventories of two artifact"
        " repositories into proven-identical, proven-different and"
        " insufficient-evidence relations."
    )
    parser.add_argument("inventory_a", type=Path, help="CSV inventory of repository A")
    parser.add_argument("inventory_b", type=Path, help="CSV inventory of repository B")
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=Path("."),
        help="directory that the CSV path column is resolved against",
    )
    args = parser.parse_args(argv)

    claims = load_inventory(args.inventory_a, args.artifacts_root)
    claims += load_inventory(args.inventory_b, args.artifacts_root)
    recon = reconcile(claims)
    print(render(recon, (args.inventory_a.stem, args.inventory_b.stem)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
