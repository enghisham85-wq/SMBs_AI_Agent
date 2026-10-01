"""WHERE THIS PROJECT RUNS — recognition, not guesswork.

Every hosting platform publishes something about itself to the code running on
it, and most publish a region and an environment with it. This module reads
only those declarations and turns them into three words from three fixed
lists.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
  - It reads no other configuration and sends none. The markers below are the
    only variables it looks at, and it sends the WORD it recognised, never the
    value it read. Nothing free-form leaves the process.
  - It never guesses a host from a similar one. Something unfamiliar declaring
    itself answers "unrecognised", which is a real answer, and an honest one.
  - It never reads an environment as "production" on a hunch. A preview's
    numbers being mistaken for the real app's is precisely what this exists to
    prevent, so a host that declares nothing answers "unknown".

Verbatim port of ``lib/boosthis-runtime-node/src/hosting.ts``. KEEP IN LOCKSTEP
with the closed lists in ``artifacts/api-server/src/lib/hostingPlatforms.ts`` —
the server checks every arriving word against them and stores anything it does
not hold as "unrecognised". A guard test on the server side fails if the lists
drift.
"""

from __future__ import annotations

import os
from typing import Dict, Mapping, Optional

__all__ = [
    "HOSTING_LABELS",
    "ENVIRONMENT_LABELS",
    "detect_hosting",
    "hosting_facts",
    "hosting_line",
    "reset_hosting_cache_for_tests",
]


# The status page cannot ask Boosthis to translate these words: it is most
# useful precisely when this process cannot reach Boosthis at all. Keep this
# copy byte-equal to the Node kit so the same host never gets two names.
HOSTING_LABELS: Dict[str, str] = {
    "vercel": "Vercel Functions",
    "aws-lambda": "AWS Lambda",
    "netlify": "Netlify Functions",
    "gcp-functions": "Google Cloud Functions",
    "azure-functions": "Azure Functions",
    "cloud-run": "Google Cloud Run",
    "azure-app-service": "Azure App Service",
    "aws-ecs": "AWS ECS",
    "fly-io": "Fly.io",
    "render": "Render",
    "railway": "Railway",
    "heroku": "Heroku",
    "replit": "Replit",
    "koyeb": "Koyeb",
    "kubernetes": "Kubernetes",
    "unrecognised": "Unrecognised host",
    "undeclared": "No hosting platform declared",
}

ENVIRONMENT_LABELS: Dict[str, str] = {
    "production": "Production",
    "preview": "Preview",
    "development": "Development",
    "unknown": "Environment not declared",
}


def _env(name: str) -> str:
    """Read one environment variable. Never raises — a hostile ``os.environ``
    replacement must not take a customer's app down."""
    try:
        v = os.environ.get(name)
        return v.strip() if isinstance(v, str) else ""
    except Exception:
        return ""


# Nothing published a host: a laptop, a plain virtual machine, a server
# somebody owns. A real answer, not a blank.
_NOTHING_DECLARED: Dict[str, str] = {
    "platform": "undeclared",
    "region": "undeclared",
    "environment": "unknown",
}


def _vercel_environment() -> str:
    """Vercel's own word for the deployment, straight through — production /
    preview / development are exactly the three it publishes."""
    v = _env("VERCEL_ENV").lower()
    if v in ("production", "preview", "development"):
        return v
    return "unknown"


def _netlify_environment() -> str:
    """Netlify publishes a build CONTEXT. Deploy previews and branch deploys
    are both previews; its "dev" is a local run, which is development, not a
    preview — mapping it to "preview" would be the guess this avoids."""
    c = _env("CONTEXT").lower()
    if c == "production":
        return "production"
    if c in ("deploy-preview", "branch-deploy"):
        return "preview"
    if c == "dev":
        return "development"
    return "unknown"


