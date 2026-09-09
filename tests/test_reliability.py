import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import backend_system
import front_system
from telethon.tl.functions.payments import GetPaymentFormRequest, GetStarsStatusRequest


class GiftStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        front_system.GIFTS_PATH = str(Path(self.temp_dir.name) / "gifts.json")
        front_system.GIFT_FILE_LOCK = None
        front_system.GIFTS = []
        Path(front_system.GIFTS_PATH).write_text("[]", encoding="utf-8")

    async def test_concurrent_updates_do_not_lose_gifts(self):
        first = [{"gift_id": "1000000001", "custom_emoji_id": "11", "emoji": "🎁", "price": 15}]
        second = [{"gift_id": "1000000002", "custom_emoji_id": "22", "emoji": "🧸", "price": 25}]

        await asyncio.gather(
            front_system.upsert_gifts(first),
            front_system.upsert_gifts(second),
        )

        stored = json.loads(Path(front_system.GIFTS_PATH).read_text(encoding="utf-8"))
        self.assertEqual({gift["gift_id"] for gift in stored}, {"1000000001", "1000000002"})
        self.assertEqual(len({gift["id"] for gift in stored}), 2)

    async def test_atomic_write_leaves_valid_json(self):
        gifts = [{"id": 1, "gift_id": "1000000001", "prix": 15}]
        await asyncio.to_thread(front_system._write_gifts_atomic, gifts)
        self.assertEqual(front_system.load_gifts_from_disk(), gifts)
        self.assertEqual(list(Path(self.temp_dir.name).glob(".gifts-*.tmp")), [])


class SessionTests(unittest.TestCase):
    def tearDown(self):
        front_system.SESSIONS.clear()

    def test_expired_session_is_removed(self):
        front_system.set_session(1, {"stage": "test"})
        front_system.SESSIONS[1]["_updated_at"] = time.monotonic() - front_system.SESSION_TTL_SECONDS - 1
        self.assertIsNone(front_system.get_session(1))
        self.assertNotIn(1, front_system.SESSIONS)


class BalanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        backend_system._balance_cache = None
        backend_system._balance_lock = None

    async def test_concurrent_balance_reads_are_coalesced(self):
        class Amount:
            amount = 321
            nanos = 0

        class Status:
            balance = Amount()

        class Client:
            calls = 0

            async def __call__(self, request):
                self.assert_request(request)
                self.calls += 1
                await asyncio.sleep(0.01)
                return Status()

            @staticmethod
            def assert_request(request):
                if not isinstance(request, GetStarsStatusRequest):
                    raise AssertionError(type(request))

        client = Client()
        balances = await asyncio.gather(
            *(backend_system.get_stars_balance(client) for _ in range(20))
        )
        self.assertEqual(client.calls, 1)
        self.assertTrue(all(balance.amount == 321 for balance in balances))


class RecipientResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        backend_system._channel_recipient_cache.clear()

    async def test_channel_username_can_be_reused_by_raw_channel_id(self):
        class FakeChannel:
            def __init__(self, channel_id):
                self.id = channel_id

        class Client:
            async def get_entity(self, value):
                if value == "giftchannel":
                    return FakeChannel(777)
                raise ValueError("entity type cannot be inferred from raw channel id")

        client = Client()
        with patch.object(backend_system, "Channel", FakeChannel):
            first = await backend_system.resolve_user(client, "@giftchannel")
            self.assertIsInstance(first, FakeChannel)

            confirmed = await backend_system.resolve_user(client, 777)
            self.assertIs(confirmed, first)


class PaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        backend_system._payment_lock = None
        backend_system.invalidate_stars_balance_cache()

    async def test_payments_are_serialized(self):
        class Form:
            form_id = 99

        class Client:
            active = 0
            max_active = 0

            async def get_input_entity(self, recipient):
                return recipient

            async def __call__(self, request):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                if isinstance(request, GetPaymentFormRequest):
                    return Form()
                return object()

        client = Client()
        results = await asyncio.gather(
            backend_system.send_gift(client, object(), 1000000001),
            backend_system.send_gift(client, object(), 1000000002),
        )
        self.assertTrue(all(result.success for result in results))
        self.assertEqual(client.max_active, 1)


if __name__ == "__main__":
    unittest.main()
