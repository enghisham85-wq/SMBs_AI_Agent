"""The shared watchable-surface vocabulary — Python copy.

The parts of a customer's app a Boosthis kit can attach to, named the same way
in every language so "we are not watching your background jobs" means the same
thing whether it came from this kit or the Node one.

The list is a copy. The one true list is ``lib/watchable-surfaces.json`` at the
repo root, which also carries the plain-English meaning of each name; the guard
in ``scripts/src/__tests__/watchableSurfaces.test.ts`` fails the build if this
copy and that list ever disagree.

Two rules this file exists to enforce:

1. The customer's own module, class and function names NEVER travel. A kit
   sends a term from this list and a count. Every word a human reads is
   written on the server.
2. A surface is reported as unwatched only when the kit POSITIVELY observed it
   present. Silence means we did not find it, and silence is never rendered as
   a clean bill of health.
"""

from __future__ import annotations

from typing import Tuple

#: Every part of an app a kit may name. Sorted, so a diff against the shared
#: list is readable.
WATCHABLE_SURFACES: Tuple[str, ...] = (
    "background-jobs",
    "database-work",
    "non-http-entry-points",
    "outbound-calls",
    "request-handling",
    "response-caching",
    "serverless-handlers",
)

#: Why a surface this kit found is not being watched. Codes only — the reason a
#: developer reads is written on the server, from this code plus the runtime,
#: so kit-authored prose never reaches a customer.
SURFACE_GAP_REASONS: Tuple[str, ...] = (
    "attach-refused",
    "host-cannot-expose",
    "no-adapter-yet",
    "not-wrapped",
)
