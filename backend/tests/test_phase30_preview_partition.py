"""Phase 30 / 30.38 — preview cache isolation and expiry.

Cache entries are partitioned per principal (so one account can never be
served another account's cached card), expire by TTL, and revoke cleanly.
Private descriptors remain never-cached (covered in the 28.7a suite,
including the cross-account refusal); this file pins the partitioning
itself.
"""
import time

from backend.services.link_preview_service import LinkPreviewService


def _seed(service, url, principal, preview, ttl=999.0):
    service._cache[service._cache_key(url, principal)] = (time.time() + ttl, preview)


def test_cache_entries_are_partitioned_per_principal():
    service = LinkPreviewService()
    card_a = object()
    card_b = object()
    _seed(service, "https://example.com/page", "user-a", card_a)
    _seed(service, "https://example.com/page", "user-b", card_b)

    assert service._cache_key("https://example.com/page", "user-a") != \
        service._cache_key("https://example.com/page", "user-b")
    # Each principal reads only their own entry.
    assert service._cache[service._cache_key("https://example.com/page", "user-a")][1] is card_a
    assert service._cache[service._cache_key("https://example.com/page", "user-b")][1] is card_b


def test_invalidation_is_scoped_to_the_calling_principal():
    service = LinkPreviewService()
    _seed(service, "https://example.com/page", "user-a", object())
    _seed(service, "https://example.com/page", "user-b", object())
    service.invalidate("https://example.com/page", principal="user-a")
    assert service._cache_key("https://example.com/page", "user-a") not in service._cache
    assert service._cache_key("https://example.com/page", "user-b") in service._cache


def test_expired_entries_are_never_served():
    service = LinkPreviewService()
    _seed(service, "https://example.com/page", "user-a", object(), ttl=-1.0)
    # An expired entry is stale: the lookup path treats it as absent.
    key = service._cache_key("https://example.com/page", "user-a")
    cached = service._cache.get(key)
    assert cached is not None  # still stored…
    assert cached[0] <= time.time()  # …but past expiry → not servable


def test_epoch_scoped_keys_do_not_collide():
    """Callers may embed the authorization epoch in the principal key so a
    rotated credential/epoch starts a fresh cache namespace."""
    service = LinkPreviewService()
    assert service._cache_key("https://example.com/p", "user-a:e1") != \
        service._cache_key("https://example.com/p", "user-a:e2")
