"""
Test Booking Flow End-to-End

Simulira cijeli booking flow kroz MessageEngine:
1. Korisnik: "Trebam vozilo sutra od 8 do 17"
2. Bot: Prikazuje slobodna vozila, pita za potvrdu
3. Korisnik: "Da"
4. Bot: Kreira rezervaciju

Pokreni: python -m tests.test_booking_flow

Note: These are integration tests requiring real Redis, DB, and API connections.
      They are skipped in CI (when APP_ENV=testing).
"""

import asyncio
import logging
import sys
import os
import pytest

# Fix Windows console encoding for emojis
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Reduce noise
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("APP_ENV") == "testing", reason="Integration test - requires real services")
async def test_booking_flow():
    """Test complete booking flow."""

    print("\n" + "="*60)
    print("🚗 BOOKING FLOW TEST")
    print("="*60 + "\n")

    # 1. Initialize services
    print("📦 Initializing services...")

    from config import get_settings
    import redis.asyncio as aioredis

    settings = get_settings()

    # Redis
    redis_client = aioredis.from_url(
        settings.REDIS_URL.replace("fleet_redis", "localhost"),  # Local testing
        encoding="utf-8",
        decode_responses=True
    )

    try:
        await redis_client.ping()
        print("✅ Redis connected")
    except Exception as e:
        print(f"❌ Redis not available: {e}")
        print("   Make sure Redis is running: docker-compose up redis -d")
        return

    # API Gateway
    from services.api_gateway import APIGateway
    gateway = APIGateway(redis_client=redis_client)
    print("✅ API Gateway initialized")

    # Tool Registry
    from services.registry import ToolRegistry
    registry = ToolRegistry(redis_client=redis_client)

    print("📥 Loading tools from Swagger...")
    success = await registry.initialize(settings.swagger_sources)

    if not success:
        print("❌ Failed to load tools")
        return

    print(f"✅ Loaded {len(registry.tools)} tools")

    # Check booking tools exist
    if "get_AvailableVehicles" not in registry.tools:
        print("❌ get_AvailableVehicles not found in registry!")
        return
    if "post_VehicleCalendar" not in registry.tools:
        print("❌ post_VehicleCalendar not found in registry!")
        return

    print("✅ Booking tools found: get_AvailableVehicles, post_VehicleCalendar")

    # Services
    from services.queue_service import QueueService
    from services.cache_service import CacheService
    from services.context_service import ContextService
    from services.message_engine import MessageEngine
    from database import AsyncSessionLocal

    queue = QueueService(redis_client)
    cache = CacheService(redis_client)
    context = ContextService(redis_client)

    # 2. Create test user context
    # Using the person from curl example
    TEST_PHONE = "+385991234567"  # Test phone
    TEST_PERSON_ID = "fc0a5a65-b832-44f6-b305-38f2178a6b56"  # Damir Škrtić

    # Simulate user in system
    user_context = {
        "person_id": TEST_PERSON_ID,
        "display_name": "Test User",
        "tenant_id": settings.MOBILITY_TENANT_ID,
        "vehicle": {
            "id": None,  # No assigned vehicle - will need to book
            "name": None,
            "plate": None
        }
    }

    print(f"\n👤 Test User: {user_context['display_name']}")
    print(f"   Person ID: {TEST_PERSON_ID}")
    print(f"   Tenant: {settings.MOBILITY_TENANT_ID}")

    # 3. Initialize MessageEngine
    print("\n🔧 Initializing MessageEngine...")

    async with AsyncSessionLocal() as db:
        engine = MessageEngine(
            gateway=gateway,
            registry=registry,
            context_service=context,
            queue_service=queue,
            cache_service=cache,
            db_session=db
        )
        print("✅ MessageEngine ready")

        # Calculate tomorrow's date for booking
        tomorrow = datetime.now() + timedelta(days=1)
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")

        # 4. Test Step 1: Request booking
        print("\n" + "-"*60)
        print("📝 STEP 1: Request booking")
        print("-"*60)

        booking_request = f"Trebam vozilo sutra od 8 do 17"
        print(f"👤 User: {booking_request}")

        response1 = await engine.process(TEST_PHONE, booking_request, "test_msg_1")

        print(f"\n🤖 Bot:\n{response1}")

        # Check if response mentions vehicles
        if "vozilo" not in response1.lower() and "slobodn" not in response1.lower():
            print("\n⚠️ Response doesn't mention vehicles. Checking state...")

        # 5. Test Step 2: Confirm with "Da"
        print("\n" + "-"*60)
        print("📝 STEP 2: Confirm reservation")
        print("-"*60)

        confirm_msg = "Da"
        print(f"👤 User: {confirm_msg}")

        response2 = await engine.process(TEST_PHONE, confirm_msg, "test_msg_2")

        print(f"\n🤖 Bot:\n{response2}")

        # Check success indicators
        success_indicators = ["uspješn", "rezerv", "potvrđen", "kreiran"]
        is_success = any(ind in response2.lower() for ind in success_indicators)

        print("\n" + "="*60)
        if is_success:
            print("✅ BOOKING FLOW TEST PASSED!")
        else:
            print("⚠️ BOOKING FLOW TEST - Check responses above")
        print("="*60)

    # Cleanup
    await gateway.close()
    await redis_client.aclose()


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION_TESTS") != "1", reason="Integration test - set RUN_INTEGRATION_TESTS=1 to run")
async def test_api_directly():
    """Quick API test without full engine."""

    print("\n" + "="*60)
    print("[API] DIRECT API TEST")
    print("="*60 + "\n")

    from config import get_settings
    import redis.asyncio as aioredis

    settings = get_settings()

    redis_client = aioredis.from_url(
        settings.REDIS_URL.replace("fleet_redis", "localhost"),
        encoding="utf-8",
        decode_responses=True
    )

    from services.api_gateway import APIGateway, HttpMethod
    gateway = APIGateway(redis_client=redis_client)

    # Test token via token_manager
    print("[KEY] Getting token...")
    token = await gateway.token_manager.get_token()
    if token:
        print(f"[OK] Token obtained: {token[:50]}...")
    else:
        print("[FAIL] Failed to get token")
        return

    # Test available vehicles
    print("\n[CAR] Testing AvailableVehicles...")

    from datetime import datetime, timedelta
    tomorrow = datetime.now() + timedelta(days=1)

    result = await gateway.execute(
        method=HttpMethod.GET,
        path="/vehiclemgt/AvailableVehicles",
        params={
            "from": tomorrow.replace(hour=8, minute=0).isoformat(),
            "to": tomorrow.replace(hour=17, minute=0).isoformat()
        }
    )

    if result.success and result.data and "Data" in result.data:
        vehicles = result.data["Data"]
        print(f"[OK] Found {len(vehicles)} available vehicles:")
        for v in vehicles[:3]:
            print(f"   - {v.get('DisplayName')} ({v.get('LicencePlate')})")
    else:
        print(f"[FAIL] API returned: {result}")

    await gateway.close()
    await redis_client.aclose()


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("APP_ENV") == "testing", reason="Integration test - requires real services")
async def test_flow_handler_directly():
    """Test FlowHandler directly without user lookup."""

    print("\n" + "="*60)
    print("[FLOW] DIRECT FLOW HANDLER TEST")
    print("="*60 + "\n")

    from config import get_settings
    import redis.asyncio as aioredis

    settings = get_settings()

    # Redis
    redis_client = aioredis.from_url(
        settings.REDIS_URL.replace("fleet_redis", "localhost"),
        encoding="utf-8",
        decode_responses=True
    )

    try:
        await redis_client.ping()
        print("[OK] Redis connected")
    except Exception as e:
        print(f"[FAIL] Redis not available: {e}")
        return

    # Initialize services
    from services.api_gateway import APIGateway
    from services.registry import ToolRegistry
    from services.tool_executor import ToolExecutor
    from services.ai_orchestrator import AIOrchestrator
    from services.response_formatter import ResponseFormatter
    from services.engine.flow_handler import FlowHandler
    from services.conversation_manager import ConversationManager

    gateway = APIGateway(redis_client=redis_client)
    registry = ToolRegistry(redis_client=redis_client)

    print("[LOAD] Loading tools...")
    await registry.initialize(settings.swagger_sources)
    print(f"[OK] Loaded {len(registry.tools)} tools")

    executor = ToolExecutor(gateway)
    ai = AIOrchestrator()
    formatter = ResponseFormatter()

    flow_handler = FlowHandler(
        registry=registry,
        executor=executor,
        ai=ai,
        formatter=formatter
    )

    # Create mock user context with known person
    TEST_PERSON_ID = "fc0a5a65-b832-44f6-b305-38f2178a6b56"  # Damir Skrtic

    user_context = {
        "person_id": TEST_PERSON_ID,
        "display_name": "Damir Skrtic",
        "tenant_id": settings.MOBILITY_TENANT_ID,
        "vehicle": {
            "id": None,
            "name": None,
            "plate": None
        }
    }

    print(f"\n[USER] Test User: {user_context['display_name']}")
    print(f"   Person ID: {TEST_PERSON_ID}")

    # Create conversation manager
    conv_manager = ConversationManager("+385991234567", redis_client)
    await conv_manager.load()

    # Calculate times
    from datetime import datetime, timedelta
    tomorrow = datetime.now() + timedelta(days=1)
    from_time = tomorrow.replace(hour=8, minute=0, second=0, microsecond=0).isoformat()
    to_time = tomorrow.replace(hour=17, minute=0, second=0, microsecond=0).isoformat()

    print(f"\n[TIME] Booking period:")
    print(f"   From: {from_time}")
    print(f"   To: {to_time}")

    # Test Step 1: Call availability flow
    print("\n" + "-"*60)
    print("[STEP 1] Calling handle_availability...")
    print("-"*60)

    tool_result = {
        "tool": "get_AvailableVehicles",
        "parameters": {
            "from": from_time,
            "to": to_time
        },
        "tool_call_id": "test_availability"
    }

    result1 = await flow_handler.handle_availability(
        tool_name="get_AvailableVehicles",
        parameters={"from": from_time, "to": to_time},
        user_context=user_context,
        conv_manager=conv_manager
    )

    print(f"\n[BOT RESPONSE]:")
    if result1.get("needs_input"):
        print(result1.get("prompt", ""))
    elif result1.get("final_response"):
        print(result1.get("final_response"))
    else:
        print(f"Result: {result1}")

    # Check state
    print(f"\n[STATE] After availability check:")
    print(f"   State: {conv_manager.get_state()}")
    print(f"   Flow: {conv_manager.get_current_flow()}")
    print(f"   Displayed items: {len(conv_manager.get_displayed_items())}")

    if conv_manager.get_displayed_items():
        # Test Step 2: Confirm
        print("\n" + "-"*60)
        print("[STEP 2] Simulating confirmation...")
        print("-"*60)

        # Select first vehicle
        first_vehicle = conv_manager.get_displayed_items()[0]
        await conv_manager.select_item(first_vehicle)
        await conv_manager.request_confirmation("Potvrdite rezervaciju")

        print(f"[USER] Da")

        # Mock callback for new request
        async def mock_new_request(*args):
            return "Mock new request"

        result2 = await flow_handler.handle_confirmation(
            sender="+385991234567",
            text="Da",
            user_context=user_context,
            conv_manager=conv_manager
        )

        print(f"\n[BOT RESPONSE]:")
        print(result2)

        # Check for success
        success_indicators = ["uspje", "rezerv", "potvrđen", "kreiran", "ID:"]
        is_success = any(ind.lower() in result2.lower() for ind in success_indicators)

        print("\n" + "="*60)
        if is_success:
            print("[SUCCESS] BOOKING FLOW COMPLETED!")
        else:
            print("[CHECK] Review response above")
        print("="*60)
    else:
        print("\n[WARN] No vehicles displayed - cannot continue to confirmation")

    # Cleanup
    await conv_manager.clear()
    await gateway.close()
    await redis_client.aclose()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--api":
        asyncio.run(test_api_directly())
    elif len(sys.argv) > 1 and sys.argv[1] == "--flow":
        asyncio.run(test_flow_handler_directly())
    else:
        asyncio.run(test_booking_flow())
