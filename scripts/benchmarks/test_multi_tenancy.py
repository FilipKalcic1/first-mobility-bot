"""
Multi-Tenancy Test Suite

Tests dynamic tenant resolution and routing.

TENANT RESOLUTION:
- Tenant ID is discovered from MobilityOne Persons API (TenantId field)
- Stored per-user in UserMapping.tenant_id
- Default tenant from env used for initial API bootstrap only

TESTS:
1. UserMapping tenant priority
2. Default tenant fallback
3. Tenant in API calls
4. End-to-end tenant flow
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@dataclass
class MockUserMapping:
    """Mock UserMapping for testing."""
    phone_number: str
    api_identity: str
    display_name: str
    tenant_id: str = None
    is_active: bool = True


async def test_user_mapping_tenant_priority():
    """Test 1: UserMapping tenant_id takes priority over default."""
    print("\n" + "=" * 60)
    print("TEST 1: UserMapping Tenant Priority")
    print("=" * 60)

    from services.tenant_service import TenantService

    service = TenantService()

    # User with API-discovered tenant
    user_with_tenant = MockUserMapping(
        phone_number="+385955087196",
        api_identity="person-123",
        display_name="Test User",
        tenant_id="company-abc-uuid"
    )

    # User without tenant (should fall back to default)
    user_without_tenant = MockUserMapping(
        phone_number="+385955087196",
        api_identity="person-456",
        display_name="Test User 2",
        tenant_id=None
    )

    # Test 1: User with tenant_id from API
    result1 = await service.get_tenant_for_user(
        phone="+385955087196",
        user_mapping=user_with_tenant
    )
    status1 = "PASS" if result1 == "company-abc-uuid" else "FAIL"
    print(f"  {status1}: User with API tenant -> {result1} (expected: company-abc-uuid)")

    # Test 2: User without tenant_id -> default
    result2 = await service.get_tenant_for_user(
        phone="+385955087196",
        user_mapping=user_without_tenant
    )
    status2 = "PASS" if result2 == service.default_tenant else "FAIL"
    print(f"  {status2}: User without tenant -> {result2} (expected: {service.default_tenant})")

    # Test 3: No user_mapping -> default
    result3 = await service.get_tenant_for_user(
        phone="+12025551234",
        user_mapping=None
    )
    status3 = "PASS" if result3 == service.default_tenant else "FAIL"
    print(f"  {status3}: No user mapping -> {result3} (expected: {service.default_tenant})")

    passed = sum(1 for s in [status1, status2, status3] if s == "PASS")
    print(f"\nResult: {passed}/3 passed")
    return passed == 3


async def test_api_gateway_uses_tenant():
    """Test 2: APIGateway uses tenant_id in x-tenant header."""
    print("\n" + "=" * 60)
    print("TEST 2: APIGateway x-tenant Header Logic")
    print("=" * 60)

    import inspect
    from services.api_gateway import APIGateway

    source = inspect.getsource(APIGateway.execute)

    has_tenant_param = "tenant_id" in source
    status1 = "PASS" if has_tenant_param else "FAIL"
    print(f"  {status1}: execute() accepts tenant_id parameter")

    uses_effective_tenant = "effective_tenant = tenant_id or self.tenant_id" in source
    status2 = "PASS" if uses_effective_tenant else "FAIL"
    print(f"  {status2}: Uses effective_tenant = tenant_id or self.tenant_id")

    sets_xtenant_header = 'request_headers["x-tenant"] = effective_tenant' in source
    status3 = "PASS" if sets_xtenant_header else "FAIL"
    print(f"  {status3}: Sets request_headers['x-tenant'] = effective_tenant")

    from services.tool_executor import ToolExecutor
    executor_source = inspect.getsource(ToolExecutor._make_http_call)

    passes_tenant = "tenant_id=tenant_id" in executor_source
    status4 = "PASS" if passes_tenant else "FAIL"
    print(f"  {status4}: ToolExecutor passes tenant_id to gateway")

    passed = sum(1 for s in [status1, status2, status3, status4] if s == "PASS")
    print(f"\nResult: {passed}/4 passed")
    return passed == 4


async def test_full_flow_simulation():
    """Test 3: Simulate full message processing flow with API-discovered tenant."""
    print("\n" + "=" * 60)
    print("TEST 3: Full Flow Simulation (API-Discovered Tenant)")
    print("=" * 60)

    print("\n  Simulating message flow:")
    print("  ========================")

    phone = "+385955087196"
    message = "Kolika je moja kilometraza?"
    print(f"  1. Received: '{message}' from {phone[-4:]}...")

    from services.tenant_service import TenantService
    tenant_service = TenantService()
    default_tenant = tenant_service.get_default_tenant()
    print(f"  2. Initial API call uses default tenant: {default_tenant}")

    # Simulated Persons API response with TenantId
    api_response_tenant = "company-xyz-tenant-uuid"
    print(f"  3. Persons API returned TenantId: {api_response_tenant}")

    user = MockUserMapping(
        phone_number=phone,
        api_identity="person-abc-123",
        display_name="Filip Kalcic",
        tenant_id=api_response_tenant
    )
    print(f"  4. UserMapping stored: tenant={user.tenant_id}")

    resolved = await tenant_service.get_tenant_for_user(phone, user)
    print(f"  5. Subsequent calls use API-discovered tenant: {resolved}")

    expected_header = {"x-tenant": api_response_tenant}
    print(f"  6. API call header: {expected_header}")

    flow_correct = resolved == api_response_tenant
    status = "PASS" if flow_correct else "FAIL"
    print(f"\n  Flow verification: {status}")
    return flow_correct


async def run_all_tests():
    """Run all multi-tenancy tests."""
    print("\n" + "=" * 60)
    print("MULTI-TENANCY TEST SUITE")
    print("=" * 60)

    results = []
    results.append(("UserMapping Tenant Priority", await test_user_mapping_tenant_priority()))
    results.append(("APIGateway x-tenant Header", await test_api_gateway_uses_tenant()))
    results.append(("Full Flow Simulation", await test_full_flow_simulation()))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = 0
    failed = 0
    for name, result in results:
        status = "PASS" if result else "FAIL"
        if result:
            passed += 1
        else:
            failed += 1
        print(f"  {status}: {name}")

    print(f"\nTotal: {passed}/{len(results)} tests passed")

    if failed == 0:
        print("\nTenant Resolution Order:")
        print("  1. UserMapping.tenant_id (API-discovered from Persons API TenantId)")
        print("  2. Default tenant from MOBILITY_TENANT_ID env var (bootstrap only)")
        print("\nAPI Call Flow:")
        print("  Message -> Persons API (discover TenantId) -> Store in DB")
        print("  -> ToolExecutor -> APIGateway -> x-tenant header (per-user)")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
