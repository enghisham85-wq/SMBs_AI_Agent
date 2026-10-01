/* ─── Boosthis: AI-provider classification (Web) ───────────────────────────
 *
 * WHY A LIST AND NOT AN ADDRESS. Outbound calls are deliberately reduced to
 * counts and durations, and the destination never leaves the process. That
 * rule does not change here. What leaves is a NUMBER from the fixed table
 * below — a classification against a maintained list, never a hostname the
 * customer chose. A call whose destination is not on the list is not "probably
 * AI": it stays ordinary outbound traffic and is never counted here.
 *
 * THE CODE IS THE WIRE FORMAT. `providerCode` is the 1-based position in
 * {@link AI_PROVIDERS}. Positions are permanent: append a new provider, never
 * reorder or delete one, or every stored reading changes meaning. `0` means
 * "no known provider".
 *
 * PARITY. This file is a copy of `lib/boosthis-runtime-node/src/aiProviders.ts`
 * (the Python kit ships the same table in `boosthis/ai_providers.py`), kept
 * byte-identical below this docblock so the same number can never mean two
 * different providers in two kits. Copied rather than imported because a kit
 * ships as a standalone bundle with no runtime dependencies.
 *
 * ONLY LLM PROVIDERS. The table is limited to providers that report token
 * usage on a normal reply. An image, speech or embedding-only host would be
 * classified as AI and then permanently reported as "usage not reported",
 * which is a finding about the app that would actually be a fact about the
 * provider — so those hosts stay unclassified on purpose.
 */

/** One entry in the maintained provider list. */
export interface AiProviderEntry {
  /** Stable short name. Display text lives on the server, never here. */
  readonly id: string;
  /** Hostname suffixes that identify this provider's data plane. */
  readonly hosts: readonly string[];
}

/**
 * The maintained list. APPEND ONLY — a provider's position IS its wire code.
 */
export const AI_PROVIDERS: readonly AiProviderEntry[] = [
  { id: "openai", hosts: ["api.openai.com"] },
  { id: "anthropic", hosts: ["api.anthropic.com"] },
  {
    id: "google",
    hosts: [
      "generativelanguage.googleapis.com",
      "aiplatform.googleapis.com",
      ".aiplatform.googleapis.com",
    ],
  },
  {
    id: "azure-openai",
    hosts: [".openai.azure.com", ".cognitiveservices.azure.com"],
  },
  { id: "aws-bedrock", hosts: [".amazonaws.com"] },
  { id: "mistral", hosts: ["api.mistral.ai", "codestral.mistral.ai"] },
  { id: "cohere", hosts: ["api.cohere.ai", "api.cohere.com"] },
  { id: "groq", hosts: ["api.groq.com"] },
  { id: "together", hosts: ["api.together.xyz", "api.together.ai"] },
  { id: "perplexity", hosts: ["api.perplexity.ai"] },
  { id: "deepseek", hosts: ["api.deepseek.com"] },
  { id: "xai", hosts: ["api.x.ai"] },
  { id: "fireworks", hosts: ["api.fireworks.ai"] },
  { id: "openrouter", hosts: ["openrouter.ai"] },
  { id: "replicate", hosts: ["api.replicate.com"] },
  {
    id: "huggingface",
    hosts: ["api-inference.huggingface.co", "router.huggingface.co"],
  },
  { id: "voyage", hosts: ["api.voyageai.com"] },
  { id: "cerebras", hosts: ["api.cerebras.ai"] },
  // THE ENDPOINT THE CUSTOMER RUNS THEMSELVES. Carries no hosts of its own:
  // the table walk can never reach it, and it is assigned only when the
  // destination matches an address the app DECLARED (see below). One code for
  // every such endpoint, so a private address can never be reconstructed from
  // a reading — see {@link declaredAiProviderCode}.
  { id: "declared", hosts: [] },
] as const;

/**
 * Wire code for a customer-declared endpoint. Derived from the table so it
 * cannot drift from the entry above.
 */
export const DECLARED_PROVIDER_CODE =
  AI_PROVIDERS.findIndex((p) => p.id === "declared") + 1;

/**
 * AWS hosts are shared by every AWS service, so the suffix alone would drag
 * S3, SQS and DynamoDB into an AI reading. Bedrock's runtime endpoint is named
 * distinctly, and only that prefix counts.
 */
const AWS_BEDROCK_PREFIXES = ["bedrock-runtime.", "bedrock."] as const;

/**
 * Classify one outbound destination against the maintained list.
 *
 * Returns the 1-based provider code, or 0 when the destination is not a known
 * AI provider. Pure, total, allocation-light and cheap enough to run on every
 * outbound call. Never throws, and reads nothing but the hostname.
 */
