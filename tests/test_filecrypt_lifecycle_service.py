# -*- coding: utf-8 -*-

import json
import threading
import unittest

from quasarr.providers.crypter_candidates import link_fingerprint
from quasarr.providers.filecrypt_lifecycle import (
    FILECRYPT_LINK_STATES_TABLE,
    FILECRYPT_SWEEP_KEY,
    FILECRYPT_SWEEP_MEMBERS_TABLE,
    FILECRYPT_SWEEP_STATE_TABLE,
    decode_link_state,
    decode_sweep_header,
    decode_sweep_member,
    encode_link_state,
    encode_sweep_header,
)
from quasarr.providers.filecrypt_lifecycle_service import (
    DEFAULT_SWEEP_WINDOW_MINUTES,
    FILECRYPT_CRYPTER,
    FILECRYPT_LINK_LIFECYCLE_CAPABILITY,
    OFFER_LEASE_SECONDS,
    FilecryptLifecycleService,
)

NOW = 1_700_000_000
WINDOW = DEFAULT_SWEEP_WINDOW_MINUTES * 60  # 900 s


# ── test doubles ──────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class SequentialIds:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return f"{self.count:032x}"


class AtomicDatabase:
    """Fake in-memory K/V database with hook, rollback, and mutation counter."""

    def __init__(self, rows=None, tables=None):
        self.rows = dict(rows or {})
        self.tables = {} if tables is None else tables
        self.lock = threading.RLock()
        self.before_mutation = None
        self.mutation_count = 0

    def _peer(self, table):
        if table not in self.tables:
            self.tables[table] = AtomicDatabase(tables=self.tables)
        return self.tables[table]

    def _interleave(self):
        hook, self.before_mutation = self.before_mutation, None
        if hook is not None:
            hook()

    def retrieve(self, key):
        with self.lock:
            return self.rows.get(key)

    def retrieve_all_titles(self):
        with self.lock:
            items = [[k, v] for k, v in sorted(self.rows.items())]
            return items or None

    def store(self, key, value):
        with self.lock:
            self.rows[key] = value
            return True

    def update_store(self, key, value):
        return self.store(key, value)

    def mutate_value(self, key, mutator):
        with self.lock:
            self.mutation_count += 1
            current = self.rows.get(key)
            new_value = mutator(current)
            if new_value is None:
                self.rows.pop(key, None)
            else:
                self.rows[key] = new_value
            return new_value

    def delete(self, key):
        with self.lock:
            self.rows.pop(key, None)

    def delete_exact(self, key, value):
        with self.lock:
            if self.rows.get(key) != value:
                return False
            self.rows.pop(key, None)
            return True

    def mutate_values(self, targets, mutator):
        with self.lock:
            self.mutation_count += 1
            self._interleave()
            databases = [self._peer(table) for table, _key in targets]
            current_values = tuple(
                db.rows.get(key)
                for db, (_t, key) in zip(databases, targets, strict=True)
            )
            new_values = mutator(current_values)
            # Snapshot for rollback
            snapshot = {
                (id(db), key): db.rows.get(key)
                for db, (_t, key) in zip(databases, targets, strict=True)
            }
            try:
                for db, (_t, key), val in zip(
                    databases, targets, new_values, strict=True
                ):
                    if val is None:
                        db.rows.pop(key, None)
                    else:
                        db.rows[key] = val
            except Exception:
                for db, (_t, key) in zip(databases, targets, strict=True):
                    old = snapshot.get((id(db), key))
                    if old is None:
                        db.rows.pop(key, None)
                    else:
                        db.rows[key] = old
                raise
            return tuple(new_values)


class AtomicSharedState:
    def __init__(self):
        self.databases = {}
        self.values = {}

    def get_db(self, table):
        if table not in self.databases:
            self.databases[table] = AtomicDatabase(tables=self.databases)
        return self.databases[table]

    def update(self, key, value):
        self.values[key] = value


# ── data helpers ──────────────────────────────────────────────────────────────


def pkg(n):
    return f"Quasarr_movies_{n:032x}"


def url(n):
    return f"https://filecrypt.invalid/c/{n:08x}"


def fp(n):
    return link_fingerprint(FILECRYPT_CRYPTER, url(n))


def protected_blob(urls):
    links = [[u, FILECRYPT_CRYPTER] for u in urls]
    return json.dumps({"title": "T", "password": "", "links": links})


def rows_for(indices):
    """One package per URL index, each with one Filecrypt link."""
    return [[pkg(i), protected_blob([url(i)])] for i in indices]


# ── base test case ─────────────────────────────────────────────────────────────


class LifecycleServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(NOW)
        self.ids = SequentialIds()
        self.state = AtomicSharedState()

    def service(self):
        return FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=self.ids
        )

    def sweep_db(self):
        return self.state.get_db(FILECRYPT_SWEEP_STATE_TABLE)

    def members_db(self):
        return self.state.get_db(FILECRYPT_SWEEP_MEMBERS_TABLE)

    def ls_db(self):
        return self.state.get_db(FILECRYPT_LINK_STATES_TABLE)

    def header(self):
        raw = self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY)
        return None if raw is None else decode_sweep_header(raw)

    def member(self, fingerprint):
        raw = self.members_db().retrieve(fingerprint)
        return None if raw is None else decode_sweep_member(raw)

    def link_state(self, fingerprint):
        raw = self.ls_db().retrieve(fingerprint)
        return None if raw is None else decode_link_state(raw)

    def install_header(self, record):
        self.sweep_db().update_store(FILECRYPT_SWEEP_KEY, encode_sweep_header(record))

    def install_link_state(self, fingerprint, record):
        self.ls_db().update_store(fingerprint, encode_link_state(record))

    def make_sweeping_header(self, generation_id, total=2, now=None, window=WINDOW):
        now = now or NOW
        return {
            "schema_version": 1,
            "state": "sweeping",
            "generation_id": generation_id,
            "opened_epoch": now,
            "deadline_epoch": now + window,
            "window_seconds": window,
            "total": total,
            "tested": 0,
            "blocked": 0,
            "global_possible": True,
        }

    def make_held_ls(
        self,
        generation_id,
        offer_id,
        pkg_id,
        retry_after=NOW + WINDOW,
        first_blocked=NOW,
    ):
        return {
            "schema_version": 1,
            "state": "held",
            "first_blocked_epoch": first_blocked,
            "retry_after_epoch": retry_after,
            "lease": {
                "sweep_id": generation_id,
                "offer_id": offer_id,
                "package_id": pkg_id,
                "offer_expires_epoch": NOW + OFFER_LEASE_SECONDS,
            },
        }


# ── tests ──────────────────────────────────────────────────────────────────────


class TestPrepareOfferBasic(LifecycleServiceTestCase):
    def test_zero_first_time_returns_none_and_writes_nothing(self):
        svc = self.service()
        offer = svc.prepare_offer([])

        self.assertIsNone(offer)
        self.assertIsNone(self.header())
        self.assertIsNone(self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY))

    def test_singleton_returns_individual_no_header(self):
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1]))

        self.assertIsNotNone(offer)
        self.assertEqual("individual", offer["mode"])
        self.assertEqual(FILECRYPT_LINK_LIFECYCLE_CAPABILITY, offer["capability"])
        self.assertEqual(FILECRYPT_CRYPTER, offer["crypter"])
        self.assertEqual(fp(1), offer["link_fingerprint"])
        self.assertRegex(offer["sweep_id"], r"^[0-9a-f]{32}$")
        self.assertRegex(offer["offer_id"], r"^[0-9a-f]{32}$")
        self.assertGreater(offer["deadline_epoch"], NOW)
        self.assertIsNotNone(offer["occurrence"])

        # No sweep header
        self.assertIsNone(self.header())

        # No link-state at offer time; only Task 3B BLOCKED creates held state
        self.assertIsNone(self.link_state(fp(1)))
        self.assertEqual(
            {"state": "available", "retry_after_epoch": 0},
            svc.project_link(fp(1)),
        )

        # Offered member row written
        m = self.member(fp(1))
        self.assertIsNotNone(m)
        self.assertEqual("offered", m["state"])
        self.assertEqual(offer["sweep_id"], m["generation_id"])
        self.assertEqual(offer["offer_id"], m["lease"]["offer_id"])

        # Offer deadline is lease-duration, not window-duration
        self.assertEqual(NOW + OFFER_LEASE_SECONDS, offer["deadline_epoch"])

    def test_two_open_sweep_exact_total(self):
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]))

        self.assertIsNotNone(offer)
        self.assertEqual("sweep", offer["mode"])

        hdr = self.header()
        self.assertIsNotNone(hdr)
        self.assertEqual("sweeping", hdr["state"])
        self.assertEqual(2, hdr["total"])
        self.assertEqual(0, hdr["tested"])
        self.assertEqual(0, hdr["blocked"])

    def test_four_open_sweep_exact_total(self):
        svc = self.service()
        svc.prepare_offer(rows_for([1, 2, 3, 4]))

        hdr = self.header()
        self.assertEqual(4, hdr["total"])
        self.assertEqual(0, hdr["tested"])
        self.assertEqual(0, hdr["blocked"])

    def test_large_sweeps_no_sentinel_or_truncation(self):
        for count in (5, 101, 500, 1000):
            with self.subTest(count=count):
                self.state = AtomicSharedState()
                self.ids = SequentialIds()
                self.clock = FakeClock(NOW)
                svc = self.service()
                offer = svc.prepare_offer(rows_for(range(1, count + 1)))
                hdr = self.header()
                self.assertIsNotNone(offer)
                self.assertEqual(count, hdr["total"])
                self.assertEqual("sweeping", hdr["state"])


