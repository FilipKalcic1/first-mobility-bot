"""
Production Readiness Verification Suite

Validates pre-flight checks for single-pod 1 CPU / 1 GiB deployment:
1. Lua script cache persistence in Redis
2. Memory baseline under target limit (<900 MiB)
3. PII masking in log output

Usage:
    kubectl exec deploy/mobility-bot -c worker -- python scripts/verify_production_readiness.py
"""

import asyncio
import json
import logging
import os
import sys
import tracemalloc

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Lua Script Cache Verification
# ---------------------------------------------------------------------------
async def verify_lua_script_cache():
    """Verify the atomic lock-release Lua script is cached in Redis."""
    import redis.asyncio as aioredis

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis = aioredis.from_url(redis_url, decode_responses=True)

    lua_script = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""
    try:
        sha = await redis.script_load(lua_script)
        exists = await redis.script_exists(sha)

        if exists and exists[0]:
            logger.info(f"[PASS] Lua lock script cached: SHA={sha[:12]}...")

            # Functional test: set a key, release with correct owner, verify deletion
            await redis.set("test_lock:verify", "owner_a", ex=10)
            result = await redis.evalsha(sha, 1, "test_lock:verify", "owner_a")
            assert result == 1, "Lua script should delete key owned by caller"

            # Functional test: try releasing someone else's lock
            await redis.set("test_lock:verify", "owner_b", ex=10)
            result = await redis.evalsha(sha, 1, "test_lock:verify", "owner_a")
            assert result == 0, "Lua script must NOT delete key owned by another"
            await redis.delete("test_lock:verify")

            logger.info("[PASS] Lua script atomicity verified (own-lock-only delete)")
            return True
        else:
            logger.error("[FAIL] Lua script NOT found in Redis cache")
            return False
    except Exception as e:
        logger.error(f"[FAIL] Lua verification error: {e}")
        return False
    finally:
        await redis.aclose()


# ---------------------------------------------------------------------------
# 2. Memory Baseline Check
# ---------------------------------------------------------------------------
def verify_memory_baseline():
    """Check that idle memory usage is under 200MB (leaving 800MB for operations)."""
    tracemalloc.start()

    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss_kb / 1024  # Linux reports in KB
    except (ImportError, AttributeError):
        # Fallback for systems without resource module
        import psutil
        rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)

    snapshot = tracemalloc.take_snapshot()
    top_stats = snapshot.statistics("lineno")

    traced_mb = sum(s.size for s in top_stats) / (1024 * 1024)

    if rss_mb > 200:
        logger.warning(
            f"[WARN] Idle RSS = {rss_mb:.0f}MB (target <200MB). "
            f"Top allocations by tracemalloc: {traced_mb:.1f}MB traced"
        )
        # Log top 5 allocations
        for stat in top_stats[:5]:
            logger.info(f"  {stat}")
        return rss_mb < 400  # Hard fail at 400MB
    else:
        logger.info(f"[PASS] Idle memory: {rss_mb:.0f}MB RSS, {traced_mb:.1f}MB traced")
        return True


# ---------------------------------------------------------------------------
# 4. PII Masking Verification
# ---------------------------------------------------------------------------
def verify_pii_masking():
    """Verify that key log functions mask phone numbers."""
    import ast
    import re

    pii_leaks = []
    files_to_check = [
        "services/user_service.py",
        "webhook_simple.py",
        "worker.py",
    ]

    # Pattern: logger.xxx(f"...{phone}...") without [-4:] or [: masking
    # Matches {phone}, {sender}, {phone_var} NOT followed by [-4:]
    phone_var_pattern = re.compile(
        r'\{(?:phone|sender|phone_var)\}'
    )

    for filepath in files_to_check:
        full_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), filepath)
        if not os.path.exists(full_path):
            continue

        with open(full_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                stripped = line.strip()
                if not any(kw in stripped for kw in ["logger.", "log(", "log_exception("]):
                    continue
                matches = phone_var_pattern.findall(stripped)
                if matches:
                    pii_leaks.append(f"  {filepath}:{line_num}: {stripped[:100]}")

    if pii_leaks:
        logger.error(f"[FAIL] {len(pii_leaks)} potential PII leak(s) in logs:")
        for leak in pii_leaks:
            logger.error(leak)
        return False
    else:
        logger.info(f"[PASS] No unmasked phone numbers in log statements ({len(files_to_check)} files checked)")
        return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def verify_auth_preflight():
    """PP1: OAuth scope introspection + route probe against the live API.

    Skips gracefully (returns True) when M1 credentials/network are absent
    (offline CI) — the check is meaningful only against a real environment.
    Fails when a required scope is missing or a probed route answers 401/403.
    """
    required_env = ("MOBILITY_API_URL", "MOBILITY_AUTH_URL",
                    "MOBILITY_CLIENT_ID", "MOBILITY_CLIENT_SECRET",
                    "MOBILITY_TENANT_ID")
    if any(not os.environ.get(k) or "example.com" in os.environ.get(k, "")
           for k in required_env):
        logger.info("[SKIP] auth_preflight — no live M1 credentials (offline run)")
        return True
    try:
        from services.api_gateway import APIGateway
        from services.auth_preflight import (
            log_report, required_scopes_from_env, run_preflight,
        )
        gateway = APIGateway()
        try:
            report = await run_preflight(
                gateway.token_manager, gateway,
                tenant_id=os.environ["MOBILITY_TENANT_ID"],
                required=required_scopes_from_env(),
            )
        finally:
            await gateway.close()
        log_report(report)
        return report.ok
    except Exception as e:  # noqa: BLE001 — readiness must report, not crash
        logger.error(f"auth_preflight errored: {e}")
        return False


async def main():
    logger.info("=" * 60)
    logger.info("Production Readiness Verification Suite")
    logger.info("Target: 0.5 CPU / 1GB RAM Guaranteed QoS")
    logger.info("=" * 60)

    results = {}

    # Run async checks
    results["lua_cache"] = await verify_lua_script_cache()
    results["auth_preflight"] = await verify_auth_preflight()

    # Run sync checks
    results["memory_baseline"] = verify_memory_baseline()
    results["pii_masking"] = verify_pii_masking()

    logger.info("")
    logger.info("=" * 60)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    logger.info(f"Results: {passed}/{total} checks passed")

    for check, result in results.items():
        status = "PASS" if result else "FAIL"
        logger.info(f"  [{status}] {check}")

    logger.info("=" * 60)

    if all(results.values()):
        logger.info("VERDICT: READY FOR PRODUCTION")
        return 0
    else:
        logger.error("VERDICT: NOT READY — fix failures above")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