export function aiProviderCode(host: unknown): number {
  try {
    const h = typeof host === "string" ? host.toLowerCase() : "";
    if (!h) return 0;
    for (let i = 0; i < AI_PROVIDERS.length; i++) {
      const entry = AI_PROVIDERS[i]!;
      for (const suffix of entry.hosts) {
        const hit = suffix.startsWith(".")
          ? h.endsWith(suffix) || h === suffix.slice(1)
          : h === suffix;
        if (!hit) continue;
        if (entry.id === "aws-bedrock") {
          // Only Bedrock's own runtime endpoint — never the rest of AWS.
          if (!AWS_BEDROCK_PREFIXES.some((p) => h.startsWith(p))) continue;
        }
        return i + 1;
      }
    }
    return 0;
  } catch {
    return 0;
  }
}

/** The stable id behind a code, or `null` for 0/out-of-range. Test + local
 *  use only: the wire carries the code, never this string. */
export function aiProviderId(code: number): string | null {
  if (!Number.isFinite(code)) return null;
  const idx = Math.round(code) - 1;
  return AI_PROVIDERS[idx]?.id ?? null;
}

/* ── The endpoint the customer runs themselves ───────────────────────────── */

/*
 * WHY THIS EXISTS. The table above is a list of companies. An app that calls a
 * model it hosts itself — an OpenAI-compatible server on its own network, a
 * sovereign or regional gateway, a private deployment of an open-weights model
 * — matches nothing in it, and until this existed the consequence was not a
 * zero and not a silence: the call was waved through unobserved and all three
 * AI axes were ABSENT. On the dashboard that is indistinguishable from an app
 * with no AI layer at all.
 *
 * WHAT DOES NOT CHANGE. The address still never leaves the page. A declared
 * endpoint is compared here, in memory, and what goes on the wire is the same
 * fixed-table number every other provider sends — {@link DECLARED_PROVIDER_CODE},
 * shared by every customer and every endpoint.
 *
 * WHAT WE REFUSE TO DO WITH IT. We do not price these calls from our own
 * table, even when the reply names a model the table knows. A model the
 * customer hosts has no list price, and a private gateway's price is their
 * contract, not the model provider's published page. The one number we DO take
 * is a cost the endpoint itself reported.
 */

/** Most declared endpoints one install may name. */
export const MAX_DECLARED_AI_ENDPOINTS = 8;

/** Longest hostname accepted, the DNS ceiling. */
const MAX_HOST_LENGTH = 253;

let declaredHosts: readonly string[] = [];

/**
 * Reduce one declared entry to the hostname this meter compares against, or
 * `null` when it is not usable. Never throws.
 */
export function normaliseDeclaredEndpoint(raw: unknown): string | null {
  try {
    if (typeof raw !== "string") return null;
    let value = raw.trim().toLowerCase();
    if (!value || /\s/.test(value) || value.includes("*")) return null;
    if (value.includes("://")) {
      try {
        value = new URL(value).hostname;
      } catch {
        return null;
      }
    } else {
      const slash = value.indexOf("/");
      if (slash >= 0) value = value.slice(0, slash);
      const at = value.lastIndexOf("@");
      if (at >= 0) value = value.slice(at + 1);
      if (!value.startsWith("[")) {
        const colon = value.lastIndexOf(":");
        if (colon >= 0) value = value.slice(0, colon);
      }
    }
    if (value.startsWith("[") && value.endsWith("]")) value = value.slice(1, -1);
    if (!value || value.length > MAX_HOST_LENGTH || value.includes("*")) {
      return null;
    }
    return value;
  } catch {
    return null;
  }
}

/**
 * Replace the model endpoints this page calls. Bad entries are dropped rather
 * than thrown; only hostnames are retained, and only inside this module.
 */
export function setDeclaredAiEndpoints(list: unknown): number {
  try {
    const candidates = Array.isArray(list) ? list : list == null ? [] : [list];
    const out: string[] = [];
    for (const candidate of candidates) {
      const host = normaliseDeclaredEndpoint(candidate);
      if (!host || out.includes(host)) continue;
      if (out.length >= MAX_DECLARED_AI_ENDPOINTS) break;
      out.push(host);
    }
    declaredHosts = out;
    return out.length;
  } catch {
    declaredHosts = [];
    return 0;
  }
}

/** How many endpoints are declared right now. The hostnames are never exposed. */
export function declaredAiEndpointCount(): number {
  return declaredHosts.length;
}

/** Classify against declared endpoints only. Exact hostname match, no suffix. */
export function declaredAiProviderCode(host: unknown): number {
  try {
    const h = typeof host === "string" ? host.toLowerCase() : "";
    if (!h) return 0;
    const bare = h.startsWith("[") && h.endsWith("]") ? h.slice(1, -1) : h;
    return declaredHosts.includes(bare) ? DECLARED_PROVIDER_CODE : 0;
  } catch {
    return 0;
  }
}

/**
 * The full classification: the maintained list first, declarations second.
 * A declaration can never override the factual owner of a maintained host.
 */
export function aiProviderCodeOrDeclared(host: unknown): number {
  const known = aiProviderCode(host);
  return known !== 0 ? known : declaredAiProviderCode(host);
}

/** Test seam: forget every declaration. */
export function _resetDeclaredAiEndpointsForTests(): void {
  declaredHosts = [];
}