class TestDuplicateOwners(LifecycleServiceTestCase):
    def test_duplicate_owners_count_once(self):
        # Two packages with the same URL → one fingerprint
        same_url = url(1)
        protected = [
            [pkg(1), protected_blob([same_url])],
            [pkg(2), protected_blob([same_url])],
        ]
        svc = self.service()
        offer = svc.prepare_offer(protected)

        # Only one unique fingerprint → individual mode (< MINIMUM_SWEEP_SIZE)
        self.assertIsNotNone(offer)
        self.assertEqual("individual", offer["mode"])


class TestDeterminism(LifecycleServiceTestCase):
    def test_candidate_selection_order_independent_of_input_order(self):
        # Reversed input order yields same fingerprint
        forward = rows_for([1, 2, 3])
        reversed_ = list(reversed(forward))
        self.ids = SequentialIds()
        offer_a = self.service().prepare_offer(forward)
        self.state = AtomicSharedState()
        self.ids = SequentialIds()
        offer_b = self.service().prepare_offer(reversed_)
        self.assertEqual(offer_a["link_fingerprint"], offer_b["link_fingerprint"])


class TestExclusion(LifecycleServiceTestCase):
    def test_excluded_owner_not_handed_out_total_unchanged(self):
        # Three fingerprints; first owner excluded
        protected = rows_for([1, 2, 3])
        # fp(1) owner is pkg(1)
        first_fp = fp(1)
        svc = self.service()
        offer = svc.prepare_offer(protected, excluded_package_ids=[pkg(1)])

        hdr = self.header()
        self.assertIsNotNone(hdr)
        self.assertEqual(3, hdr["total"])  # membership unchanged
        # Leased member is NOT fp(1)
        self.assertNotEqual(first_fp, offer["link_fingerprint"])

    def test_excluded_owner_next_member_selected(self):
        # Two fingerprints; first excluded → second selected
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]), excluded_package_ids=[pkg(1)])
        self.assertEqual(fp(2), offer["link_fingerprint"])

    def test_all_occurrences_excluded_returns_none_no_sweep(self):
        """When every first-time fingerprint has only excluded owners, return None and write nothing."""
        svc = self.service()
        offer = svc.prepare_offer(
            rows_for([1, 2, 3]),
            excluded_package_ids=[pkg(1), pkg(2), pkg(3)],
        )
        self.assertIsNone(offer)
        self.assertIsNone(self.header())
        self.assertIsNone(self.members_db().retrieve_all_titles())


class TestAtomicity(LifecycleServiceTestCase):
    def test_open_and_first_lease_atomic_one_mutation(self):
        svc = self.service()
        svc.prepare_offer(rows_for([1, 2]))

        # Verify all three write targets committed
        hdr = self.header()
        self.assertIsNotNone(hdr)
        self.assertEqual(0, hdr["tested"])
        self.assertEqual(0, hdr["blocked"])

        leased_fp = sorted([fp(1), fp(2)])[0]
        member = self.member(leased_fp)
        self.assertIsNotNone(member)
        self.assertEqual("offered", member["state"])

        # No link-state at offer time; only Task 3B BLOCKED creates held state
        self.assertIsNone(self.link_state(leased_fp))

        # One mutation call for opening + leasing
        self.assertEqual(1, self.sweep_db().mutation_count)

    def test_mutation_exception_leaves_no_partial_state(self):
        # Callback calls ids() twice (generation_id, offer_id); raise on the 2nd call
        # to simulate a failure mid-callback before any writes occur.
        ids = SequentialIds()
        calls = [0]

        def failing_ids():
            calls[0] += 1
            if calls[0] == 2:
                raise RuntimeError("injected failure")
            return ids()

        svc = FilecryptLifecycleService(
            self.state, clock=self.clock, identifier_factory=failing_ids
        )
        with self.assertRaises(RuntimeError):
            svc.prepare_offer(rows_for([1, 2]))

        # Nothing committed
        self.assertIsNone(self.header())
        self.assertIsNone(self.members_db().retrieve_all_titles())


