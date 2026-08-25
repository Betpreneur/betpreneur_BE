from dataclasses import dataclass

from django.test import SimpleTestCase, TestCase

from betpreneur.platform.events.base import DomainEvent
from betpreneur.platform.events.bus import EventBus


@dataclass(frozen=True, kw_only=True)
class ThingHappened(DomainEvent):
    thing_id: int


@dataclass(frozen=True, kw_only=True)
class OtherHappened(DomainEvent):
    pass


class EventBusTests(SimpleTestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_delivers_to_every_subscriber_of_that_type(self):
        seen = []
        self.bus.subscribe(ThingHappened, lambda e: seen.append(("a", e.thing_id)))
        self.bus.subscribe(ThingHappened, lambda e: seen.append(("b", e.thing_id)))
        self.bus.subscribe(OtherHappened, lambda e: seen.append(("wrong", 0)))

        self.bus.publish(ThingHappened(thing_id=7), immediate=True)

        self.assertEqual(seen, [("a", 7), ("b", 7)])

    def test_a_failing_handler_does_not_break_the_publisher(self):
        seen = []

        def explodes(event):
            raise RuntimeError("handler is broken")

        self.bus.subscribe(ThingHappened, explodes)
        self.bus.subscribe(ThingHappened, lambda e: seen.append(e.thing_id))

        with self.assertLogs("betpreneur.platform.events.bus", "ERROR"):
            self.bus.publish(ThingHappened(thing_id=1), immediate=True)

        self.assertEqual(seen, [1], "later handlers must still run")

    def test_event_name_is_stable(self):
        self.assertTrue(ThingHappened.name().endswith("ThingHappened"))

    def test_occurred_at_is_excluded_from_equality(self):
        self.assertEqual(ThingHappened(thing_id=3), ThingHappened(thing_id=3))


class EventBusCommitTests(TestCase):
    def test_delivery_waits_for_commit(self):
        bus = EventBus()
        seen = []
        bus.subscribe(ThingHappened, lambda e: seen.append(e.thing_id))

        with self.captureOnCommitCallbacks(execute=True):
            bus.publish(ThingHappened(thing_id=42), immediate=False)
            self.assertEqual(seen, [], "must not fire before commit")

        self.assertEqual(seen, [42], "must fire on commit")
