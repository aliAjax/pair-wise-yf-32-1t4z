import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, OrganAllocationService, iso, utcnow


class OrganFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, expires_days=2):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(days=3)), "expires_at": iso(self.now + timedelta(days=expires_days)), "clinical_match": 8})

    def candidate(self, name="患者甲", hospital="H2", urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9})

    def test_complete_allocation_and_cold_chain_flow(self):
        donor, candidate = self.donor(), self.candidate()
        rank = self.svc.ranking(donor["id"], "allocation_officer", "")
        self.assertEqual(rank["candidates"][0]["id"], candidate["id"])
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(accepted["status"], "accepted")
        transit = self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 3.5})
        self.assertEqual(transit["status"], "in_transit")
        handoff = self.svc.initiate_handoff(allocation["id"], "hospital-h1", "hospital", "H1", {"expected_revision": transit["revision"], "to_hospital": "H2", "cold_chain_temp": 3.0})
        self.assertEqual(handoff["handoff"]["status"], "initiated")
        received = self.svc.accept_handoff(allocation["id"], "hospital-h2", "hospital", "H2", {})
        self.assertEqual(received["status"], "handed_off")
        implanted = self.svc.implant(allocation["id"], "allocator", "allocation_officer", {})
        self.assertEqual(implanted["status"], "implanted")
        audit = self.svc.audit(allocation["id"], "auditor")
        self.assertEqual([item["action"] for item in audit], ["allocation_proposed", "allocation_accepted", "transfer_started", "handoff_initiated", "handoff_accepted", "organ_implanted"])

    def test_expiry_privacy_and_single_allocation(self):
        expired = self.donor(expires_days=-1); candidate = self.candidate()
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": expired["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "organ_expired")
        donor2 = self.donor(); allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "wrong", "hospital", "H1", {"expected_revision": 1})
        self.assertEqual(ctx.exception.status, 403)
        masked = self.svc.get_allocation(allocation["id"], "hospital", "H1")
        self.assertEqual(masked["patient_name"], "***")
        with self.assertRaises(ApiError) as ctx:
            self.svc.propose("allocator", "allocation_officer", {"donor_id": donor2["id"], "candidate_id": candidate["id"]})
        self.assertEqual(ctx.exception.code, "donor_unavailable")
        other = self.candidate("患者乙", "H2", 4, 300)
        self.assertNotEqual(other["id"], candidate["id"])
        with self.assertRaises(ApiError) as ctx:
            self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 12})
        self.assertEqual(ctx.exception.code, "cold_chain_violation")


class ReassignFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = OrganAllocationService(Path(self.tmp.name) / "test.db"); self.now = utcnow()

    def tearDown(self): self.tmp.cleanup()

    def donor(self, hours=3):
        return self.svc.register_donor("coord", "coordinator", {"blood_type": "O", "organ": "kidney", "hospital": "H1", "region": "East", "available_at": iso(self.now - timedelta(hours=1)), "expires_at": iso(self.now + timedelta(hours=hours)), "clinical_match": 8})

    def candidate(self, name, hospital, transit_minutes, urgency=5, wait=500):
        return self.svc.register_candidate("coord", "coordinator", {"patient_name": name, "blood_type": "B", "organ": "kidney", "hospital": hospital, "region": "East", "urgency": urgency, "wait_days": wait, "willing": True, "clinical_match": 9, "transit_minutes": transit_minutes})

    def test_eta_screen_withdraw_and_reassign(self):
        donor = self.donor(hours=3)
        first, second = self.candidate("患者甲", "H2", 300), self.candidate("患者乙", "H3", 45, urgency=4, wait=300)
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        accepted = self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        eta = self.svc.report_eta(allocation["id"], "hospital-h2", "hospital", "H2", {"eta_at": iso(self.now + timedelta(hours=2, minutes=30))})
        self.assertEqual(eta["eta_reported_by"], "H2")
        self.assertGreater(eta["revision"], accepted["revision"])
        self.svc.report_delay(allocation["id"], "allocator", "allocation_officer", {"delayed_minutes": 90, "reason": "冷链车高速晚点"})

        options = self.svc.reassign_options(allocation["id"], "coordinator")
        self.assertLessEqual(options["remaining_minutes"], 180)
        by_id = {item["id"]: item for item in options["candidates"]}
        self.assertFalse(by_id[first["id"]]["feasible"])
        self.assertTrue(by_id[first["id"]]["current"])
        self.assertTrue(by_id[second["id"]]["feasible"])
        self.assertLess(options["candidates"].index(by_id[second["id"]]), options["candidates"].index(by_id[first["id"]]))

        withdrawn = self.svc.withdraw(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "预计抵达过晚，原患者来不及接收"})
        self.assertEqual(withdrawn["status"], "withdrawn")
        reassigned = self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": second["id"], "reason": "冷链晚点，改派给转运 45 分钟可达的患者乙"})
        self.assertEqual(reassigned["status"], "proposed")
        self.assertEqual(reassigned["candidate_id"], second["id"])
        self.assertEqual(reassigned["donor_status"], "allocated")
        self.assertIsNone(reassigned["eta_at"])
        self.assertEqual(len(reassigned["reassignments"]), 1)
        self.assertEqual(reassigned["reassignments"][0]["from_candidate_id"], first["id"])
        self.assertEqual(reassigned["reassignments"][0]["reason"], "冷链晚点，改派给转运 45 分钟可达的患者乙")

        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": accepted["revision"]})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "allocation_reassigned")

        confirmed = self.svc.accept(allocation["id"], "hospital-h3", "hospital", "H3", {"expected_revision": reassigned["revision"]})
        self.assertEqual(confirmed["status"], "accepted")
        actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(actions, ["allocation_proposed", "allocation_accepted", "eta_reported", "logistics_delay", "allocation_withdrawn", "allocation_reassigned", "allocation_accepted"])

    def test_reassign_guards(self):
        donor = self.donor(hours=3)
        first, far = self.candidate("患者甲", "H2", 30), self.candidate("患者丙", "H4", 400)
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "coord", "coordinator", {"candidate_id": far["id"], "reason": "x"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": far["id"], "reason": " "})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": first["id"], "reason": "x"})
        self.assertEqual(ctx.exception.code, "same_candidate")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": far["id"], "reason": "太远"})
        self.assertEqual(ctx.exception.code, "transit_window_exceeded")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_options(allocation["id"], "hospital")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.report_eta(allocation["id"], "outsider", "hospital", "H9", {"eta_at": iso(self.now + timedelta(hours=1))})
        self.assertEqual(ctx.exception.code, "wrong_hospital")
        self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.mark_transit(allocation["id"], "allocator", "allocation_officer", {"cold_chain_temp": 4.0})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": far["id"], "reason": "转运中尝试改派"})
        self.assertEqual(ctx.exception.code, "invalid_transition")


if __name__ == "__main__": unittest.main()