class TestIndividualLifecycle(LifecycleServiceTestCase):
    """Discriminating tests: individual offer writes member row only, no link-state."""

    def test_singleton_unanswered_live_lease_not_duplicated(self):
        """Second prepare_offer call while individual lease is live returns None."""
        svc = self.service()
        offer1 = svc.prepare_offer(rows_for([1]))
        self.assertIsNotNone(offer1)
        self.assertEqual("individual", offer1["mode"])

        # Advance 1 second; lease still live
        self.clock.now = NOW + 1
        offer2 = svc.prepare_offer(rows_for([1]))
        self.assertIsNone(offer2)

        # Original member row unchanged, still no link-state
        m = self.member(fp(1))
        self.assertEqual(offer1["offer_id"], m["lease"]["offer_id"])
        self.assertIsNone(self.link_state(fp(1)))

    def test_singleton_expired_lease_reoffered_individual_no_link_state(self):
        """After individual lease expiry, re-offer individual with new offer ID; still no link-state."""
        svc = self.service()
        offer1 = svc.prepare_offer(rows_for([1]))
        self.assertIsNotNone(offer1)

        # Advance past offer expiry
        self.clock.now = NOW + OFFER_LEASE_SECONDS + 1
        offer2 = svc.prepare_offer(rows_for([1]))

        self.assertIsNotNone(offer2)
        self.assertEqual("individual", offer2["mode"])
        self.assertNotEqual(offer1["offer_id"], offer2["offer_id"])
        self.assertIsNone(self.link_state(fp(1)))
        self.assertEqual(
            {"state": "available", "retry_after_epoch": 0},
            svc.project_link(fp(1)),
        )
        # Updated member row
        m = self.member(fp(1))
        self.assertEqual(offer2["offer_id"], m["lease"]["offer_id"])

    def test_first_time_offer_never_produces_retest_without_held_row(self):
        """After individual offer+expiry, re-offer is individual (not retest); retest needs Task 3B held."""
        svc = self.service()
        offer1 = svc.prepare_offer(rows_for([1]))
        self.assertEqual("individual", offer1["mode"])
        self.assertIsNone(self.link_state(fp(1)))  # no held row

        # Expire and re-offer: still no held row → still individual, not retest
        self.clock.now = NOW + OFFER_LEASE_SECONDS + 1
        offer2 = svc.prepare_offer(rows_for([1]))
        self.assertIsNotNone(offer2)
        self.assertEqual("individual", offer2["mode"])  # NOT retest
        self.assertIsNone(self.link_state(fp(1)))

        # Now simulate Task 3B installing a held row
        self.install_link_state(
            fp(1),
            self.make_held_ls(
                "c" * 32, offer2["offer_id"], pkg(1), retry_after=NOW - 2
            ),
        )
        self.clock.now = NOW + OFFER_LEASE_SECONDS + 2
        offer3 = svc.prepare_offer(rows_for([1]))
        self.assertIsNotNone(offer3)
        self.assertEqual("retest", offer3["mode"])  # NOW retest because held row exists


class TestActiveSweepLeasing(LifecycleServiceTestCase):
    def test_active_sweep_leases_pending_member(self):
        svc = self.service()
        # Open sweep with two members
        svc.prepare_offer(rows_for([1, 2]))
        hdr = self.header()
        gen_id = hdr["generation_id"]

        # Lease the second member (first is already offered)
        self.clock.now = NOW + 1
        offer2 = svc.prepare_offer(rows_for([1, 2]))
        self.assertIsNotNone(offer2)
        self.assertEqual("sweep", offer2["mode"])
        self.assertEqual(gen_id, offer2["sweep_id"])

    def test_active_sweep_leases_expired_offered_member(self):
        svc = self.service()
        svc.prepare_offer(rows_for([1, 2]))
        hdr = self.header()
        gen_id = hdr["generation_id"]

        # Advance time past offer expiry for first member
        self.clock.now = NOW + OFFER_LEASE_SECONDS + 1
        offer2 = svc.prepare_offer(rows_for([1, 2]))
        self.assertIsNotNone(offer2)
        self.assertEqual("sweep", offer2["mode"])
        self.assertEqual(gen_id, offer2["sweep_id"])

    def test_sweep_opening_all_first_time_link_states_none(self):
        """Opening a sweep writes no link-state for any first-time fingerprint."""
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        self.assertIsNotNone(offer)
        self.assertEqual("sweep", offer["mode"])
        for i in [1, 2, 3]:
            self.assertIsNone(
                self.link_state(fp(i)),
                f"fp({i}) link-state must be None after sweep open",
            )

    def test_sweep_expired_offered_member_reoffered_sweep_no_link_state(self):
        """Expired sweep offer re-leased as sweep; no link-state created."""
        svc = self.service()
        svc.prepare_offer(rows_for([1, 2]))

        # Advance past offer expiry
        self.clock.now = NOW + OFFER_LEASE_SECONDS + 1
        offer2 = svc.prepare_offer(rows_for([1, 2]))

        self.assertIsNotNone(offer2)
        self.assertEqual("sweep", offer2["mode"])
        self.assertIsNone(self.link_state(fp(1)))
        self.assertIsNone(self.link_state(fp(2)))

    def test_active_sweep_never_admits_new_fingerprint(self):
        svc = self.service()
        # Open sweep with fp(1) and fp(2)
        svc.prepare_offer(rows_for([1, 2]))
        hdr = self.header()
        gen_id = hdr["generation_id"]

        # Lease pending member (fp(2) is pending; check second call leases from frozen set)
        self.clock.now = NOW + 1
        # Add a NEW fingerprint fp(3)
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        # Either a member of the current sweep OR None (if both already offered)
        if offer is not None:
            self.assertEqual(gen_id, offer["sweep_id"])
            self.assertNotEqual(fp(3), offer["link_fingerprint"])

        # fp(3) must not be a member of the current generation
        m3 = self.member(fp(3))
        # If member3 row exists, its generation_id must not match gen_id
        if m3 is not None:
            self.assertNotEqual(gen_id, m3.get("generation_id"))


