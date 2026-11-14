import base64
import hashlib

import pytest

from installer.inventory import Inventory, InventoryError
from installer.records import Hash, RecordEntry


def make_entry(path, content=None):
    if content is None:
        return RecordEntry(path=path, hash_=None, size=None)
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    return RecordEntry(
        path=path,
        hash_=Hash("sha256", digest.decode("ascii").rstrip("=")),
        size=len(content),
    )


@pytest.fixture()
def inventory():
    return Inventory()


class TestClaims:
    def test_shared_path_has_multiple_holders(self, inventory):
        inventory.import_record("alpha", [make_entry("shared.py", b"same")])
        inventory.import_record("beta", [make_entry("shared.py", b"same")])

        assert inventory.holders("shared.py") == ("alpha", "beta")

    def test_remove_distribution_keeps_other_holders(self, inventory):
        inventory.import_record(
            "alpha", [make_entry("shared.py", b"same"), make_entry("alpha_only.py")]
        )
        inventory.import_record("beta", [make_entry("shared.py", b"same")])

        inventory.remove_distribution("alpha")

        assert inventory.holders("shared.py") == ("beta",)
        assert inventory.holders("alpha_only.py") == ()

    def test_upgrade_replaces_own_claims_only(self, inventory):
        inventory.import_record(
            "alpha", [make_entry("shared.py", b"old"), make_entry("stale.py")]
        )
        inventory.import_record("beta", [make_entry("shared.py", b"old")])

        inventory.import_record("alpha", [make_entry("shared.py", b"old")])

        assert inventory.holders("shared.py") == ("alpha", "beta")
        assert inventory.holders("stale.py") == ()

    def test_expected_content_is_exposed_per_holder(self, inventory):
        inventory.import_record("alpha", [make_entry("shared.py", b"one")])
        inventory.import_record("beta", [make_entry("shared.py", b"two")])

        status = inventory.snapshot()["shared.py"]

        assert status.holders == ("alpha", "beta")
        entries = [claim.entry for claim in status.claims]
        assert make_entry("shared.py", b"one") in entries
        assert make_entry("shared.py", b"two") in entries


class TestDisputes:
    def test_different_digests_are_disputed(self, inventory):
        inventory.import_record("alpha", [make_entry("shared.py", b"one")])
        inventory.import_record("beta", [make_entry("shared.py", b"two")])

        disputes = inventory.disputes()

        assert set(disputes) == {"shared.py"}
        assert make_entry("shared.py", b"one") in disputes["shared.py"]
        assert make_entry("shared.py", b"two") in disputes["shared.py"]
        assert inventory.snapshot()["shared.py"].disputed

    def test_same_digest_is_not_disputed(self, inventory):
        inventory.import_record("alpha", [make_entry("shared.py", b"same")])
        inventory.import_record("beta", [make_entry("shared.py", b"same")])

        assert inventory.disputes() == {}
        assert not inventory.snapshot()["shared.py"].disputed

    def test_hashless_claim_does_not_create_dispute(self, inventory):
        inventory.import_record("alpha", [make_entry("shared.py", b"one")])
        inventory.import_record("beta", [make_entry("shared.py")])

        assert inventory.disputes() == {}


class TestAudit:
    def test_audit_reports_reality(self, inventory, tmp_path):
        inventory.import_record(
            "alpha",
            [
                make_entry("ok.py", b"fine"),
                make_entry("changed.py", b"expected"),
                make_entry("gone.py", b"away"),
            ],
        )
        (tmp_path / "ok.py").write_bytes(b"fine")
        (tmp_path / "changed.py").write_bytes(b"tampered")

        report = inventory.audit(tmp_path)

        assert report["ok.py"].present
        assert report["ok.py"].mismatched == ()
        assert report["changed.py"].present
        assert report["changed.py"].mismatched == ("alpha",)
        assert not report["gone.py"].present

    def test_snapshot_does_not_touch_disk(self, inventory):
        inventory.import_record("alpha", [make_entry("ok.py", b"fine")])

        assert inventory.snapshot()["ok.py"].present is None

    def test_missing_lists_absent_claimed_paths(self, inventory, tmp_path):
        inventory.import_record(
            "alpha", [make_entry("here.py", b"fine"), make_entry("gone.py")]
        )
        (tmp_path / "here.py").write_bytes(b"fine")

        assert inventory.missing(tmp_path) == ("gone.py",)


class TestGenerations:
    def test_generation_increments_per_operation(self, inventory):
        assert inventory.generation == 0
        first = inventory.import_record("alpha", [make_entry("a.py")])
        second = inventory.remove_distribution("alpha")

        assert (first, second, inventory.generation) == (1, 2, 2)

    def test_queries_as_of_earlier_generation(self, inventory):
        inventory.import_record("alpha", [make_entry("shared.py", b"one")])
        before_beta = inventory.generation
        inventory.import_record("beta", [make_entry("shared.py", b"two")])

        assert inventory.holders("shared.py", generation=before_beta) == ("alpha",)
        assert inventory.disputes(generation=before_beta) == {}
        assert inventory.holders("shared.py") == ("alpha", "beta")
        assert set(inventory.disputes()) == {"shared.py"}

    def test_missing_as_of_earlier_generation(self, inventory, tmp_path):
        inventory.import_record("alpha", [make_entry("old.py")])
        before = inventory.generation
        inventory.import_record("beta", [make_entry("new.py")])

        assert inventory.missing(tmp_path, generation=before) == ("old.py",)
        assert inventory.missing(tmp_path) == ("new.py", "old.py")

    def test_future_generation_is_rejected(self, inventory):
        inventory.import_record("alpha", [make_entry("a.py")])

        with pytest.raises(InventoryError):
            inventory.snapshot(generation=inventory.generation + 1)


class TestRollback:
    def test_rollback_restores_previous_claims(self, inventory):
        inventory.import_record("alpha", [make_entry("a.py", b"v1")])
        inventory.import_record("beta", [make_entry("b.py", b"v1")])
        bad = inventory.import_record(
            "alpha", [make_entry("a.py", b"bad"), make_entry("extra.py")]
        )

        inventory.rollback(bad)

        status = inventory.snapshot()
        assert status["a.py"].claims[0].entry == make_entry("a.py", b"v1")
        assert "extra.py" not in status
        assert status["b.py"].holders == ("beta",)

    def test_rollback_of_removal_restores_claims(self, inventory):
        inventory.import_record("alpha", [make_entry("a.py", b"v1")])
        removal = inventory.remove_distribution("alpha")

        inventory.rollback(removal)

        assert inventory.holders("a.py") == ("alpha",)

    def test_history_is_not_rewritten(self, inventory, tmp_path):
        inventory.import_record("alpha", [make_entry("a.py", b"v1")])
        before_beta = inventory.generation
        inventory.import_record("beta", [make_entry("b.py", b"v1")])
        bad = inventory.import_record("alpha", [make_entry("a.py", b"bad")])

        owners_before = inventory.snapshot(generation=before_beta)
        missing_before = inventory.missing(tmp_path, generation=before_beta)

        inventory.rollback(bad)

        assert inventory.snapshot(generation=before_beta) == owners_before
        assert inventory.missing(tmp_path, generation=before_beta) == missing_before

    def test_unknown_generation_is_rejected(self, inventory):
        inventory.import_record("alpha", [make_entry("a.py")])

        with pytest.raises(InventoryError):
            inventory.rollback(inventory.generation + 1)
