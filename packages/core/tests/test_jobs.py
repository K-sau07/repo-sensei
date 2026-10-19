"""Sending tasks from the API: names, queues and a broker that is down."""

from __future__ import annotations

import uuid
from typing import Any

from kombu.exceptions import OperationalError

from sensei_core.config import Settings
from sensei_core.jobs import CeleryDispatcher, Queue, TaskName, make_celery


class FakeCelery:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self._error = error

    def send_task(self, name: str, **options: Any) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append((name, options))


async def test_dispatch_sends_the_delivery_to_the_webhooks_queue() -> None:
    fake = FakeCelery()
    delivery = uuid.uuid4()
    await CeleryDispatcher(fake).dispatch(delivery)  # pyright: ignore[reportArgumentType]
    assert fake.sent == [
        (
            TaskName.PROCESS_GITHUB_WEBHOOK.value,
            {"kwargs": {"delivery_id": str(delivery)}, "queue": Queue.WEBHOOKS.value},
        )
    ]


async def test_a_broker_outage_does_not_fail_the_delivery() -> None:
    """The delivery is already stored; the periodic sweep queues it once the broker is back."""
    fake = FakeCelery(OperationalError("Error 111 connecting to localhost:56379"))
    await CeleryDispatcher(fake).dispatch(uuid.uuid4())  # pyright: ignore[reportArgumentType]
    assert fake.sent == []


def test_task_names_route_to_their_queue() -> None:
    app = make_celery(Settings(env="test"))
    for name in TaskName:
        queue = name.value.split(".")[1]
        assert app.amqp.router.route({}, name.value)["queue"].name == queue
        assert queue in {q.value for q in Queue}