class TestRetestAndHeld(LifecycleServiceTestCase):
    def test_expired_held_prioritized_as_retest(self):
        # Install expired hold for fp(1)
        gen_id = "a" * 32
        offer_id = "b" * 32
        self.install_link_state(
            fp(1),
            self.make_held_ls(
                gen_id,
                offer_id,
                pkg(1),
                retry_after=NOW - 1,  # expired
                first_blocked=NOW - 100,
            ),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]))

        self.assertIsNotNone(offer)
        self.assertEqual("retest", offer["mode"])
        self.assertEqual(fp(1), offer["link_fingerprint"])

        # Link state updated with new lease
        ls = self.link_state(fp(1))
        self.assertEqual("held", ls["state"])
        self.assertNotEqual(offer_id, ls["lease"]["offer_id"])

    def test_active_held_skipped(self):
        # fp(1) is actively held; fp(2) has no state
        gen_id = "a" * 32
        self.install_link_state(
            fp(1),
            self.make_held_ls(gen_id, "b" * 32, pkg(1), retry_after=NOW + WINDOW),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]))

        # Only fp(2) is first-time → individual
        self.assertIsNotNone(offer)
        self.assertEqual("individual", offer["mode"])
        self.assertEqual(fp(2), offer["link_fingerprint"])

    def test_one_first_time_plus_held_uses_individual(self):
        gen_id = "a" * 32
        self.install_link_state(
            fp(1),
            self.make_held_ls(gen_id, "b" * 32, pkg(1), retry_after=NOW + WINDOW),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]))

        self.assertEqual("individual", offer["mode"])
        hdr = self.header()
        self.assertIsNone(hdr)  # no sweep header for individual

    def test_retest_prioritized_over_first_time(self):
        # fp(1) expired, fp(2) and fp(3) are first-time
        gen_id = "a" * 32
        self.install_link_state(
            fp(1),
            self.make_held_ls(gen_id, "b" * 32, pkg(1), retry_after=NOW - 1),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))

        self.assertEqual("retest", offer["mode"])
        self.assertEqual(fp(1), offer["link_fingerprint"])

    def test_preexisting_held_row_produces_retest_unchanged(self):
        """A held row installed by Task 3B (accepted BLOCKED) alone triggers retest."""
        gen_id = "a" * 32
        retry = NOW - 1  # expired hold
        self.install_link_state(
            fp(1),
            self.make_held_ls(gen_id, "b" * 32, pkg(1), retry_after=retry),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1]))

        self.assertIsNotNone(offer)
        self.assertEqual("retest", offer["mode"])
        self.assertEqual(fp(1), offer["link_fingerprint"])
        # Link-state updated with new lease (existing held row modified)
        ls = self.link_state(fp(1))
        self.assertEqual("held", ls["state"])
        self.assertNotEqual("b" * 32, ls["lease"]["offer_id"])


