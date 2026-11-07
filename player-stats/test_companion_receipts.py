"""Local cancellation and small receipt queries never retransmit a timeline."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from companion.local_replay_library import report_fingerprint
from companion.outbox import Outbox
from companion.receiver import API_PATH, ArchiveIndex, ReceiverStore, create_server
from companion.service import CompanionService, DeliveryWorker
from companion.upload import Uploader, UploadError, safe_receipt
from test_companion_receiver import native_archive, payload


class ReceiptQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.now = 1000.0
        self.box = Outbox(self.base / 'outbox.sqlite3', clock=lambda: self.now)

    def test_old_queue_migration_preserves_installation_payload_and_delivery(self):
        original = payload()
        self.box.enqueue(original)
        self.box.mark_sent(original['submission_id'], {'submission_id': original['submission_id'], 'status': 'pending_match'})
        with contextlib.closing(sqlite3.connect(self.box.path)) as db, db:
            before = db.execute('SELECT payload,payload_sha256,receipt FROM companion_outbox').fetchone()
            db.execute('DROP TABLE companion_delivery_state')
            db.execute('DROP TABLE companion_deleted_reports')
        restored = Outbox(self.box.path, clock=lambda: self.now)
        self.assertEqual(restored.installation_id, self.box.installation_id)
        self.assertEqual(restored.report_statuses()[report_fingerprint(original)]['server_status'], 'pending_match')
        self.assertEqual(restored.receipts_due(), [original['submission_id']])
        with contextlib.closing(sqlite3.connect(self.box.path)) as db:
            self.assertEqual(db.execute('SELECT payload,payload_sha256,receipt FROM companion_outbox').fetchone(), before)

    def test_cancel_one_capture_removes_unsent_jobs_and_prevents_recreation(self):
        capture = payload()
        fingerprint = report_fingerprint(capture)
        for index, state in enumerate(('pending', 'rejected', 'sent')):
            job = {**capture, 'submission_id': str(index) * 64}
            self.box.enqueue(job)
            if state == 'rejected':
                self.box.mark_rejected(job['submission_id'], 'http_403')
            elif state == 'sent':
                self.box.mark_sent(job['submission_id'], {'submission_id': job['submission_id'], 'status': 'accepted'})
        other = {**capture, 'submission_id': 'd' * 64, 'report_generation': 2}
        self.box.enqueue(other)
        self.assertEqual(self.box.cancel_report(fingerprint), 2)
        self.assertEqual([row['submission_id'] for row in self.box.due()], [other['submission_id']])
        self.assertEqual(self.box.status()['total'], 1)
        restored = Outbox(self.box.path, clock=lambda: self.now)
        self.assertFalse(restored.enqueue({**capture, 'submission_id': 'f' * 64}))
        self.assertEqual(restored.cancel_report(fingerprint), 0)
        self.assertEqual(restored.receipts_due(), [])
        self.assertNotIn(fingerprint, restored.report_statuses())

    def test_receipt_refresh_is_bounded_and_accepted_does_not_regress(self):
        report = payload()
        identifier = report['submission_id']
        self.box.enqueue(report)
        self.box.mark_sent(identifier, {'submission_id': identifier, 'status': 'pending_match',
                                       'pending_reason': 'archive_temporarily_unavailable'})
        self.assertEqual(self.box.status()['server_pending'], 1)
        self.assertEqual(self.box.receipts_due(), [identifier])
        self.assertEqual(self.box.receipts_due(), [])
        self.now += 60
        self.assertEqual(self.box.receipts_due(), [identifier])
        self.assertTrue(self.box.update_receipt(identifier, {'submission_id': identifier, 'status': 'accepted'}))
        self.assertFalse(self.box.update_receipt(identifier, {'submission_id': identifier, 'status': 'pending_match'}))
        self.assertEqual(self.box.status()['matched'], 1)
        self.assertEqual(self.box.status()['server_pending'], 0)
        self.now += 60
        self.assertEqual(self.box.receipts_due(), [])
        with contextlib.closing(sqlite3.connect(self.box.path)) as db:
            self.assertEqual(json.loads(db.execute('SELECT receipt FROM companion_outbox').fetchone()[0])['status'], 'accepted')

    def test_delete_after_job_fetch_prevents_the_network_send(self):
        report = payload()
        self.box.enqueue(report)
        jobs = self.box.due()
        self.box.cancel_report(report_fingerprint(report))
        transport = Mock()
        with patch.object(self.box, 'due', return_value=jobs):
            self.assertEqual(CompanionService(self.box, uploader=transport).flush(), 0)
        transport.send.assert_not_called()

    def test_file_marker_still_blocks_send_if_initial_sql_cancellation_failed(self):
        from companion.local_deletion import delete_record
        report = payload()
        self.box.enqueue(report)
        fingerprint = report_fingerprint(report)
        broken = Mock()
        broken.cancel_report.side_effect = OSError('temporary storage error')
        with self.assertRaises(OSError):
            delete_record(self.base, fingerprint, outbox=broken)
        self.assertFalse(self.box.enqueue({**report, 'submission_id': 'b' * 64}))
        transport = Mock()
        self.assertEqual(CompanionService(self.box, uploader=transport).flush(), 0)
        transport.send.assert_not_called()
        self.assertEqual(self.box.status()['total'], 0)

    def test_delivery_worker_refreshes_sent_pending_without_sending_again(self):
        report = payload()
        identifier = report['submission_id']
        self.box.enqueue(report)
        self.box.mark_sent(identifier, {'submission_id': identifier, 'status': 'pending_match'})
        ready = threading.Event()
        transport = Mock()
        transport.query_receipt.return_value = {'submission_id': identifier, 'status': 'accepted'}
        def event(value):
            if value['event'] == 'server_receipt_updated':
                ready.set()
        worker = DeliveryWorker(CompanionService(self.box, uploader=transport, emit=event), interval=0.01)
        worker.start()
        try:
            self.assertTrue(ready.wait(2))
        finally:
            worker.close()
        transport.send.assert_not_called()
        transport.query_receipt.assert_called_once_with(identifier)
        self.assertEqual(self.box.status()['matched'], 1)

    def test_http_receipt_after_reconcile_updates_local_status_without_resubmission(self):
        archive = self.base / 'archive.sqlite3'
        native_archive(archive, finished=False, room='test-room')
        store = ReceiverStore(self.base / 'receiver.sqlite3', archive_index=ArchiveIndex(archive))
        token = 'fixture-receipt-token'
        server = create_server(store, token)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        uploader = Uploader('http://127.0.0.1:' + str(server.server_port) + API_PATH,
                            token=token, allow_loopback_http=True)
        report = {**payload(), 'installation_id': self.box.installation_id}
        self.box.enqueue(report)
        self.assertEqual(CompanionService(self.box, uploader=uploader).flush(), 1)
        with contextlib.closing(sqlite3.connect(store.path)) as db:
            before = db.execute('SELECT payload,received_at FROM submissions').fetchone()
        with contextlib.closing(sqlite3.connect(archive)) as db, db:
            archived = json.loads(db.execute('SELECT payload FROM reports').fetchone()[0])
            archived['match']['completion']['status'] = 'finished'
            db.execute("UPDATE reports SET completion_status='finished',payload=?", (json.dumps(archived),))
        store.reconcile()
        receipt = uploader.query_receipt(report['submission_id'])
        self.assertEqual(receipt, {'submission_id': report['submission_id'], 'status': 'accepted'})
        self.box.update_receipt(report['submission_id'], receipt)
        self.assertEqual(self.box.status()['matched'], 1)
        with contextlib.closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute('SELECT payload,received_at FROM submissions').fetchall(), [before])
        with self.assertRaises(UploadError) as missing:
            uploader.query_receipt('f' * 64)
        self.assertEqual(missing.exception.status_code, 404)

    def test_receipt_only_keeps_controlled_reason_and_rejects_malformed_status(self):
        identifier = 'a' * 64
        receipt = safe_receipt({'submission_id': identifier, 'status': 'pending_match',
                                'pending_reason': 'archive_temporarily_unavailable', 'message': 'private'}, identifier)
        self.assertNotIn('message', receipt)
        for reason in ('private', {'not': 'a code'}, ['not a code']):
            result = safe_receipt({'submission_id': identifier, 'status': 'pending_match', 'pending_reason': reason}, identifier)
            self.assertNotIn('pending_reason', result)
        for status in ([], {}, 'not-a-status'):
            with self.assertRaises(ValueError):
                safe_receipt({'submission_id': identifier, 'status': status}, identifier)


if __name__ == '__main__':
    unittest.main()
