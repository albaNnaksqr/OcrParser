import asyncio
import json

import httpx
import pytest

from ocr_platform.agent.client import ControlClient, EventSpool, LogSpool


def test_event_spool_trims_oldest_records_when_pending_bytes_exceed_limit(tmp_path):
    spool = EventSpool(tmp_path / "event-spool", max_pending_bytes=610)

    for page_no in range(1, 6):
        spool.append(
            server_id="server-a",
            job_id=f"job-{page_no}",
            event={
                "type": "page_done",
                "payload": {"page_no": page_no, "message": "x" * 80},
            },
        )

    spooled_path = tmp_path / "event-spool" / "events.jsonl"
    records = [json.loads(line) for line in spooled_path.read_text(encoding="utf-8").splitlines()]
    dropped = json.loads((tmp_path / "event-spool" / "events.dropped.json").read_text(encoding="utf-8"))

    assert spooled_path.stat().st_size <= int(610 * 0.8)
    assert [record["job_id"] for record in records] == ["job-5"]
    assert dropped["dropped"] == 4
    assert dropped["last_drop_reason"] == "pending_spool_max_bytes_exceeded"


def test_log_spool_trims_oldest_records_when_pending_bytes_exceed_limit(tmp_path):
    spool = LogSpool(tmp_path / "event-spool", max_pending_bytes=610)

    for index in range(1, 6):
        spool.append(
            server_id="server-a",
            job_id=f"job-{index}",
            stream="stdout",
            line=f"log line {index} " + "x" * 80,
        )

    spooled_path = tmp_path / "event-spool" / "logs.jsonl"
    records = [json.loads(line) for line in spooled_path.read_text(encoding="utf-8").splitlines()]
    dropped = json.loads((tmp_path / "event-spool" / "logs.dropped.json").read_text(encoding="utf-8"))

    assert spooled_path.stat().st_size <= int(610 * 0.8)
    assert [record["job_id"] for record in records] == ["job-5"]
    assert dropped["dropped"] == 4
    assert dropped["last_drop_reason"] == "pending_spool_max_bytes_exceeded"


def test_post_event_spools_transient_control_failure_and_replays_after_restart(monkeypatch, tmp_path):
    requests = []
    fail = True

    def handler(request):
        nonlocal fail
        requests.append((str(request.url), json.loads(request.content)))
        if fail:
            return httpx.Response(503, json={"detail": "control unavailable"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    spool_dir = tmp_path / "event-spool"

    async def exercise():
        nonlocal fail
        first_client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
        )
        try:
            await first_client.post_event("job-1", {"type": "page_done", "payload": {"page_no": 1}})
        finally:
            await first_client.close()

        spooled_path = spool_dir / "events.jsonl"
        assert spooled_path.exists()
        spooled = [json.loads(line) for line in spooled_path.read_text(encoding="utf-8").splitlines()]
        assert spooled[0]["job_id"] == "job-1"
        assert spooled[0]["event"]["type"] == "page_done"

        fail = False
        second_client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
        )
        try:
            replayed = await second_client.replay_spooled_events()
        finally:
            await second_client.close()
        return replayed, spooled_path

    replayed, spooled_path = asyncio.run(exercise())

    assert replayed == 1
    assert spooled_path.read_text(encoding="utf-8") == ""
    assert requests == [
        ("http://control:8080/api/jobs/job-1/events", {"type": "page_done", "payload": {"page_no": 1}}),
        ("http://control:8080/api/jobs/job-1/events", {"type": "page_done", "payload": {"page_no": 1}}),
    ]