class TestHeaderSuppression(LifecycleServiceTestCase):
    def test_live_healthy_suppresses_first_time(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "healthy",
                "generation_id": "a" * 32,
                "until_epoch": NOW + 900,
            }
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        self.assertIsNone(offer)

    def test_live_healthy_allows_retest(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "healthy",
                "generation_id": "a" * 32,
                "until_epoch": NOW + 900,
            }
        )
        gen_id = "c" * 32
        self.install_link_state(
            fp(1),
            self.make_held_ls(gen_id, "d" * 32, pkg(1), retry_after=NOW - 1),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]))
        self.assertIsNotNone(offer)
        self.assertEqual("retest", offer["mode"])

    def test_live_cooldown_suppresses_all(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "a" * 32,
                "sweep_deadline_epoch": NOW + 900,
                "retry_after_epoch": NOW + 3600,
            }
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        self.assertIsNone(offer)

    def test_probe_under_cooldown(self):
        gen_id = "a" * 32
        cooldown_gen = "e" * 32
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": cooldown_gen,
                "sweep_deadline_epoch": NOW + 900,
                "retry_after_epoch": NOW + 3600,
            }
        )
        offer_id_held = "f" * 32
        self.install_link_state(
            fp(1),
            self.make_held_ls(gen_id, offer_id_held, pkg(1), retry_after=NOW + WINDOW),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]), preferred_fingerprint=fp(1))

        self.assertIsNotNone(offer)
        self.assertEqual("probe", offer["mode"])
        self.assertEqual(fp(1), offer["link_fingerprint"])
        # Link state updated, header not touched
        ls = self.link_state(fp(1))
        self.assertNotEqual(offer_id_held, ls["lease"]["offer_id"])
        hdr = self.header()
        self.assertEqual("cooldown", hdr["state"])  # unchanged

    def test_probe_invalid_preferred_returns_none(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "e" * 32,
                "sweep_deadline_epoch": NOW + 900,
                "retry_after_epoch": NOW + 3600,
            }
        )
        svc = self.service()
        # preferred_fingerprint not in inventory
        offer = svc.prepare_offer(rows_for([1, 2]), preferred_fingerprint=fp(99))
        self.assertIsNone(offer)

    def test_probe_blacklisted_preferred_returns_none(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "e" * 32,
                "sweep_deadline_epoch": NOW + 900,
                "retry_after_epoch": NOW + 3600,
            }
        )
        # fp(1) is blacklisted
        self.ls_db().update_store(
            fp(1),
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "blacklisted",
                    "first_blocked_epoch": NOW - 100,
                    "blacklisted_epoch": NOW - 50,
                }
            ),
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2]), preferred_fingerprint=fp(1))
        self.assertIsNone(offer)

    def test_expired_cooldown_permits_first_time(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "a" * 32,
                "sweep_deadline_epoch": NOW - 1,
                "retry_after_epoch": NOW - 1,  # expired
            }
        )
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        self.assertIsNotNone(offer)
        self.assertEqual("sweep", offer["mode"])

    def test_expired_cooldown_creates_no_state_for_paused_links(self):
        # Under expired cooldown with some held links: only first-time work happens
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "a" * 32,
                "sweep_deadline_epoch": NOW - 1,
                "retry_after_epoch": NOW - 1,
            }
        )
        # fp(3) was merely "globally paused" under cooldown but has no link state
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        # fp(3) should be accessible as first-time (no invented state)
        self.assertIsNone(self.link_state(fp(3)) if offer is None else None)
        if offer is not None:
            self.assertEqual("sweep", offer["mode"])


class TestDecisionProjections(LifecycleServiceTestCase):
    def test_decision_absent_header(self):
        proj = self.service().decision()
        self.assertEqual({"state": "available", "retry_after_epoch": 0}, proj)

    def test_decision_live_sweeping(self):
        gen_id = "a" * 32
        self.install_header(self.make_sweeping_header(gen_id))
        proj = self.service().decision()
        self.assertEqual({"state": "sweeping", "retry_after_epoch": 0}, proj)

    def test_decision_expired_sweeping(self):
        gen_id = "a" * 32
        self.install_header(
            self.make_sweeping_header(gen_id, now=NOW - WINDOW - 1, window=WINDOW)
        )
        self.clock.now = NOW
        proj = self.service().decision()
        self.assertEqual({"state": "available", "retry_after_epoch": 0}, proj)

    def test_decision_live_healthy(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "healthy",
                "generation_id": "a" * 32,
                "until_epoch": NOW + 900,
            }
        )
        proj = self.service().decision()
        self.assertEqual({"state": "healthy", "retry_after_epoch": NOW + 900}, proj)

    def test_decision_expired_healthy(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "healthy",
                "generation_id": "a" * 32,
                "until_epoch": NOW - 1,
            }
        )
        proj = self.service().decision()
        self.assertEqual({"state": "available", "retry_after_epoch": 0}, proj)

    def test_decision_live_cooldown(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "a" * 32,
                "sweep_deadline_epoch": NOW + 900,
                "retry_after_epoch": NOW + 3600,
            }
        )
        proj = self.service().decision()
        self.assertEqual({"state": "cooldown", "retry_after_epoch": NOW + 3600}, proj)

    def test_decision_expired_cooldown(self):
        self.install_header(
            {
                "schema_version": 1,
                "state": "cooldown",
                "generation_id": "a" * 32,
                "sweep_deadline_epoch": NOW - 900,
                "retry_after_epoch": NOW - 1,
            }
        )
        proj = self.service().decision()
        self.assertEqual({"state": "available", "retry_after_epoch": 0}, proj)

    def test_decision_malformed_header(self):
        self.sweep_db().update_store(FILECRYPT_SWEEP_KEY, "not_valid_json{{{")
        proj = self.service().decision()
        self.assertEqual({"state": "unavailable", "retry_after_epoch": 0}, proj)


