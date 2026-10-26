"""The look-alike check (S17): does an invented face resemble a well-known person?

A port with a fake, like every external call (Part 7 rule 6). The real adapter
is AWS Rekognition's celebrity recognition, about $0.001 an image, used only
when its key is set. With no vendor the check **reports that it did not run**
(`None`) — the avatar is saved as "not checked", never shown as clear.

The fake answers "clear" unless `FAKE_LIKENESS_MATCH` is set, which is how the
tests and the Playwright run reach the refusal path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings

from ai.providers.switches import uses_fake
from common.exceptions import ProviderError

#: Rekognition's own confidence, 0-100, above which a face counts as a match.
MATCH_CONFIDENCE = 90.0


@dataclass(frozen=True)
class LikenessResult:
    match: bool
    provider: str
    #: The vendor's name for the person matched — kept for the audit, never
    #: shown to the user.
    name: str = ""


class LikenessChecker(Protocol):
    def check(self, image: bytes) -> LikenessResult: ...


class FakeLikenessChecker:
    def check(self, image: bytes) -> LikenessResult:
        if getattr(settings, "FAKE_LIKENESS_MATCH", False):
            return LikenessResult(match=True, provider="fake", name="A famous person")
        return LikenessResult(match=False, provider="fake")


class RekognitionLikenessChecker:
    def __init__(self) -> None:
        import boto3  # type: ignore[import-untyped]

        self.client: Any = boto3.client(
            "rekognition",
            region_name=settings.LIKENESS_AWS_REGION,
            aws_access_key_id=settings.LIKENESS_AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.LIKENESS_AWS_SECRET_ACCESS_KEY,
        )

    def check(self, image: bytes) -> LikenessResult:
        try:
            answer = self.client.recognize_celebrities(Image={"Bytes": image})
        except Exception as exc:  # botocore raises its own tree; the port speaks ours
            raise ProviderError(
                "The look-alike check did not answer.", detail={"provider": "rekognition"}
            ) from exc
        faces = [
            face
            for face in answer.get("CelebrityFaces", [])
            if float(face.get("MatchConfidence", 0)) >= MATCH_CONFIDENCE
        ]
        return LikenessResult(
            match=bool(faces),
            provider="rekognition",
            name=str(faces[0].get("Name", "")) if faces else "",
        )


def get_likeness_checker() -> LikenessChecker | None:
    if uses_fake("LIKENESS"):
        return FakeLikenessChecker()
    if not getattr(settings, "LIKENESS_AWS_ACCESS_KEY_ID", ""):
        return None
    return RekognitionLikenessChecker()
