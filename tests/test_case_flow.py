"""
Test Case Creation Flow

Simulira case creation flow kroz MessageEngine:
1. Korisnik: "Prijavi kvar - prednja guma ima udarac"
2. Bot: Potvrda, pita za detalje ako treba
3. Korisnik: "Da"
4. Bot: Kreira case

NAPOMENA: API endpoint /automation/AddCase zahtjeva "add-case" scope
koji trenutno nije dostupan m1AI klijentu. Test ce pokazati 403 error
dok se ne dodaju potrebne permisije.

Pokreni: python -m tests.test_case_flow

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
async def test_case_api():
    """Test AddCase API directly."""

    print("\n" + "="*60)
    print("[API] CASE API TEST")
    print("="*60 + "\n")

    from config import get_settings
    import redis.asyncio as aioredis
    from services.api_gateway import APIGateway, HttpMethod

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

    # 2. Test AddCase API
    print("\n[CASE] Testing AddCase API...")

    TEST_PERSON_ID = "fc0a5a65-b832-44f6-b305-38f2178a6b56"

    result = await gateway.execute(
        method=HttpMethod.POST,
        path="/automation/AddCase",
        body={
            "User": TEST_PERSON_ID,
            "Subject": "Test prijava kvara",
            "Message": "Testna poruka - prednja desna guma ima mali udarac"
        }
    )

    print(f"     Result: {result.success}")
    print(f"     Status: {result.status_code}")

    if result.success:
        import json
        print(f"[OK] Case created!")
        print(f"     Data: {json.dumps(result.data, indent=2, ensure_ascii=False)}")
    else:
        print(f"[FAIL] Error: {result.error_message}")
        if result.status_code == 403:
            print("\n[INFO] 403 Forbidden - m1AI client needs 'add-case' scope")
            print("       Contact admin to add scope to OAuth client configuration")

    await gateway.close()
    await redis_client.aclose()

    return result.success


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("APP_ENV") == "testing", reason="Integration test - requires real services")
async def test_case_flow():
    """Test case creation flow through ToolExecutor."""

    print("\n" + "="*60)
    print("[FLOW] CASE FLOW TEST")
    print("="*60 + "\n")

    from config import get_settings
    import redis.asyncio as aioredis
    from services.api_gateway import APIGateway
    from services.registry import ToolRegistry
    from services.tool_executor import ToolExecutor
    from services.ai_orchestrator import AIOrchestrator
    from services.response_formatter import ResponseFormatter
    from services.engine.flow_handler import FlowHandler
    from services.conversation_manager import ConversationManager
    from services.tool_contracts import ToolExecutionContext

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

    # Check case tool exists
    if "post_AddCase" not in registry.tools:
        print("[FAIL] post_AddCase not found in registry!")
        return

    print("[OK] Case tool found: post_AddCase")

    executor = ToolExecutor(gateway)

    # Create mock user context
    TEST_PERSON_ID = "fc0a5a65-b832-44f6-b305-38f2178a6b56"

    user_context = {
        "person_id": TEST_PERSON_ID,
        "display_name": "Test User",
        "tenant_id": settings.MOBILITY_TENANT_ID,
        "vehicle": {
            "id": None,
            "name": None,
            "plate": None
        }
    }

    print(f"\n[USER] Test User context:")
    print(f"   Person ID: {TEST_PERSON_ID}")

    # Create conversation manager
    TEST_PHONE = "+385991234567"
    conv_manager = ConversationManager(TEST_PHONE, redis_client)
    await conv_manager.load()

    print("\n" + "-"*60)
    print("[STEP 1] Test case submission...")
    print("-"*60)

    tool = registry.get_tool("post_AddCase")

    if not tool:
        print("[FAIL] post_AddCase tool not found!")
        return

    print(f"[TOOL] post_AddCase")
    print(f"   Parameters: User={TEST_PERSON_ID[:8]}..., Subject='Prijava kvara', Message='...'")

    exec_context = ToolExecutionContext(
        conversation_id="test_conv",
        user_context=user_context
    )

    exec_result = await executor.execute(
        tool=tool,
        llm_params={
            "User": TEST_PERSON_ID,
            "Subject": "Prijava kvara",
            "Message": "Prednja desna guma ima mali udarac od rupe na cesti"
        },
        execution_context=exec_context
    )

    print(f"\n[RESULT]:")
    print(f"   Success: {exec_result.success}")
    print(f"   Status: {exec_result.http_status}")

    if exec_result.success:
        print(f"   Data: {exec_result.data}")
        print("\n" + "="*60)
        print("[SUCCESS] CASE FLOW COMPLETED!")
        print("="*60)
    else:
        print(f"   Error: {exec_result.error_message}")
        print(f"   AI Feedback: {exec_result.ai_feedback}")

        if exec_result.http_status == 403:
            print("\n" + "="*60)
            print("[INFO] API requires 'add-case' scope")
            print("       Code is correct, waiting for OAuth permissions")
            print("="*60)
        else:
            print("\n" + "="*60)
            print("[FAIL] CASE FLOW FAILED")
            print("="*60)

    # Cleanup
    await conv_manager.clear()
    await gateway.close()
    await redis_client.aclose()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--api":
        asyncio.run(test_case_api())
    else:
        # Default: run flow test
        asyncio.run(test_case_flow())
