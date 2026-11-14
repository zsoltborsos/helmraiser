"""Tests for EventLogger — local JSONL event logging."""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from helmraiser.ledger.event_logger import EventLogger


class TestEventLoggerHappyPath:
    """EventLogger writes JSONL correctly."""

    def test_log_writes_single_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            logger.log("agent_started", agent_id="agent-1", run_id="run-123")
            events_file = Path(tmpdir) / "events.jsonl"
            assert events_file.exists()
            lines = events_file.read_text().strip().split("\n")
            assert len(lines) == 1
            event = json.loads(lines[0])
            assert event["event_type"] == "agent_started"
            assert event["agent_id"] == "agent-1"
            assert event["run_id"] == "run-123"
            assert "timestamp" in event

    def test_log_appends_multiple_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            logger.log("event_1", agent_id="a1")
            logger.log("event_2", agent_id="a2")
            logger.log("event_3", agent_id="a3")
            events_file = Path(tmpdir) / "events.jsonl"
            lines = events_file.read_text().strip().split("\n")
            assert len(lines) == 3
            assert json.loads(lines[0])["event_type"] == "event_1"
            assert json.loads(lines[1])["event_type"] == "event_2"
            assert json.loads(lines[2])["event_type"] == "event_3"

    def test_log_without_agent_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            logger.log("run_started", run_id="run-123")
            events_file = Path(tmpdir) / "events.jsonl"
            event = json.loads(events_file.read_text().strip())
            assert event["event_type"] == "run_started"
            assert event["agent_id"] is None
            assert event["run_id"] == "run-123"

    def test_log_with_extra_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            logger.log(
                "phase_complete",
                agent_id="a1",
                phase=2,
                output_file="output.md",
                duration=42.5,
            )
            events_file = Path(tmpdir) / "events.jsonl"
            event = json.loads(events_file.read_text().strip())
            assert event["event_type"] == "phase_complete"
            assert event["phase"] == 2
            assert event["output_file"] == "output.md"
            assert event["duration"] == 42.5

    def test_creates_run_dir_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "subdir" / "run-123"
            logger = EventLogger(run_dir=run_dir)
            logger.log("test_event")
            events_file = run_dir / "events.jsonl"
            assert events_file.exists()

    def test_timestamp_is_iso_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            logger.log("test_event")
            events_file = Path(tmpdir) / "events.jsonl"
            event = json.loads(events_file.read_text().strip())
            # Should be parseable as ISO format
            from datetime import datetime

            datetime.fromisoformat(event["timestamp"])


class TestEventLoggerDisasterPath:
    """EventLogger never fails — catches all exceptions."""

    def test_log_catches_exception_on_unwritable_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            run_dir = Path(tmpdir) / "readonly"
            run_dir.mkdir()
            os.chmod(run_dir, 0o000)
            try:
                logger = EventLogger(run_dir=run_dir)
                # Should not raise
                logger.log("test_event", agent_id="a1")
            finally:
                os.chmod(run_dir, 0o755)

    def test_log_catches_exception_on_full_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            with patch("builtins.open", side_effect=OSError("No space left on device")):
                # Should not raise
                logger.log("test_event", agent_id="a1")

    def test_log_catches_json_serialization_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))

            # Non-serializable object should not crash
            class Unserializable:
                pass

            logger.log("test_event", bad_data=Unserializable())


class TestEventLoggerConcurrent:
    """EventLogger handles concurrent writes."""

    def test_concurrent_writes_from_multiple_threads(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            threads = []

            def write_events(prefix, count):
                for i in range(count):
                    logger.log(f"{prefix}_event", index=i)

            for t in range(4):
                thread = threading.Thread(target=write_events, args=(f"thread{t}", 10))
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join(timeout=10)

            events_file = Path(tmpdir) / "events.jsonl"
            lines = events_file.read_text().strip().split("\n")
            # All 40 events should be written
            assert len(lines) == 40

    def test_concurrent_writes_produce_valid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = EventLogger(run_dir=Path(tmpdir))
            threads = []

            def write_events(n):
                for i in range(n):
                    logger.log("concurrent_event", index=i)

            for _ in range(4):
                thread = threading.Thread(target=write_events, args=(10,))
                threads.append(thread)
                thread.start()

            for thread in threads:
                thread.join(timeout=10)

            events_file = Path(tmpdir) / "events.jsonl"
            lines = events_file.read_text().strip().split("\n")
            # Every line should be valid JSON
            for line in lines:
                event = json.loads(line)
                assert "event_type" in event
                assert "timestamp" in event
