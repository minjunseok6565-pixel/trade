from __future__ import annotations

import hashlib
import json

from ...models import Deal, canonicalize_deal, serialize_deal

# =============================================================================
# Dedupe / misc
# =============================================================================

def deal_signature_payload(deal: Deal):
    """Canonical payload used for signature comparisons (includes meta).

    NOTE: sweetener 등에서 '딜이 실제로 변했는지' 비교 용도로 사용.
    """
    try:
        return serialize_deal(canonicalize_deal(deal))
    except Exception:
        return repr(deal)

def dedupe_hash(deal: Deal) -> str:
    """Deal identity hash for dedupe.

    IMPORTANT:
    - MUST ignore deal.meta (tags/debug fields) so the same transaction (teams+legs)
      does not survive as duplicates with only meta differences.
    """
    canon = canonicalize_deal(deal)
    payload = serialize_deal(canon)
    # Ignore meta completely for dedupe (A)
    payload.pop("meta", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()