def test_post_event_does_not_spool_client_errors(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(401, json={"detail": "bad token"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    client = ControlClient(
        "http://control:8080",
        "server-a",
        event_spool_dir=tmp_path / "event-spool",
    )

    async def exercise():
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.post_event("job-1", {"type": "page_done", "payload": {"page_no": 1}})
        finally:
            await client.close()

    asyncio.run(exercise())

    assert not (tmp_path / "event-spool" / "events.jsonl").exists()


def test_post_log_spools_transient_control_failure_and_replays_after_restart(monkeypatch, tmp_path):
    requests = []
    fail = True

    def handler(request):
        nonlocal fail
        requests.append((str(request.url), json.loads(request.content)))
        if fail:
            return httpx.Response(503, json={"detail": "control unavailable"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    spool_dir = tmp_path / "event-spool"

    async def exercise():
        nonlocal fail
        first_client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
        )
        try:
            await first_client.post_log("job-1", "stdout", "hello from worker")
        finally:
            await first_client.close()

        spooled_path = spool_dir / "logs.jsonl"
        assert spooled_path.exists()
        spooled = [json.loads(line) for line in spooled_path.read_text(encoding="utf-8").splitlines()]
        assert spooled[0]["job_id"] == "job-1"
        assert spooled[0]["log"] == {
            "server_id": "server-a",
            "stream": "stdout",
            "line": "hello from worker",
        }

        fail = False
        second_client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
        )
        try:
            replayed = await second_client.replay_spooled_logs()
        finally:
            await second_client.close()
        return replayed, spooled_path

    replayed, spooled_path = asyncio.run(exercise())

    assert replayed == 1
    assert spooled_path.read_text(encoding="utf-8") == ""
    assert requests == [
        (
            "http://control:8080/api/jobs/job-1/logs",
            {"server_id": "server-a", "stream": "stdout", "line": "hello from worker"},
        ),
        (
            "http://control:8080/api/jobs/job-1/logs",
            {"server_id": "server-a", "stream": "stdout", "line": "hello from worker"},
        ),
    ]


def test_replay_spooled_logs_preserves_original_server_id(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        requests.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    spool_dir = tmp_path / "event-spool"
    spool_dir.mkdir()
    (spool_dir / "logs.jsonl").write_text(
        json.dumps(
            {
                "id": "log-1",
                "server_id": "original-worker",
                "job_id": "job-1",
                "log": {
                    "server_id": "original-worker",
                    "stream": "stderr",
                    "line": "captured before restart",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    client = ControlClient(
        "http://control:8080",
        "replay-worker",
        event_spool_dir=spool_dir,
    )

    async def exercise():
        try:
            return await client.replay_spooled_logs()
        finally:
            await client.close()

    replayed = asyncio.run(exercise())

    assert replayed == 1
    assert requests == [
        (
            "http://control:8080/api/jobs/job-1/logs",
            {"server_id": "original-worker", "stream": "stderr", "line": "captured before restart"},
        ),
    ]


def test_replay_quarantines_permanent_event_errors_and_continues(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        if str(request.url).endswith("/api/jobs/missing-job/events"):
            return httpx.Response(404, json={"detail": "unknown job"})
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    spool_dir = tmp_path / "event-spool"
    spool_dir.mkdir()
    (spool_dir / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "bad-event",
                        "server_id": "server-a",
                        "job_id": "missing-job",
                        "event": {"type": "page_done", "payload": {"page_no": 1}},
                    }
                ),
                json.dumps(
                    {
                        "id": "good-event",
                        "server_id": "server-a",
                        "job_id": "job-1",
                        "event": {"type": "page_done", "payload": {"page_no": 2}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = ControlClient(
        "http://control:8080",
        "server-a",
        event_spool_dir=spool_dir,
    )

    async def exercise():
        try:
            return await client.replay_spooled_events()
        finally:
            await client.close()

    replayed = asyncio.run(exercise())

    assert replayed == 1
    assert (spool_dir / "events.jsonl").read_text(encoding="utf-8") == ""
    quarantine_path = spool_dir / "events.failed.jsonl"
    quarantined = [json.loads(line) for line in quarantine_path.read_text(encoding="utf-8").splitlines()]
    assert quarantined[0]["id"] == "bad-event"
    assert quarantined[0]["replay_error"]["status_code"] == 404
    assert requests == [
        ("http://control:8080/api/jobs/missing-job/events", {"type": "page_done", "payload": {"page_no": 1}}),
        ("http://control:8080/api/jobs/job-1/events", {"type": "page_done", "payload": {"page_no": 2}}),
    ]


def test_replay_quarantines_malformed_spool_lines_and_continues(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    spool_dir = tmp_path / "event-spool"
    spool_dir.mkdir()
    (spool_dir / "events.jsonl").write_text(
        "\n".join(
            [
                '{"id": "broken-event", "job_id": ',
                json.dumps(
                    {
                        "id": "good-event",
                        "server_id": "server-a",
                        "job_id": "job-1",
                        "event": {"type": "page_done", "payload": {"page_no": 2}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = ControlClient(
        "http://control:8080",
        "server-a",
        event_spool_dir=spool_dir,
    )

    async def exercise():
        try:
            return await client.replay_spooled_events()
        finally:
            await client.close()

    replayed = asyncio.run(exercise())

    assert replayed == 1
    assert (spool_dir / "events.jsonl").read_text(encoding="utf-8") == ""
    quarantined = [
        json.loads(line)
        for line in (spool_dir / "events.failed.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert quarantined[0]["id"] == "malformed-spool-line-1"
    assert quarantined[0]["raw_line"] == '{"id": "broken-event", "job_id": '
    assert quarantined[0]["replay_error"]["error_type"] == "spool_parse_error"
    assert requests == [
        ("http://control:8080/api/jobs/job-1/events", {"type": "page_done", "payload": {"page_no": 2}}),
    ]


def test_replay_quarantines_non_object_spool_lines_and_continues(monkeypatch, tmp_path):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append((str(request.url), payload))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        return real_async_client(transport=transport, headers=kwargs.get("headers"))

    monkeypatch.setattr("ocr_platform.agent.client.httpx.AsyncClient", make_client)
    spool_dir = tmp_path / "event-spool"
    spool_dir.mkdir()
    (spool_dir / "events.jsonl").write_text(
        "\n".join(
            [
                "[]",
                json.dumps(
                    {
                        "id": "good-event",
                        "server_id": "server-a",
                        "job_id": "job-1",
                        "event": {"type": "page_done", "payload": {"page_no": 2}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    client = ControlClient(
        "http://control:8080",
        "server-a",
        event_spool_dir=spool_dir,
    )

    async def exercise():
        try:
            return await client.replay_spooled_events()
        finally:
            await client.close()

    replayed = asyncio.run(exercise())

    assert replayed == 1
    assert (spool_dir / "events.jsonl").read_text(encoding="utf-8") == ""
    quarantined = [
        json.loads(line)
        for line in (spool_dir / "events.failed.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert quarantined[0]["id"] == "invalid-spool-line-1"
    assert quarantined[0]["raw_line"] == "[]"
    assert quarantined[0]["replay_error"]["error_type"] == "spool_parse_error"
    assert "JSON object" in quarantined[0]["replay_error"]["message"]
    assert requests == [
        ("http://control:8080/api/jobs/job-1/events", {"type": "page_done", "payload": {"page_no": 2}}),
    ]


def test_event_replay_preserves_record_appended_while_request_is_in_flight(tmp_path):
    spool_dir = tmp_path / "event-spool"

    async def exercise():
        client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
        )
        client._spool_event("job-old", {"type": "page_done", "payload": {"page_no": 1}})
        request_started = asyncio.Event()
        release_request = asyncio.Event()
        delivered = []

        async def post_event(job_id, event):
            delivered.append((job_id, event))
            request_started.set()
            await release_request.wait()

        client._post_event_direct = post_event
        replay_task = asyncio.create_task(client.replay_spooled_events(limit=1))
        await request_started.wait()
        client._spool_event("job-new", {"type": "page_done", "payload": {"page_no": 2}})
        before_release = [record["job_id"] for record in client._event_spool.read_records()]
        release_request.set()
        first_replayed = await replay_task
        after_first_replay = [record["job_id"] for record in client._event_spool.read_records()]
        second_replayed = await client.replay_spooled_events()
        remaining = client._event_spool.read_records()
        await client.close()
        return first_replayed, second_replayed, before_release, after_first_replay, remaining, delivered

    first, second, before, after, remaining, delivered = asyncio.run(exercise())

    assert first == 1
    assert second == 1
    assert before == ["job-old", "job-new"]
    assert after == ["job-new"]
    assert remaining == []
    assert [job_id for job_id, _event in delivered] == ["job-old", "job-new"]


def test_log_replay_preserves_record_appended_while_request_is_in_flight(tmp_path):
    spool_dir = tmp_path / "event-spool"

    async def exercise():
        client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
        )
        client._spool_log("job-old", "stdout", "old")
        request_started = asyncio.Event()
        release_request = asyncio.Event()
        delivered = []

        async def post_log(job_id, stream, line, *, server_id=None):
            delivered.append((job_id, stream, line, server_id))
            request_started.set()
            await release_request.wait()

        client._post_log_direct = post_log
        replay_task = asyncio.create_task(client.replay_spooled_logs(limit=1))
        await request_started.wait()
        client._spool_log("job-new", "stderr", "new")
        before_release = [record["job_id"] for record in client._log_spool.read_records()]
        release_request.set()
        first_replayed = await replay_task
        after_first_replay = [record["job_id"] for record in client._log_spool.read_records()]
        second_replayed = await client.replay_spooled_logs()
        remaining = client._log_spool.read_records()
        await client.close()
        return first_replayed, second_replayed, before_release, after_first_replay, remaining, delivered

    first, second, before, after, remaining, delivered = asyncio.run(exercise())

    assert first == 1
    assert second == 1
    assert before == ["job-old", "job-new"]
    assert after == ["job-new"]
    assert remaining == []
    assert [job_id for job_id, _stream, _line, _server_id in delivered] == [
        "job-old",
        "job-new",
    ]


def test_concurrent_event_replays_do_not_send_the_same_record_twice(tmp_path):
    async def exercise():
        client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=tmp_path / "event-spool",
        )
        client._spool_event("job-1", {"type": "page_done", "payload": {"page_no": 1}})
        request_started = asyncio.Event()
        release_request = asyncio.Event()
        second_started = asyncio.Event()
        delivered = []

        async def post_event(job_id, event):
            delivered.append((job_id, event))
            request_started.set()
            await release_request.wait()

        async def second_replay():
            second_started.set()
            return await client.replay_spooled_events()

        client._post_event_direct = post_event
        first_task = asyncio.create_task(client.replay_spooled_events())
        await request_started.wait()
        second_task = asyncio.create_task(second_replay())
        await second_started.wait()
        release_request.set()
        results = await asyncio.gather(first_task, second_task)
        remaining = client._event_spool.read_records()
        await client.close()
        return results, delivered, remaining

    results, delivered, remaining = asyncio.run(exercise())

    assert sorted(results) == [0, 1]
    assert [job_id for job_id, _event in delivered] == ["job-1"]
    assert remaining == []


def test_concurrent_log_replays_do_not_send_the_same_record_twice(tmp_path):
    async def exercise():
        client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=tmp_path / "event-spool",
        )
        client._spool_log("job-1", "stdout", "line")
        request_started = asyncio.Event()
        release_request = asyncio.Event()
        second_started = asyncio.Event()
        delivered = []

        async def post_log(job_id, stream, line, *, server_id=None):
            delivered.append((job_id, stream, line, server_id))
            request_started.set()
            await release_request.wait()

        async def second_replay():
            second_started.set()
            return await client.replay_spooled_logs()

        client._post_log_direct = post_log
        first_task = asyncio.create_task(client.replay_spooled_logs())
        await request_started.wait()
        second_task = asyncio.create_task(second_replay())
        await second_started.wait()
        release_request.set()
        results = await asyncio.gather(first_task, second_task)
        remaining = client._log_spool.read_records()
        await client.close()
        return results, delivered, remaining

    results, delivered, remaining = asyncio.run(exercise())

    assert sorted(results) == [0, 1]
    assert [job_id for job_id, _stream, _line, _server_id in delivered] == ["job-1"]
    assert remaining == []


def test_event_replay_acknowledgement_does_not_remove_records_kept_after_trim(tmp_path):
    spool_dir = tmp_path / "event-spool"

    async def exercise():
        client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=spool_dir,
            event_spool_max_bytes=900,
        )
        client._spool_event(
            "job-old",
            {"type": "page_done", "payload": {"page_no": 1, "message": "x" * 120}},
        )
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        async def post_event(_job_id, _event):
            request_started.set()
            await release_request.wait()

        client._post_event_direct = post_event
        replay_task = asyncio.create_task(client.replay_spooled_events(limit=1))
        await request_started.wait()
        for page_no in range(2, 7):
            client._spool_event(
                f"job-{page_no}",
                {
                    "type": "page_done",
                    "payload": {"page_no": page_no, "message": "x" * 120},
                },
            )
        before_release = client._event_spool.read_records()
        release_request.set()
        replayed = await replay_task
        after_replay = client._event_spool.read_records()
        await client.close()
        return replayed, before_release, after_replay

    replayed, before, after = asyncio.run(exercise())

    expected_ids = [record["id"] for record in before if record["job_id"] != "job-old"]
    assert replayed == 1
    assert [record["id"] for record in after] == expected_ids
    assert all(record["job_id"] != "job-old" for record in after)


def test_event_replay_limit_zero_and_one_preserve_pending_order(tmp_path):
    async def exercise():
        client = ControlClient(
            "http://control:8080",
            "server-a",
            event_spool_dir=tmp_path / "event-spool",
        )
        client._spool_event("job-1", {"type": "page_done", "payload": {"page_no": 1}})
        client._spool_event("job-2", {"type": "page_done", "payload": {"page_no": 2}})
        delivered = []

        async def post_event(job_id, event):
            delivered.append((job_id, event))

        client._post_event_direct = post_event
        zero_replayed = await client.replay_spooled_events(limit=0)
        after_zero = [record["job_id"] for record in client._event_spool.read_records()]
        one_replayed = await client.replay_spooled_events(limit=1)
        after_one = [record["job_id"] for record in client._event_spool.read_records()]
        await client.close()
        return zero_replayed, one_replayed, after_zero, after_one, delivered

    zero, one, after_zero, after_one, delivered = asyncio.run(exercise())

    assert zero == 0
    assert one == 1
    assert after_zero == ["job-1", "job-2"]
    assert after_one == ["job-2"]
    assert [job_id for job_id, _event in delivered] == ["job-1"]
