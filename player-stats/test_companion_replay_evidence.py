"""Replay evidence: parse once, keep only what the valuation needs."""
import json
from pathlib import Path
import tempfile
import unittest

from companion import replay_evidence


def sample_report():
    return {
        "identity_binding": {"validated": True, "method": "slot_and_time"},
        "capabilities": {"production_orders": True},
        "catalog": {"sha256": "a" * 64},
        "match": {"version": 168061, "map_name": "Mogilev #10vs10"},
        "format": {"command_stream": {"production_orders": [{"source_slot": 3, "unit_guids": ["g"] * 1}],
                                      "cancellation_orders": [{"source_slot": 3}]}},
        "players": [{"id": "123", "stream_source_slot": 3,
                     "slot_binding": {"status": "cross_validated"},
                     "stats": {"production_orders": {"evidence": "production_requests", "units": []}},
                     "rows": [{"native": "row"}]}],
        "rows": [{"native": "row"}],
    }


class ReplayEvidenceTests(unittest.TestCase):
    def test_extract_keeps_the_subset_derive_match_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "match.rpl3"
            replay.write_bytes(b"ESAV\0\0\0\x03" + b"payload" * 8)
            digest = __import__("hashlib").sha256(replay.read_bytes()).hexdigest()
            stored = replay_evidence.extract(replay, digest, directory,
                                             parser=lambda _path: sample_report())
            self.assertEqual(stored["identity_binding"]["validated"], True)
            self.assertEqual(stored["format"]["command_stream"]["production_orders"],
                             [{"source_slot": 3, "unit_guids": ["g"]}])
            self.assertEqual(stored["players"][0]["stats"]["production_orders"]["evidence"], "production_requests")
            self.assertNotIn("rows", stored["players"][0])
            # The replay itself is never copied; only the small evidence file.
            self.assertEqual(sorted(item.name for item in Path(directory).iterdir()),
                             ["match.rpl3", "replay-evidence"])
            loaded = replay_evidence.load(directory, digest)
            self.assertEqual(loaded["match"]["version"], 168061)
            self.assertEqual(replay.read_bytes()[:8], b"ESAV\0\0\0\x03")

    def test_a_digest_mismatch_or_missing_file_never_stores_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "match.rpl3"
            replay.write_bytes(b"ESAV\0\0\0\x03" + b"payload" * 8)
            wrong = "b" * 64
            with self.assertRaises(ValueError):
                replay_evidence.extract(replay, wrong, directory, parser=lambda _path: sample_report())
            self.assertFalse(replay_evidence.evidence_path(directory, wrong).exists())
            with self.assertRaises(OSError):
                replay_evidence.extract(Path(directory) / "missing.rpl3", "c" * 64, directory,
                                        parser=lambda _path: sample_report())
            for digest in (None, "", "xyz", "c" * 63):
                self.assertIsNone(replay_evidence.load(directory, digest))

    def test_changed_replay_during_parsing_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "match.rpl3"
            replay.write_bytes(b"ESAV\0\0\0\x03" + b"payload" * 8)
            digest = __import__("hashlib").sha256(replay.read_bytes()).hexdigest()

            def parser(path):
                Path(path).write_bytes(b"ESAV\0\0\0\x03" + b"changed" * 8)
                return sample_report()

            with self.assertRaises(ValueError):
                replay_evidence.extract(replay, digest, directory, parser=parser)
            self.assertFalse(replay_evidence.evidence_path(directory, digest).exists())

    def test_compact_drops_unknown_shapes_without_raising(self):
        value = replay_evidence.compact({"players": ["not-a-dict", {"id": "1"}], "format": {"command_stream": None}})
        self.assertEqual(value["players"], [{"id": "1", "stream_source_slot": None,
                                             "slot_binding": None, "stats": {"production_orders": None}}])
        self.assertEqual(value["format"]["command_stream"]["production_orders"], [])


if __name__ == "__main__":
    unittest.main()