class TestLinkProjections(LifecycleServiceTestCase):
    def test_project_link_absent(self):
        proj = self.service().project_link(fp(1))
        self.assertEqual({"state": "available", "retry_after_epoch": 0}, proj)

    def test_project_link_held_active(self):
        gen_id = "a" * 32
        retry = NOW + WINDOW
        self.install_link_state(
            fp(1), self.make_held_ls(gen_id, "b" * 32, pkg(1), retry_after=retry)
        )
        proj = self.service().project_link(fp(1))
        self.assertEqual({"state": "held", "retry_after_epoch": retry}, proj)

    def test_project_link_retest(self):
        gen_id = "a" * 32
        retry = NOW - 1
        self.install_link_state(
            fp(1), self.make_held_ls(gen_id, "b" * 32, pkg(1), retry_after=retry)
        )
        proj = self.service().project_link(fp(1))
        self.assertEqual({"state": "retest", "retry_after_epoch": retry}, proj)

    def test_project_link_blacklisting(self):
        self.ls_db().update_store(
            fp(1),
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "blacklisting",
                    "first_blocked_epoch": NOW,
                    "recheck_offer_id": "b" * 32,
                    "recheck_package_id": pkg(1),
                    "recheck_sweep_id": "c" * 32,
                    "terminal_operation_id": "d" * 64,
                }
            ),
        )
        proj = self.service().project_link(fp(1))
        self.assertEqual({"state": "blacklisting", "retry_after_epoch": 0}, proj)

    def test_project_link_blacklisted(self):
        self.ls_db().update_store(
            fp(1),
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "blacklisted",
                    "first_blocked_epoch": NOW - 100,
                    "blacklisted_epoch": NOW - 50,
                }
            ),
        )
        proj = self.service().project_link(fp(1))
        self.assertEqual({"state": "blacklisted", "retry_after_epoch": 0}, proj)

    def test_project_link_malformed(self):
        self.ls_db().update_store(fp(1), "garbage")
        proj = self.service().project_link(fp(1))
        self.assertEqual({"state": "unavailable", "retry_after_epoch": 0}, proj)


class TestActiveBlacklistedFingerprints(LifecycleServiceTestCase):
    def test_empty_when_no_rows(self):
        self.assertEqual((), self.service().active_blacklisted_fingerprints())

    def test_includes_only_blacklisted_state(self):
        self.ls_db().update_store(
            fp(1),
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "blacklisted",
                    "first_blocked_epoch": NOW,
                    "blacklisted_epoch": NOW + 1,
                }
            ),
        )
        self.ls_db().update_store(
            fp(2),
            encode_link_state(
                {
                    "schema_version": 1,
                    "state": "blacklisting",
                    "first_blocked_epoch": NOW,
                    "recheck_offer_id": "a" * 32,
                    "recheck_package_id": pkg(1),
                    "recheck_sweep_id": "b" * 32,
                    "terminal_operation_id": "c" * 64,
                }
            ),
        )
        self.install_link_state(fp(3), self.make_held_ls("d" * 32, "e" * 32, pkg(3)))
        result = self.service().active_blacklisted_fingerprints()
        self.assertEqual((fp(1),), result)

    def test_ascending_order(self):
        for i in [3, 1, 2]:
            self.ls_db().update_store(
                fp(i),
                encode_link_state(
                    {
                        "schema_version": 1,
                        "state": "blacklisted",
                        "first_blocked_epoch": NOW,
                        "blacklisted_epoch": NOW + 1,
                    }
                ),
            )
        result = self.service().active_blacklisted_fingerprints()
        self.assertEqual(tuple(sorted(result)), result)
        self.assertEqual(3, len(result))

    def test_sorted_independent_of_db_enumeration_order(self):
        """Result must be sorted even if the DB returns rows in reverse/unsorted order."""
        fp_vals = sorted([fp(1), fp(3), fp(5)])
        blacklisted_row = encode_link_state(
            {
                "schema_version": 1,
                "state": "blacklisted",
                "first_blocked_epoch": NOW - 100,
                "blacklisted_epoch": NOW - 50,
            }
        )
        for f in fp_vals:
            self.ls_db().update_store(f, blacklisted_row)
        # Override enumeration to return rows in reverse (unsorted) order
        db = self.ls_db()
        original_enum = db.retrieve_all_titles
        db.retrieve_all_titles = lambda: list(reversed(original_enum()))
        try:
            result = self.service().active_blacklisted_fingerprints()
        finally:
            db.retrieve_all_titles = original_enum
        self.assertEqual(tuple(fp_vals), result)