def detect_hosting() -> Dict[str, str]:
    """Recognise the platform, its region and its environment from what the
    platform publishes about itself. Pure, cheap, and never raises.

    ORDER MATTERS. Several of these hosts run ON another one and set its
    markers too: Vercel's Python functions run on Lambda, Netlify's do too, and
    Google's Cloud Functions run on the same container service as Cloud Run.
    The more specific host is tested first every time, or a Vercel project
    would file itself as raw AWS.
    """
    try:
        # ── Function hosts ──

        # Vercel before Lambda: its functions carry the Lambda markers as well.
        if _env("VERCEL") == "1" or _env("VERCEL_ENV"):
            return {
                "platform": "vercel",
                "region": _env("VERCEL_REGION") or "undeclared",
                "environment": _vercel_environment(),
            }

        # Netlify before Lambda, for the same reason.
        if _env("NETLIFY") == "true" or _env("NETLIFY_LOCAL") or _env("SITE_ID"):
            return {
                "platform": "netlify",
                "region": _env("AWS_REGION") or "undeclared",
                "environment": _netlify_environment(),
            }

        if _env("AWS_LAMBDA_FUNCTION_NAME"):
            return {
                "platform": "aws-lambda",
                "region": _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "undeclared",
                # Lambda has no environment concept of its own — a stage lives
                # in the function name, which we neither read nor send.
                # "unknown" is the honest answer; calling every Lambda
                # "production" would be a guess.
                "environment": "unknown",
            }

        # Google Cloud Functions before Cloud Run: a function sets BOTH the
        # function marker and Cloud Run's service marker.
        if _env("FUNCTION_TARGET"):
            return {
                "platform": "gcp-functions",
                "region": _env("FUNCTION_REGION") or "undeclared",
                "environment": "unknown",
            }

        # Azure Functions before App Service, same overlap.
        if _env("FUNCTIONS_WORKER_RUNTIME"):
            return {
                "platform": "azure-functions",
                "region": _env("REGION_NAME") or "undeclared",
                "environment": "unknown",
            }

        # ── Long-running container and application hosts ──

        if _env("K_SERVICE"):
            return {
                "platform": "cloud-run",
                # Cloud Run publishes its region only through its metadata
                # service, which is a network call this must never make.
                "region": "undeclared",
                "environment": "unknown",
            }

        if _env("WEBSITE_SITE_NAME"):
            return {
                "platform": "azure-app-service",
                "region": _env("REGION_NAME") or "undeclared",
                "environment": "unknown",
            }

        if _env("FLY_APP_NAME"):
            return {
                "platform": "fly-io",
                "region": _env("FLY_REGION") or "undeclared",
                "environment": "unknown",
            }

        if _env("RENDER") == "true" or _env("RENDER_SERVICE_ID"):
            return {
                "platform": "render",
                "region": "undeclared",
                # Render's own marker for a pull-request preview service.
                # Absent means an ordinary service, which Render only ever runs
                # as production.
                "environment": "preview" if _env("IS_PULL_REQUEST") == "true" else "production",
            }

        if _env("RAILWAY_ENVIRONMENT") or _env("RAILWAY_PROJECT_ID"):
            # Railway lets a project have any number of named environments and
            # calls the live one "production". Every other name is one of the
            # throwaway environments people spin up per branch, so it reads as
            # a preview — never silently as production.
            named = (_env("RAILWAY_ENVIRONMENT_NAME") or _env("RAILWAY_ENVIRONMENT")).lower()
            if not named:
                environment = "unknown"
            elif named == "production":
                environment = "production"
            else:
                environment = "preview"
            return {
                "platform": "railway",
                "region": _env("RAILWAY_REPLICA_REGION") or "undeclared",
                "environment": environment,
            }

        if _env("KOYEB_APP_NAME"):
            return {
                "platform": "koyeb",
                "region": _env("KOYEB_REGION") or "undeclared",
                "environment": "unknown",
            }

        if _env("REPL_ID") or _env("REPLIT_DEPLOYMENT"):
            return {
                "platform": "replit",
                "region": "undeclared",
                # Replit publishes exactly this distinction: a published app
                # sets the marker, the workspace copy does not. The workspace
                # copy is a development run, not a preview of one.
                "environment": "production" if _env("REPLIT_DEPLOYMENT") == "1" else "development",
            }

        # ECS before Heroku and Kubernetes: a container on Fargate can carry a
        # scheduler's markers too, and its own is the specific one.
        if _env("ECS_CONTAINER_METADATA_URI_V4") or _env("ECS_CONTAINER_METADATA_URI"):
            return {
                "platform": "aws-ecs",
                "region": _env("AWS_REGION") or _env("AWS_DEFAULT_REGION") or "undeclared",
                "environment": "unknown",
            }

        if _env("DYNO"):
            return {
                "platform": "heroku",
                "region": "undeclared",
                "environment": "unknown",
            }

        # Last of the recognised hosts on purpose. Kubernetes is underneath a
        # great many of the platforms above, so it is only the answer once none
        # of them has claimed the process — and it is a genuine answer: we know
        # it is a cluster, and we deliberately do not guess which cloud is
        # under it.
        if _env("KUBERNETES_SERVICE_HOST"):
            return {
                "platform": "kubernetes",
                "region": "undeclared",
                "environment": "unknown",
            }

        return dict(_NOTHING_DECLARED)
    except Exception:
        # Reading the environment is the only thing that can fail here, and a
        # project's readings must never be lost over a question about where it
        # runs. Answer "we could not tell" and carry on.
        return dict(_NOTHING_DECLARED)


_cached: Optional[Dict[str, str]] = None


def hosting_facts() -> Dict[str, str]:
    """The detection, read once per process.

    Caching is safe and correct here: a process cannot move between platforms
    while it is running. A project that MOVES gets a new process, which detects
    afresh and re-registers, which is exactly how the record stops describing
    where the project used to run.
    """
    global _cached
    if _cached is None:
        _cached = detect_hosting()
    return dict(_cached)


def hosting_line(facts: Optional[Mapping[str, str]] = None) -> str:
    """One status-page line in the same words and order as the dashboard.

    The environment belongs in the line only when it warns that these are not
    production numbers. Printing "not declared" on every ordinary healthy
    install would bury the preview warning this line exists to make obvious.
    """
    if facts is None:
        facts = hosting_facts()
    platform = HOSTING_LABELS.get(
        facts.get("platform", ""), HOSTING_LABELS["unrecognised"]
    )
    parts = [platform]
    environment = facts.get("environment")
    if environment in ("preview", "development"):
        parts.append(ENVIRONMENT_LABELS[environment])
    region = facts.get("region")
    if region and region != "undeclared":
        parts.append(region)
    return " · ".join(parts)


def reset_hosting_cache_for_tests() -> None:
    """Test seam. Never called in production."""
    global _cached
    _cached = None
