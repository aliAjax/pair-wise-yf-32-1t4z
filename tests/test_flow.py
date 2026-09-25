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
    def test_pre_transit_reassignment_flow(self):
        donor = self.donor(expires_days=1)
        first = self.candidate("患者甲", "H2", 5, 500)
        second = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者乙", "blood_type": "B", "organ": "kidney", "hospital": "H3", "region": "East", "urgency": 4, "wait_days": 300, "willing": True, "clinical_match": 9, "prep_minutes": 90})
        too_slow = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者丙", "blood_type": "B", "organ": "kidney", "hospital": "H4", "region": "East", "urgency": 5, "wait_days": 600, "willing": True, "clinical_match": 9, "prep_minutes": 180})
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.svc.report_delay(allocation["id"], "allocator", "allocation_officer", {"delayed_minutes": 150, "reason": "冷链车晚点"})
        eta = iso(self.now + timedelta(hours=22))
        reported = self.svc.report_eta(allocation["id"], "hospital-h2", "hospital", "H2", {"eta": eta})
        self.assertEqual(reported["eta_at"], eta)
        options = self.svc.reassign_options(allocation["id"], "allocation_officer")
        self.assertTrue(119.9 < options["remaining_minutes"] < 120.1)
        self.assertEqual([c["id"] for c in options["candidates"]], [second["id"]])
        self.assertNotIn(too_slow["id"], [c["id"] for c in options["candidates"]])
        withdrawn = self.svc.withdraw(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "预计抵达过晚，原患者来不及接收"})
        self.assertEqual(withdrawn["status"], "withdrawn")
        moved = self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": second["id"], "reason": "冷链晚点，改派给赶得上的患者"})
        self.assertEqual((moved["status"], moved["candidate_id"], moved["original_candidate_id"]), ("proposed", second["id"], first["id"]))
        self.assertEqual(moved["reassignments"][0]["reason"], "冷链晚点，改派给赶得上的患者")
        self.assertEqual(moved["reassignments"][0]["to_patient_name"], "患者乙")
        with self.assertRaises(ApiError) as ctx:
            self.svc.accept(allocation["id"], "hospital-h2", "hospital", "H2", {"expected_revision": 1})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        accepted = self.svc.accept(allocation["id"], "hospital-h3", "hospital", "H3", {"expected_revision": moved["revision"]})
        self.assertEqual(accepted["status"], "accepted")
        actions = [item["action"] for item in self.svc.audit(allocation["id"], "auditor")]
        self.assertEqual(actions, ["allocation_proposed", "allocation_accepted", "logistics_delay", "eta_reported", "allocation_withdrawn", "allocation_reassigned", "allocation_accepted"])

    def test_reassign_window_enforced(self):
        donor = self.donor(expires_days=1)
        first = self.candidate("患者甲", "H2")
        second = self.svc.register_candidate("coord", "coordinator", {"patient_name": "患者乙", "blood_type": "B", "organ": "kidney", "hospital": "H3", "region": "East", "urgency": 4, "wait_days": 300, "willing": True, "clinical_match": 9, "prep_minutes": 300})
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": first["id"]})
        self.svc.report_eta(allocation["id"], "hospital-h1", "hospital", "H1", {"eta": iso(self.now + timedelta(hours=22))})
        self.assertEqual(self.svc.reassign_options(allocation["id"], "allocation_officer")["candidates"], [])
        self.svc.withdraw(allocation["id"], "hospital-h2", "hospital", "H2", {"reason": "来不及接收"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "allocator", "allocation_officer", {"candidate_id": second["id"], "reason": "尝试改派"})
        self.assertEqual(ctx.exception.code, "reassign_window_insufficient")

    def test_eta_reporting_permissions(self):
        donor, candidate = self.donor(), self.candidate()
        allocation = self.svc.propose("allocator", "allocation_officer", {"donor_id": donor["id"], "candidate_id": candidate["id"]})
        with self.assertRaises(ApiError) as ctx:
            self.svc.report_eta(allocation["id"], "coord", "coordinator", "", {"eta": iso(self.now + timedelta(hours=3))})
        self.assertEqual(ctx.exception.code, "eta_forbidden")
        with self.assertRaises(ApiError) as ctx:
            self.svc.report_eta(allocation["id"], "hospital-h9", "hospital", "H9", {"eta": iso(self.now + timedelta(hours=3))})
        self.assertEqual(ctx.exception.code, "wrong_hospital")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign(allocation["id"], "coord", "coordinator", {"candidate_id": candidate["id"], "reason": "越权"})
        self.assertEqual(ctx.exception.code, "reassign_forbidden")


if __name__ == "__main__": unittest.main()