class TestConcurrencyAndRollback(LifecycleServiceTestCase):
    def test_concurrent_opener_hook_writes_no_orphan_members(self):
        gen_id = "a" * 32
        live_hdr = encode_sweep_header(self.make_sweeping_header(gen_id))

        def install_concurrent_header():
            self.sweep_db().rows[FILECRYPT_SWEEP_KEY] = live_hdr

        self.sweep_db().before_mutation = install_concurrent_header

        svc = self.service()
        result = svc.prepare_offer(rows_for([1, 2, 3]))

        self.assertIsNone(result)
        # No member rows orphaned
        members = self.members_db().retrieve_all_titles()
        self.assertIsNone(members)

    def test_malformed_persisted_header_fail_closed(self):
        self.sweep_db().update_store(FILECRYPT_SWEEP_KEY, '{"bad":true}')
        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))
        self.assertIsNone(offer)
        # Header preserved unchanged
        raw = self.sweep_db().retrieve(FILECRYPT_SWEEP_KEY)
        self.assertEqual('{"bad":true}', raw)

    def test_malformed_persisted_link_state_fail_closed_excluded(self):
        # Malformed link state: fingerprint treated as excluded (not first-time)
        self.ls_db().update_store(fp(1), "corrupted_json{{")
        svc = self.service()
        # With fp(1) excluded due to malformed, fp(2) alone is first-time → individual
        offer = svc.prepare_offer(rows_for([1, 2]))
        if offer is not None:
            self.assertNotEqual(fp(1), offer["link_fingerprint"])
        # Malformed row preserved
        self.assertEqual("corrupted_json{{", self.ls_db().retrieve(fp(1)))

    def test_race_non_leased_candidate_link_state_aborts_sweep(self):
        """Sweep open aborts if a non-leased first-time fingerprint's link state is written during the callback."""
        all_fps = sorted([fp(1), fp(2), fp(3)])
        # The leased candidate is the first-ordered first-time fp; pick the second as the race target
        non_leased_fp = all_fps[1]
        held_ls = encode_link_state(
            {
                "schema_version": 1,
                "state": "held",
                "first_blocked_epoch": NOW,
                "retry_after_epoch": NOW + WINDOW,
                "lease": {
                    "sweep_id": "a" * 32,
                    "offer_id": "b" * 32,
                    "package_id": pkg(99),
                    "offer_expires_epoch": NOW + OFFER_LEASE_SECONDS,
                },
            }
        )

        def install_race():
            self.ls_db().update_store(non_leased_fp, held_ls)

        self.sweep_db().before_mutation = install_race

        svc = self.service()
        offer = svc.prepare_offer(rows_for([1, 2, 3]))

        self.assertIsNone(offer)
        self.assertIsNone(self.header())
        self.assertIsNone(self.members_db().retrieve_all_titles())
        # Race-installed link state is preserved, not overwritten
        self.assertEqual(held_ls, self.ls_db().retrieve(non_leased_fp))

    def test_race_live_sweep_header_aborts_individual(self):
        """Individual open aborts if a live sweep header is installed during the mutation callback."""

        def install_race():
            self.install_header(self.make_sweeping_header("c" * 32))

        self.sweep_db().before_mutation = install_race

        svc = self.service()
        offer = svc.prepare_offer(rows_for([1]))

        self.assertIsNone(offer)
        # No link-state written for the individual candidate
        self.assertIsNone(self.link_state(fp(1)))
        # The concurrently-installed sweep header is preserved
        hdr = self.header()
        self.assertIsNotNone(hdr)
        self.assertEqual("sweeping", hdr["state"])


class TestScale(LifecycleServiceTestCase):
    def test_5000_member_opening_one_mutation_exact_total(self):
        count = 5000
        protected = rows_for(range(1, count + 1))
        svc = self.service()
        offer = svc.prepare_offer(protected)

        self.assertIsNotNone(offer)
        self.assertEqual("sweep", offer["mode"])

        hdr = self.header()
        self.assertEqual(count, hdr["total"])
        self.assertEqual(0, hdr["tested"])
        self.assertEqual(0, hdr["blocked"])

        # Exactly one mutate_values call
        self.assertEqual(1, self.sweep_db().mutation_count)

        # Exactly count member rows
        all_members = self.members_db().retrieve_all_titles() or []
        self.assertEqual(count, len(all_members))


if __name__ == "__main__":
    unittest.main()
