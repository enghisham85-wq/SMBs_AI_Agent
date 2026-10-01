"""Turn an outside hostname into one of six privacy-safe dependency kinds.

The input hostname is used only on this stack and is immediately replaced by a
word from the closed vocabulary below.  No vendor, host, path, query, userinfo,
header, payload, or status code is retained.

In-app auth is detected from ``sys.modules`` because that is the honest read:
it proves the host already loaded a sign-in library without this kit importing,
resolving, or otherwise causing one to exist.
"""
from __future__ import annotations

from typing import Any

DEPENDENCY_KINDS = ("signin", "storage", "payments", "messaging", "search", "other")

# This is the Node table verbatim: (domain, prefix, infix, kind).  A domain is
# matched on a label boundary — the host IS it or sits under it — never as a
# bare suffix, which would read notclerk.com as a sign-in service.  Order
# matters where vendor domains overlap.
KIND_RULES = (
    ("clerk.accounts.dev", None, None, "signin"),
    ("clerk.com", None, None, "signin"),
    ("auth0.com", None, None, "signin"),
    ("okta.com", None, None, "signin"),
    ("oktapreview.com", None, None, "signin"),
    ("okta-emea.com", None, None, "signin"),
    ("identitytoolkit.googleapis.com", None, None, "signin"),
    ("securetoken.googleapis.com", None, None, "signin"),
    ("accounts.google.com", None, None, "signin"),
    ("oauth2.googleapis.com", None, None, "signin"),
    ("login.microsoftonline.com", None, None, "signin"),
    ("appleid.apple.com", None, None, "signin"),
    ("amazoncognito.com", None, None, "signin"),
    ("amazonaws.com", "cognito-idp.", None, "signin"),
    ("amazonaws.com", "cognito-identity.", None, "signin"),
    ("workos.com", None, None, "signin"),
    ("authkit.app", None, None, "signin"),
    ("stytch.com", None, None, "signin"),
    ("supertokens.io", None, None, "signin"),
    ("supertokens.com", None, None, "signin"),
    ("propelauth.com", None, None, "signin"),
    ("kinde.com", None, None, "signin"),
    ("frontegg.com", None, None, "signin"),
    ("descope.com", None, None, "signin"),
    ("logto.app", None, None, "signin"),
    ("ory.sh", None, None, "signin"),
    ("api.magic.link", None, None, "signin"),
    ("keycloak.org", None, None, "signin"),
    ("s3.amazonaws.com", None, None, "storage"),
    ("amazonaws.com", "s3.", None, "storage"),
    ("amazonaws.com", "s3-", None, "storage"),
    ("amazonaws.com", None, ".s3.", "storage"),
    ("amazonaws.com", None, ".s3-", "storage"),
    ("storage.googleapis.com", None, None, "storage"),
    ("firebasestorage.googleapis.com", None, None, "storage"),
    ("blob.core.windows.net", None, None, "storage"),
    ("r2.cloudflarestorage.com", None, None, "storage"),
    ("digitaloceanspaces.com", None, None, "storage"),
    ("backblazeb2.com", None, None, "storage"),
    ("wasabisys.com", None, None, "storage"),
    ("storage.bunnycdn.com", None, None, "storage"),
    ("cloudinary.com", None, None, "storage"),
    ("imagekit.io", None, None, "storage"),
    ("uploadthing.com", None, None, "storage"),
    ("uploadcare.com", None, None, "storage"),
    ("ucarecdn.com", None, None, "storage"),
    ("filestackapi.com", None, None, "storage"),
    ("bytescale.com", None, None, "storage"),
    ("mux.com", None, None, "storage"),
    ("transloadit.com", None, None, "storage"),
    ("stripe.com", None, None, "payments"),
    ("paypal.com", None, None, "payments"),
    ("adyen.com", None, None, "payments"),
    ("checkout.com", None, None, "payments"),
    ("razorpay.com", None, None, "payments"),
    ("paddle.com", None, None, "payments"),
    ("squareup.com", None, None, "payments"),
    ("braintreegateway.com", None, None, "payments"),
    ("lemonsqueezy.com", None, None, "payments"),
    ("mollie.com", None, None, "payments"),
    ("paystack.co", None, None, "payments"),
    ("flutterwave.com", None, None, "payments"),
    ("moyasar.com", None, None, "payments"),
    ("tap.company", None, None, "payments"),
    ("whop.com", None, None, "payments"),
    ("revenuecat.com", None, None, "payments"),
    ("resend.com", None, None, "messaging"),
    ("sendgrid.com", None, None, "messaging"),
    ("mailgun.net", None, None, "messaging"),
    ("mailgun.org", None, None, "messaging"),
    ("postmarkapp.com", None, None, "messaging"),
    ("mailjet.com", None, None, "messaging"),
    ("mailchimp.com", None, None, "messaging"),
    ("brevo.com", None, None, "messaging"),
    ("sendinblue.com", None, None, "messaging"),
    ("amazonaws.com", "email.", None, "messaging"),
    ("twilio.com", None, None, "messaging"),
    ("vonage.com", None, None, "messaging"),
    ("nexmo.com", None, None, "messaging"),
    ("unifonic.com", None, None, "messaging"),
    ("msg91.com", None, None, "messaging"),
    ("api.telegram.org", None, None, "messaging"),
    ("slack.com", None, None, "messaging"),
    ("discord.com", None, None, "messaging"),
    ("discordapp.com", None, None, "messaging"),
    ("fcm.googleapis.com", None, None, "messaging"),
    ("onesignal.com", None, None, "messaging"),
    ("exp.host", None, None, "messaging"),
    ("pushover.net", None, None, "messaging"),
    ("knock.app", None, None, "messaging"),
    ("courier.com", None, None, "messaging"),
    ("pinecone.io", None, None, "search"),
    ("weaviate.network", None, None, "search"),
    ("weaviate.cloud", None, None, "search"),
    ("qdrant.io", None, None, "search"),
    ("qdrant.tech", None, None, "search"),
    ("zillizcloud.com", None, None, "search"),
    ("zilliz.com", None, None, "search"),
    ("trychroma.com", None, None, "search"),
    ("turbopuffer.com", None, None, "search"),
    ("vectara.io", None, None, "search"),
    ("algolia.net", None, None, "search"),
    ("algolianet.com", None, None, "search"),
    ("algolia.io", None, None, "search"),
    ("typesense.net", None, None, "search"),
    ("meilisearch.io", None, None, "search"),
    ("meilisearch.com", None, None, "search"),
    ("elastic-cloud.com", None, None, "search"),
)

IN_APP_SIGNIN_PACKAGES = (
    "flask_login",
    "flask_security",
    "django.contrib.auth",
    "authlib",
    "fastapi_users",
    "allauth",
    "flask_jwt_extended",
)


def dependency_kind_for(host: Any) -> str:
    """Return a closed kind word for ``host``. Total and never raises."""
    try:
        h = host.lower() if isinstance(host, str) else ""
        if not h:
            return "other"
        for domain, prefix, infix, kind in KIND_RULES:
            if not (h == domain or h.endswith("." + domain)):
                continue
            if prefix is not None and not h.startswith(prefix):
                continue
            if infix is not None and infix not in h:
                continue
            return kind
    except Exception:  # noqa: BLE001
        pass
    return "other"


__all__ = ["DEPENDENCY_KINDS", "KIND_RULES", "IN_APP_SIGNIN_PACKAGES", "dependency_kind_for"]