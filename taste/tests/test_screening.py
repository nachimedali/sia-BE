"""Hard-constraint screening — **brand policy, not artifact quality** (C-09, P5-02).

Two layers, and merging them "for simplicity" is the failure this file exists
to guard against:

* the **quality gate** (`ai/services/quality.py`) judges the artifact —
  resolution, aspect, product identity by dHash, is the body non-empty;
* **screening** judges the brand — never mention competitors, no alcohol
  imagery, always include a CTA.

A candidate can be a technically perfect image that violates policy (P5-03),
and a candidate can be on-brand and technically unusable. Neither layer
subsumes the other, and a system with only one of them will ship the wrong
thing for whichever reason it stopped checking.

**Evaluated in code, never by the model.** A policy check the generator is
asked to self-assess is a policy check that fails exactly when the prompt has
drifted — which is the moment it matters.
"""

from __future__ import annotations

import pytest

from taste.services.screening import CONSTRAINT_KINDS, screen

pytestmark = pytest.mark.django_db


class TestTheVocabulary:
    def test_every_declared_kind_is_evaluable(self) -> None:
        # A constraint kind nothing evaluates is a policy the workspace thinks
        # it has — worse than no policy, because it is believed.
        for kind, evaluate in CONSTRAINT_KINDS.items():
            assert callable(evaluate), kind

    def test_an_unknown_constraint_kind_is_refused_not_ignored(self) -> None:
        from rest_framework.exceptions import ValidationError

        from taste.services.screening import validate_constraints

        with pytest.raises(ValidationError):
            validate_constraints({"vibes_must_be_good": True})

    def test_a_declared_kind_validates(self) -> None:
        from taste.services.screening import validate_constraints

        assert validate_constraints({"banned_phrases": ["cheap"]})

    def test_a_wrongly_shaped_value_is_refused(self) -> None:
        from rest_framework.exceptions import ValidationError

        from taste.services.screening import validate_constraints

        with pytest.raises(ValidationError):
            validate_constraints({"banned_phrases": "cheap"})


class TestBannedPhrases:
    def test_a_clean_body_passes(self) -> None:
        result = screen("Our new glaze is here.", {"banned_phrases": ["cheap"]})

        assert result.passed
        assert result.violations == []

    def test_a_banned_phrase_is_caught(self) -> None:
        result = screen("Our cheap new glaze.", {"banned_phrases": ["cheap"]})

        assert not result.passed
        assert "banned_phrases" in result.violations[0]

    def test_matching_ignores_case(self) -> None:
        # "Cheap" and "cheap" are the same policy violation, and a check that
        # only caught one would be trivially evaded by the generator's own
        # capitalisation.
        assert not screen("CHEAP glaze", {"banned_phrases": ["cheap"]}).passed

    def test_a_competitor_name_inside_a_word_is_not_a_false_positive(self) -> None:
        """Word boundaries, not substrings. A brand banning "Ace" should not
        have every "place" and "surface" refused — a screening layer that cries
        wolf gets switched off, which is worse than not having it."""
        assert screen("A lovely surface finish.", {"banned_phrases": ["Ace"]}).passed


class TestRequiredElements:
    def test_a_missing_cta_is_caught(self) -> None:
        result = screen("Just a statement.", {"require_cta": True})

        assert not result.passed

    def test_a_link_counts_as_a_cta(self) -> None:
        assert screen("Read more: https://acme.test/x", {"require_cta": True}).passed

    def test_an_imperative_counts_as_a_cta(self) -> None:
        assert screen("Shop the new collection today.", {"require_cta": True}).passed

    def test_a_required_phrase_must_appear(self) -> None:
        constraints = {"required_phrases": ["#ad"]}

        assert screen("New drop #ad", constraints).passed
        assert not screen("New drop", constraints).passed


class TestStructuralLimits:
    def test_too_many_hashtags_is_caught(self) -> None:
        body = "Launch " + " ".join(f"#tag{index}" for index in range(1, 8))

        assert not screen(body, {"max_hashtags": 5}).passed

    def test_exactly_the_limit_passes(self) -> None:
        body = "Launch " + " ".join(f"#tag{index}" for index in range(1, 6))

        assert screen(body, {"max_hashtags": 5}).passed

    def test_a_banned_topic_is_caught(self) -> None:
        assert not screen("Pair it with a whisky.", {"banned_topics": ["whisky"]}).passed


class TestTheResult:
    def test_no_constraints_means_nothing_to_violate(self) -> None:
        # An empty policy passes everything. A workspace that has not written
        # its rules down has not thereby forbidden everything.
        assert screen("Anything at all.", {}).passed

    def test_every_violation_is_reported_not_just_the_first(self) -> None:
        """The composer shows why a candidate was screened out, and fixing one
        problem only to meet the next is the loop this avoids."""
        result = screen(
            "cheap whisky",
            {"banned_phrases": ["cheap"], "banned_topics": ["whisky"], "require_cta": True},
        )

        assert len(result.violations) == 3

    def test_the_reason_names_the_constraint_that_failed(self) -> None:
        result = screen("cheap", {"banned_phrases": ["cheap"]})

        assert "cheap" in result.reason
