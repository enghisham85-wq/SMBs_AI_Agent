"""Gradio, in one call — the kit's pages AND per-interaction measuring.

    import gradio as gr, boosthis

    with gr.Blocks() as demo:
        ...

    boosthis.mount(demo)
    demo.launch()

WHAT A UNIT OF WORK IS HERE
------------------------------------------------------------------
A Gradio app is not a page a visitor loads; it is a set of event handlers a
visitor triggers. The honest unit is therefore ONE event-handler invocation —
the button press, the slider change, the chat turn — measured end to end,
including the queue-side work Gradio does around it. That is the thing a Gradio
developer waits for, and the thing they would say is slow.

Every interaction is measured at ``Blocks.call_function``, the single funnel
every event goes through, so a generator handler that streams tokens is timed
until it finishes streaming rather than until it returns its first chunk. Each
handler is labelled with its own function name (``gradio.summarize``), which is
code-defined, bounded in number, and never carries a user's input.

Gradio also runs on FastAPI, so the HTTP layer underneath gets the kit's normal
FastAPI treatment — the same routes and the same per-request meters as any other
FastAPI app.

WHY BOTH HALVES ARRIVE AT ``launch()``
------------------------------------------------------------------
The FastAPI app a Gradio demo runs on does not exist until ``launch()`` builds
it. So this adapter arms ONE hook on Gradio's app factory, and when that hook
fires it installs the pages and the interaction measuring together, in a single
step, before the server accepts its first request. If ``launch()`` never
happens, neither half exists — a Gradio demo can never end up with a panel and
no readings, or readings with nowhere to read them.
"""

from __future__ import annotations

from typing import Any, Optional

from . import host_surface
from . import unit_of_work

#: Marker set on the FastAPI app a demo launched on, so relaunching a demo (or
#: mounting twice) cannot install two layers of routes or two timers.
_APP_MARK = "_boosthis_gradio_wired"
#: Marker set on the Blocks whose ``call_function`` is already measured.
_BLOCKS_MARK = "_boosthis_gradio_measured"

SURFACE = "gradio"


def is_gradio_blocks(app: Any) -> bool:
    """True for a ``gr.Blocks`` (or ``gr.Interface``/``ChatInterface``), without
    importing Gradio.

    Gradio's *served app* is a FastAPI subclass and must keep resolving as
    FastAPI; only the Blocks object routes here."""
    try:
        for cls in type(app).__mro__:
            mod = (getattr(cls, "__module__", "") or "").lower()
            name = getattr(cls, "__name__", "")
            if (mod == "gradio.blocks" or mod.startswith("gradio.blocks.")) and name == "Blocks":
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _handler_label(block_fn: Any) -> str:
    """``gradio.summarize`` from the handler's own name."""
    for attr in ("api_name", "name"):
        try:
            value = getattr(block_fn, attr, None)
            if isinstance(value, str) and value and value not in ("false", "None"):
                return unit_of_work.safe_label("gradio.%s" % value, "gradio.interaction")
        except Exception:  # noqa: BLE001
            continue
    try:
        fn = getattr(block_fn, "fn", None)
        fname = getattr(fn, "__name__", None)
        if isinstance(fname, str) and fname and fname != "<lambda>":
            return unit_of_work.safe_label("gradio.%s" % fname, "gradio.interaction")
    except Exception:  # noqa: BLE001
        pass
    return "gradio.interaction"


def _install_interaction_measuring(blocks: Any) -> None:
    """Time every event-handler invocation on this demo.

    Wraps the ONE async funnel (``Blocks.call_function``) on the instance rather
    than each registered handler: handlers may be plain functions, coroutines,
    generators or async generators, and re-wrapping them would change what
    Gradio's own inspection sees. Raises on failure — the caller turns that into
    a reported missing half, and installs no panel either."""
    if getattr(blocks, _BLOCKS_MARK, False):
        return
    original = blocks.call_function
    if not callable(original):
        raise RuntimeError("this Gradio app has no call_function to measure")

    async def measured_call_function(block_fn: Any, *args: Any, **kwargs: Any) -> Any:
        resolved = block_fn
        try:
            if isinstance(block_fn, int):
                resolved = blocks.fns[block_fn]
        except Exception:  # noqa: BLE001
            resolved = block_fn
        unit = unit_of_work.begin(_handler_label(resolved), sample_loop_lag=True)
        try:
            return await original(block_fn, *args, **kwargs)
        finally:
            unit_of_work.end(unit)

    blocks.call_function = measured_call_function
    setattr(blocks, _BLOCKS_MARK, True)


