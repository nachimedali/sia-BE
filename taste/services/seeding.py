"""Inferring a first taste profile from published history (P5-04).

**A blank profile at first run is the single largest predictor of early churn**
— the same reasoning that made `Product.is_generation_ready` a gate. Somebody
who must describe their own voice from nothing, before the product has done
anything for them, mostly will not.

So the first profile is *measured* from what they already publish and handed
over as a **draft to correct**, never as a finished answer and never activated
on their behalf. Two rules keep it honest:

* **Counted in code, never guessed by a model.** Every number here comes from
  real posts (Part 7 rule 15's habit, applied a phase early). An LLM asked to
  "describe this brand's voice" would produce something plausible and
  unfalsifiable, which is the worst possible seed for a profile the whole
  attribution loop then hangs off.
* **Policy is never inferred.** That a brand has never mentioned a competitor
  is not evidence that it forbids doing so, and a `hard_constraint` invented
  here would silently screen out real work for a rule nobody wrote.
"""

from __future__ import annotations

import statistics
from typing import Any

from accounts.models import User
from common.text import HASHTAG_RE
from content.models import Post, PostStatus
from taste.models import TasteProfile
from workspaces.models import Workspace

#: How much history to read. Enough to be representative, bounded because a
#: workspace importing years of posts should not make onboarding slow.
SAMPLE_SIZE = 50

#: Unicode ranges that are emoji in practice — pictographs, symbols, transport
#: and the dingbats. Deliberately coarse: the question is "does this brand use
#: emoji at all", and a precise grapheme parser would be a lot of code to
#: answer a yes/no.
_EMOJI_RANGES = (
    (0x1F300, 0x1FAFF),
    (0x2600, 0x27BF),
    (0x1F000, 0x1F2FF),
)


def _has_emoji(text: str) -> bool:
    return any(
        any(low <= ord(character) <= high for low, high in _EMOJI_RANGES) for character in text
    )


def infer_profile_fields(workspace: Workspace) -> dict[str, Any]:
    """What the brand's own posts say about how it writes.

    Reads **published** posts only. A draft is what somebody was considering,
    not what the brand sounds like — inferring voice from abandoned drafts
    would teach the profile the opposite of what actually shipped.

    Returns empty sections with no history rather than plausible defaults: a
    workspace that has not shown us its voice has not shown us its voice, and
    a fabricated brand is something the user would have to notice was wrong.
    """
    bodies = list(
        Post.objects.filter(workspace=workspace, status=PostStatus.PUBLISHED)
        .exclude(master_body="")
        .order_by("-created_at")
        .values_list("master_body", flat=True)[:SAMPLE_SIZE]
    )
    if not bodies:
        return {"sampled_posts": 0, "voice": {}, "structural": {}, "topic_posture": {}}

    # The **median**, not the mean: one outlier post carrying thirty tags
    # should not become the brand's declared style.
    hashtag_counts = [len(HASHTAG_RE.findall(body)) for body in bodies]
    emoji_share = sum(1 for body in bodies if _has_emoji(body)) / len(bodies)

    return {
        "sampled_posts": len(bodies),
        "voice": {
            # A habit, not a rule — half the posts carrying emoji is a brand
            # that uses them, and the user is the one who decides the policy.
            "emoji_policy": "uses_emoji" if emoji_share >= 0.5 else "no_emoji",
        },
        "structural": {
            "hashtag_count": int(statistics.median(hashtag_counts)),
            "median_length": int(statistics.median(len(body) for body in bodies)),
        },
        "topic_posture": {},
    }


def seed_profile(*, workspace: Workspace, created_by: User | None = None) -> TasteProfile:
    """The workspace's first profile, inferred and **left inactive**.

    Inactive on purpose: activating a profile nobody has read would produce
    content nobody recognises, and the correction step is the point (P5-04).
    The completeness score is what tells the user there is still something to
    say — an inferred profile that read as finished would never be corrected.

    Idempotent. Onboarding is resumable, and a resumed step must not mint a
    second draft the user then has to choose between; a workspace that already
    has any profile is left exactly as it is.
    """
    from taste.services.profiles import create_profile

    existing = TasteProfile.objects.filter(workspace=workspace).order_by("version").first()
    if existing is not None:
        return existing

    inferred = infer_profile_fields(workspace)
    return create_profile(
        workspace=workspace,
        created_by=created_by,
        voice=inferred["voice"],
        structural=inferred["structural"],
        topic_posture=inferred["topic_posture"],
    )
