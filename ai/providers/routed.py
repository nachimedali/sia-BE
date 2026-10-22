"""Ports bound to a route (`ai.services.routing`).

`get_text_provider(task, workspace=…)` and its image and video siblings hand
back one of these instead of the bare adapter. Callers keep calling the port
as before; the wrapper fills in the model the route names, the route's
parameters, and the one fallback attempt — so the 15 call sites change by one
argument each, and none of them reads a setting or the route table.

**A route that came from settings passes `model=None`**, exactly what every
caller passed before the table existed: an empty table is the previous
behaviour, call for call. The same holds on the fakes, so a test that never
seeded a route sees the fake's own model names.

What actually ran is what the adapter reports back (`result.model`), so
`Generation.model` stays true when the fallback answered.
"""

from __future__ import annotations

from typing import Any

from ai.services.routing import SOURCE_SETTINGS, Route, call_with_fallback


def _call(route: Route, explicit: str | None, call: Any) -> Any:
    if explicit:
        # A caller naming its own model (an eval harness, a test) is obeyed and
        # not second-guessed with a fallback it did not ask for.
        return call(explicit)
    if route.source == SOURCE_SETTINGS:
        # No row: send no model, exactly as before the table existed.
        return call(None)
    return call_with_fallback(route, call)


class RoutedTextProvider:
    def __init__(self, inner: Any, route: Route) -> None:
        self.inner = inner
        self.route = route

    def generate(self, *, system: str, prompt: str, n: int, model: str | None = None) -> Any:
        return _call(
            self.route,
            model,
            lambda chosen: self.inner.generate(system=system, prompt=prompt, n=n, model=chosen),
        )

    def caption(
        self, *, system: str, prompt: str, image_bytes: bytes, n: int, model: str | None = None
    ) -> Any:
        return _call(
            self.route,
            model,
            lambda chosen: self.inner.caption(
                system=system, prompt=prompt, image_bytes=image_bytes, n=n, model=chosen
            ),
        )

    def classify_constraints(
        self, *, image_bytes: bytes, restrictions: list[str], model: str | None = None
    ) -> list[str]:
        result: list[str] = _call(
            self.route,
            model,
            lambda chosen: self.inner.classify_constraints(
                image_bytes=image_bytes, restrictions=restrictions, model=chosen
            ),
        )
        return result


class RoutedImageProvider:
    def __init__(self, inner: Any, route: Route) -> None:
        self.inner = inner
        self.route = route

    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        n: int,
        batch: bool,
        model: str | None = None,
        style: dict[str, float] | None = None,
        image_size: str | None = None,
    ) -> Any:
        params = self.route.params
        # The route may switch a task's batch mode (`batch` param); without
        # one, the caller's choice stands (autopilot asks for batch, D8).
        use_batch = bool(params["batch"]) if "batch" in params else batch
        size = image_size or params.get("image_size")
        return _call(
            self.route,
            model,
            lambda chosen: self.inner.generate(
                prompt=prompt,
                reference_images=reference_images,
                aspect=aspect,
                n=n,
                batch=use_batch,
                model=chosen,
                style=style,
                image_size=size,
            ),
        )


class RoutedVideoProvider:
    """Resolves per call: the same workspace animates towards a first frame
    and towards a last frame on different models (`video_start`/`video_end`)."""

    def __init__(self, inner: Any, *, workspace: Any) -> None:
        self.inner = inner
        self.workspace = workspace

    def _send(
        self,
        task: str,
        method: str,
        model: str | None,
        resolution: str | None,
        audio: bool | None,
        **kwargs: Any,
    ) -> Any:
        from ai.services.routing import resolve_for

        route = resolve_for(task, self.workspace)
        kwargs["resolution"] = resolution or route.params.get("resolution")
        kwargs["audio"] = audio if audio is not None else route.params.get("audio")
        return _call(
            route, model, lambda chosen: getattr(self.inner, method)(model=chosen, **kwargs)
        )

    def generate(
        self,
        *,
        prompt: str,
        reference_images: list[bytes],
        aspect: str,
        duration_seconds: float,
        model: str | None = None,
        resolution: str | None = None,
        audio: bool | None = None,
    ) -> Any:
        from ai.models import AITask

        return self._send(
            AITask.VIDEO_START,
            "generate",
            model,
            resolution,
            audio,
            prompt=prompt,
            reference_images=reference_images,
            aspect=aspect,
            duration_seconds=duration_seconds,
        )

    def animate(
        self,
        *,
        image: bytes,
        role: str,
        prompt: str,
        aspect: str,
        duration_seconds: float,
        model: str | None = None,
        resolution: str | None = None,
        audio: bool | None = None,
    ) -> Any:
        from ai.models import AITask

        return self._send(
            AITask.VIDEO_END if role == "end" else AITask.VIDEO_START,
            "animate",
            model,
            resolution,
            audio,
            image=image,
            role=role,
            prompt=prompt,
            aspect=aspect,
            duration_seconds=duration_seconds,
        )