def mount_gradio(blocks: Any, prefix: str, bubble: Optional[bool] = None) -> Any:
    """Arm both halves for a Gradio demo and return it.

    Nothing is installed here; the hook installed below does both halves at
    once when Gradio builds the demo's FastAPI app."""
    wiring = host_surface.begin(SURFACE, unit="interaction")
    wiring.deferred = True

    try:
        from gradio import routes as _gradio_routes  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        reason = "Gradio's routes module could not be imported (%s: %s)" % (
            type(exc).__name__,
            exc,
        )
        wiring.deferred = False
        host_surface.note_panel(wiring, False, reason)
        host_surface.note_measurement(wiring, False, reason)
        host_surface.finish(wiring)
        return blocks

    original_create_app = getattr(_gradio_routes.App, "create_app", None)
    if not callable(original_create_app):
        reason = (
            "this version of Gradio has no App.create_app, so there is no point "
            "at which the kit can attach before the server starts"
        )
        wiring.deferred = False
        host_surface.note_panel(wiring, False, reason)
        host_surface.note_measurement(wiring, False, reason)
        host_surface.finish(wiring)
        return blocks

    def _wire(app: Any) -> None:
        """Both halves, one step, before the first request is served."""
        from .mount import _mount_fastapi

        _install_interaction_measuring(blocks)
        try:
            _mount_fastapi(app, prefix, bubble)
        except Exception:
            # The measuring went in but the pages did not: undo the measuring
            # rather than leave the demo half-wired, and re-raise so the mount
            # is reported honestly.
            try:
                del blocks.call_function
            except Exception:  # noqa: BLE001
                pass
            try:
                setattr(blocks, _BLOCKS_MARK, False)
            except Exception:  # noqa: BLE001
                pass
            raise
        setattr(app, _APP_MARK, True)

    if getattr(original_create_app, "_boosthis_hook", False):
        hooked = original_create_app
    else:
        def hooked(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            app = original_create_app(*args, **kwargs)
            target = args[0] if args else kwargs.get("blocks")
            for pending in list(_PENDING):
                if pending["blocks"] is not target and target is not None:
                    continue
                if getattr(app, _APP_MARK, False):
                    continue
                w = pending["wiring"]
                try:
                    pending["wire"](app)
                    w.deferred = False
                    host_surface.note_panel(w, True)
                    host_surface.note_measurement(w, True)
                except Exception as exc:  # noqa: BLE001
                    w.deferred = False
                    reason = "%s: %s" % (type(exc).__name__, exc)
                    host_surface.note_panel(w, False, reason)
                    host_surface.note_measurement(w, False, reason)
                host_surface.finish(w)
                try:
                    _PENDING.remove(pending)
                except ValueError:  # pragma: no cover - concurrent launches
                    pass
            return app

        hooked._boosthis_hook = True  # type: ignore[attr-defined]
        try:
            _gradio_routes.App.create_app = staticmethod(hooked)
        except Exception as exc:  # noqa: BLE001
            reason = "the kit could not attach to Gradio's app factory (%s: %s)" % (
                type(exc).__name__,
                exc,
            )
            wiring.deferred = False
            host_surface.note_panel(wiring, False, reason)
            host_surface.note_measurement(wiring, False, reason)
            host_surface.finish(wiring)
            return blocks

    _PENDING.append({"blocks": blocks, "wiring": wiring, "wire": _wire})
    host_surface.finish(wiring)
    return blocks


#: Demos whose halves are armed and waiting for ``launch()``.
_PENDING: list = []
