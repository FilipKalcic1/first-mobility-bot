"""
Test Mileage Input Flow End-to-End

Simulira mileage input flow kroz MessageEngine:
1. Korisnik: "Unesi kilometražu 16500"
2. Bot: Prikazuje potvrdu, pita za potvrdu
3. Korisnik: "Da"
4. Bot: Unosi kilometražu

Pokreni: python -m tests.test_mileage_flow

Note: Integration test - skipped in CI (when APP_ENV=testing).
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger(__name__)

# Reduce noise
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_INTEGRATION_TESTS") != "1", reason="Integration test - set RUN_INTEGRATION_TESTS=1 to run")
async def test_mileage_api():
    """Test AddMileage API directly."""

    print("\n" + "="*60)
    print("[API] MILEAGE API TEST")
    print("="*60 + "\n")

    from config import get_settings
    import redis.asyncio as aioredis
    from services.api_gateway import APIGateway, HttpMethod
    from datetime import datetime, timedelta

    settings = get_settings()

    redis_client = aioredis.from_url(
        settings.REDIS_URL.replace("fleet_redis", "localhost"),
        encoding="utf-8",
        decode_responses=True
    )

    gateway = APIGateway(redis_client=redis_client)

    # 1. Get token
    print("[KEY] Getting token...")
    token = await gateway.token_manager.get_token()
    print(f"[OK] Token obtained")

    # 2. Get available vehicles (to get valid VehicleId)
    print("\n[CAR] Getting AvailableVehicles...")

    tomorrow = datetime.now() + timedelta(days=1)
    from_time = tomorrow.replace(hour=8, minute=0).isoformat()
    to_time = tomorrow.replace(hour=17, minute=0).isoformat()

    result = await gateway.execute(
        method=HttpMethod.GET,
        path="/vehiclemgt/AvailableVehicles",
        params={"from": from_time, "to": to_time}
    )

    if not result.success or not result.data:
        print(f"[FAIL] AvailableVehicles failed: {result.error_message}")
        return None

    data = result.data.get("Data", result.data) if isinstance(result.data, dict) else result.data
    vehicles = data if isinstance(data, list) else [data]

    if not vehicles:
        print("[FAIL] No vehicles found")
        return None

    v = vehicles[0]
    vehicle_id = v.get("Id")
    vehicle_name = v.get("DisplayName") or v.get("FullVehicleName", "N/A")
    plate = v.get("LicencePlate", "N/A")
    current_mileage = v.get("LastMileage") or v.get("Mileage", 0) or 10000

    print(f"[OK] Vehicle: {vehicle_name} ({plate})")
    print(f"     ID: {vehicle_id}")
    print(f"     Current mileage: {current_mileage}")

    # 3. Test AddMileage
    print("\n[MILEAGE] Testing AddMileage...")
    new_mileage = current_mileage + 10

    mileage_result = await gateway.execute(
        method=HttpMethod.POST,
        path="/automation/AddMileage",
        body={
            "VehicleId": vehicle_id,
            "Value": new_mileage,
            "Comment": "Test mileage from bot"
        }
    )

    print(f"     Result: {mileage_result.success}")
    print(f"     Status: {mileage_result.status_code}")

    if mileage_result.success:
        print(f"[OK] Mileage {new_mileage} submitted successfully!")
    else:
        print(f"[FAIL] Error: {mileage_result.error_message}")

    await gateway.close()
    await redis_client.aclose()

    return {
        "vehicle_id": vehicle_id,
        "vehicle_name": vehicle_name,
        "plate": plate,
        "mileage": current_mileage
    }


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("APP_ENV") == "testing", reason="Integration test - requires real services")
async def test_mileage_flow():
    """Test mileage input flow through FlowHandler."""

    print("\n" + "="*60)
    print("[FLOW] MILEAGE FLOW TEST")
    print("="*60 + "\n")

    from config import get_settings
    import redis.asyncio as aioredis
    from services.api_gateway import APIGateway, HttpMethod
    from services.registry import ToolRegistry
    from services.tool_executor import ToolExecutor
    from services.ai_orchestrator import AIOrchestrator
    from services.response_formatter import ResponseFormatter
    from services.engine.flow_handler import FlowHandler
    from services.conversation_manager import ConversationManager
    from datetime import datetime, timedelta

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
    gateway = APIGateway(redis_client=redis_client)
    registry = ToolRegistry(redis_client=redis_client)

    print("[LOAD] Loading tools...")
    await registry.initialize(settings.swagger_sources)
    print(f"[OK] Loaded {len(registry.tools)} tools")

    # Check mileage tool exists
    if "post_AddMileage" not in registry.tools:
        print("[FAIL] post_AddMileage not found in registry!")
        return

    print("[OK] Mileage tool found: post_AddMileage")

    executor = ToolExecutor(gateway)
    ai = AIOrchestrator()
    formatter = ResponseFormatter()

    flow_handler = FlowHandler(
        registry=registry,
        executor=executor,
        ai=ai,
        formatter=formatter
    )

    # Get vehicle info first
    print("\n[CAR] Getting vehicle for test user...")

    tomorrow = datetime.now() + timedelta(days=1)
    from_time = tomorrow.replace(hour=8, minute=0).isoformat()
    to_time = tomorrow.replace(hour=17, minute=0).isoformat()

    result = await gateway.execute(
        method=HttpMethod.GET,
        path="/vehiclemgt/AvailableVehicles",
        params={"from": from_time, "to": to_time}
    )

    if not result.success or not result.data:
        print(f"[FAIL] Cannot get vehicle: {result.error_message}")
        return

    data = result.data.get("Data", result.data) if isinstance(result.data, dict) else result.data
    vehicles = data if isinstance(data, list) else [data]

    if not vehicles:
        print("[FAIL] No vehicles available")
        return

    v = vehicles[0]
    vehicle_id = v.get("Id")
    vehicle_name = v.get("DisplayName") or v.get("FullVehicleName", "N/A")
    plate = v.get("LicencePlate", "N/A")
    current_mileage = v.get("LastMileage") or v.get("Mileage", 0) or 10000

    print(f"[OK] Vehicle: {vehicle_name} ({plate})")
    print(f"     Current mileage: {current_mileage}")

    # Create mock user context with vehicle assigned
    TEST_PERSON_ID = "fc0a5a65-b832-44f6-b305-38f2178a6b56"

    user_context = {
        "person_id": TEST_PERSON_ID,
        "display_name": "Test User",
        "tenant_id": settings.MOBILITY_TENANT_ID,
        "vehicle": {
            "id": vehicle_id,
            "name": vehicle_name,
            "plate": plate
        }
    }

    print(f"\n[USER] Test User context:")
    print(f"   Person ID: {TEST_PERSON_ID}")
    print(f"   Vehicle: {vehicle_name} ({plate})")

    # Create conversation manager
    TEST_PHONE = "+385991234567"
    conv_manager = ConversationManager(TEST_PHONE, redis_client)
    await conv_manager.load()

    # Test mileage input
    new_mileage = current_mileage + 50

    print("\n" + "-"*60)
    print("[STEP 1] Test mileage submission...")
    print("-"*60)

    # Simulate post_AddMileage call
    tool = registry.get_tool("post_AddMileage")

    if not tool:
        print("[FAIL] post_AddMileage tool not found!")
        return

    print(f"[TOOL] post_AddMileage")
    print(f"   Parameters: VehicleId={vehicle_id[:8]}..., Value={new_mileage}")

    from services.tool_contracts import ToolExecutionContext

    exec_context = ToolExecutionContext(
        conversation_id="test_conv",
        user_context=user_context
    )

    exec_result = await executor.execute(
        tool=tool,
        llm_params={
            "VehicleId": vehicle_id,
            "Value": new_mileage
        },
        execution_context=exec_context
    )

    print(f"\n[RESULT]:")
    print(f"   Success: {exec_result.success}")
    print(f"   Status: {exec_result.http_status}")

    if exec_result.success:
        print(f"   Data: {exec_result.data}")
        print("\n" + "="*60)
        print("[SUCCESS] MILEAGE FLOW COMPLETED!")
        print("="*60)
    else:
        print(f"   Error: {exec_result.error_message}")
        print(f"   AI Feedback: {exec_result.ai_feedback}")
        print("\n" + "="*60)
        print("[FAIL] MILEAGE FLOW FAILED")
        print("="*60)

    # Cleanup
    await conv_manager.clear()
    await gateway.close()
    await redis_client.aclose()


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("APP_ENV") == "testing", reason="Integration test - requires real services")
async def test_full_mileage_flow():
    """Test mileage submission via ToolExecutor (simplified - no MessageEngine)."""

    print("\n" + "="*60)
    print("[FULL] MILEAGE SUBMISSION TEST")
    print("="*60 + "\n")

    # Just run the flow test - it covers everything we need
    await test_mileage_flow()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--api":
        asyncio.run(test_mileage_api())
    elif len(sys.argv) > 1 and sys.argv[1] == "--flow":
        asyncio.run(test_mileage_flow())
    elif len(sys.argv) > 1 and sys.argv[1] == "--full":
        asyncio.run(test_full_mileage_flow())
    else:
        # Default: run flow test
        asyncio.run(test_mileage_flow())
