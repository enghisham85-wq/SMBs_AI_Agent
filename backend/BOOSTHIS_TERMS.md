# Boosthis — Terms of Service & Privacy Policy

_Last updated: 2026-09-29_

This single document contains **both** the Terms of Service (Part A) and the
Privacy Policy (Part B) for **Boosthis**, a developer
performance toolkit ("Boosthis", "we", "us", "our"). By installing, enabling,
or otherwise using Boosthis, you ("you", "your", or the "User") agree to this
entire document. If you do not agree, do not install or use Boosthis.

---

# Part A — Terms of Service

## 1. Acceptance and eligibility

- By installing, enabling, configuring, or using Boosthis — including adding it
  to source code, running its runtimes, calling its APIs, or connecting to its
  servers — you accept these Terms.
- **To hold a Boosthis account you must be at least 16 years old.** Boosthis
  is a developer tool and is not intended for children. This is a
  data-protection floor: 16 is the age below which we will not process a
  person's own data on their own say-so.
- **To buy a Paid Plan you must additionally be at least 18 years old**, or
  older where the age of majority where you live is higher, or else be acting
  with the authority of an organization that is. This is a different floor
  answering a different question — contractual capacity. Below the age of
  majority a person generally cannot be held to a subscription, which would
  make the subscription voidable and the payment disputable, so we do not sell
  one. The two ages differ because being old enough to be measured is not the
  same as being old enough to be billed.
- Both floors are asked as their own question, and both are enforced: no
  account is created without an answer to the first, and no payment is started
  without an answer to the second. Neither is verified — both are your own
  declaration — and answering either falsely is a breach of these Terms that
  may cost you the account under Section 14.
- You may use Boosthis only if you can form a binding contract with us.
- If you use Boosthis on behalf of a company or other organization, you
  represent that you have authority to bind that organization to these Terms,
  and "you" includes that organization.

## 2. Definitions

- **"Boosthis" / "the Service"** — the Boosthis software (the runtime kits we
  publish — for phone apps, browsers, servers and hosted edge functions — and
  any other runtimes we add), the rule book, the command-line tools, the MCP
  and HTTP servers, the hosted telemetry API, the developer and admin
  dashboards, and all related documentation. The kits are listed, with what
  each one can measure, on our public pricing page; what a kit measures differs
  by runtime, as described in Part B.
- **"Telemetry"** — everything your app sends to Boosthis: the always-on issue
  signatures and fix-resolution signals, and any optional full performance
  samples you choose to enable. It also covers anything submitted to the rule
  book **through the MCP server or the HTTP API** — including a draft rule an
  AI you have connected writes and submits on your behalf. Defined in detail
  in Part B.
- **"Project Key"** (still called an **"invite key"** in parts of the
  product's code and API field names) — a credential you create from your
  dashboard that lets one
  registered project of yours report to, and read back from, the hosted
  service. Older accounts may hold a key the Maintainer issued by hand; both
  work the same way.
- **"Account"** — the developer account you create to use the hosted web and
  mobile dashboards. Creating one is open to the public and requires your full
  name, an email address, and a password.
- **"Workspace"** — an Account together with any teammates its owner has
  invited, and the projects, keys, and data they share.
- **"Veqtara Tech Company"** (the **"Company"**) — Veqtara Tech Company,
  commercial registration (CR) number 7054832287, registered in the Kingdom of
  Saudi Arabia and trading as **Boosthis**. The Company is the Maintainer: the
  legal entity that operates the Service, that you contract with under these
  Terms, and that sells and invoices every Paid Plan.
- **"Maintainer"** — the creator, owner, and operator(s) of Boosthis.
- **"Protected Parties"** — the Maintainer together with Boosthis's owner,
  creator, administrator(s), operator(s), contributors, and licensors, and each
  of their respective affiliates, officers, employees, and agents. Every
  disclaimer, limitation, release, and indemnity in these Terms runs in favour
  of all Protected Parties.
- **"Paid Plan"** — an optional paid subscription to the hosted Boosthis
  service (described in Section 15).
- **"Payment Processor"** — the third-party payment platform (currently Moyasar)
  that hosts checkout and processes subscription payments on the Maintainer's
  behalf.

## 3. Accounts, Project Keys, and workspaces

- **Anyone can create an account.** Sign-up is open to the public. You give
  your **full name**, an email address, and a password; you must keep those
  details accurate, keep your password and sign-in codes confidential, and you
  are responsible for everything done under your Account. One person, one
  Account — do not share Account credentials. Accounts created before the name
  became mandatory are asked for it once, the next time the account holder
  opens the dashboard.
- **Project Keys are yours to create and to protect.** You mint them from your
  dashboard (how many you may hold at once depends on your plan). You must not
  share, resell, sublicense, publish, or transfer a Project Key, and you are
  responsible for all activity that occurs under your Project Keys and install
  IDs.
- We may rotate, disable, or revoke keys at any time. We are not liable for any
  consequence of a revoked or expired key.
- **Access is still discretionary.** Creating an Account does not entitle you
  to the Service indefinitely: access to the telemetry program and the hosted
  services may be refused, limited, suspended, or revoked at the Maintainer's
  sole discretion — for example for abuse, non-payment, or breach of these
  Terms — as described in Section 14.
- **Teammates see your workspace data.** You may invite teammates by email
  address. Anyone who accepts can see that workspace's projects, their
  performance data, and its activity log; billing, Project Keys, and AI tokens
  stay under the workspace owner's control. The number of seats depends on your
  plan. Invite only people you are willing to share that data with, and remove
  them when they no longer need access.

## 4. License to use Boosthis, and restrictions

- The Boosthis software is licensed to you under the terms of the project
  `LICENSE` (Apache License 2.0). That license governs your rights to use,
  copy, and modify the **code**. These Terms govern your use of the **Service**
  (including the hosted telemetry API, the hosted dashboards, and the AI
  tooling). Where the
  Apache License and these Terms both apply, the Apache License controls your
  rights in the **code**, and these Terms control your use of the **Service**;
  nothing in these Terms limits, modifies, or adds conditions to the rights the
  Apache License grants in the code.
- Except as permitted by the `LICENSE` — and in every case when interacting
  with the Service — you must **not**:
  - use Boosthis or its servers in any unlawful way, or in violation of these
    Terms;
  - probe, scan, overload, rate-abuse, or attempt to gain unauthorized access
    to the telemetry API, admin dashboard, or any Boosthis infrastructure;
  - attempt to bypass, disable, or weaken the PII guard, the privacy
    chokepoint, or any access control;
  - tamper with, remove, disable, or circumvent — or assist or direct anyone
    or anything (including any AI agent or automated tool) to tamper with,
    remove, disable, or circumvent — the Service's activation, entitlement,
    kill-switch, or integrity-verification mechanisms, or misrepresent your
    install's identity, version, or integrity state to the Service;
  - submit false, poisoned, automated, or bulk telemetry intended to corrupt
    the rule book or analytics;
  - remove, obscure, or misrepresent any notice, license, or attribution; or
  - use Boosthis to build a competing dataset or service from telemetry you do
    not own.

## 5. Acceptable use

You are solely responsible for your own application, your source code, your
data, your users, and for what you place into route names, labels, metadata,
the optional app display name, and exception messages. The optional app display
name is developer-provided metadata: it is transmitted to and stored by the
Maintainer, shown back to you in your own dashboard so you can tell your
installs apart, and — like every other field — must not contain personal data
or anything you are not entitled to share. You must not use Boosthis to process,
transmit, or expose unlawful content or to violate the rights of any third
party.

**You — not the Maintainer — are responsible for your end users.** Boosthis runs
inside your application; you are the controller of any data your app handles. If
any law, contract, or privacy regime that applies to you requires you to
disclose to, or obtain consent from, your own end users before collecting,
processing, or transmitting performance telemetry from their use of your app,
**that disclosure and consent are your responsibility, not the Maintainer's.**
The consent screens, notices, and controls Boosthis ships govern the
relationship between you (the developer/integrator) and the Maintainer; they are
**not** a substitute for any notice or consent you owe your own users, and the
Maintainer makes no representation that they satisfy your obligations.

**You must have the right to instrument the application.** You represent and
warrant that, for every application, service, or site you install Boosthis into,
you either own it or are authorised by its owner to install monitoring software
in it and to transmit its telemetry to the Maintainer. You must never install
Boosthis into an application you do not own or control, and you must never use a
Project Key to collect telemetry on behalf of anyone who has not authorised it.

**Prohibited uses.** In addition to Section 4, you must not use Boosthis, its
API, its dashboards, or its AI tooling:

- for any unlawful purpose, or in furtherance of any criminal activity;
- to monitor, profile, track, locate, or gather information about any person, or
  about any organisation's application, without the right to do so — including
  covert surveillance, stalking, or harassment;
- to collect, infer, or transmit personal data, special-category data, payment
  card data, authentication credentials, or secrets through any Boosthis field,
  label, name, or message;
- to develop, host, distribute, test, or operate malware, ransomware,
  credential-stuffing, denial-of-service, scraping, or intrusion tooling, or to
  support, plan, or conceal an attack on any system;
- to reach, read, or attempt to reach any project, key, install, account, or
  telemetry that is not yours;
- in breach of applicable export-control or sanctions law (see Section 18); or
- in any way that would place the Maintainer in breach of any law, or of any
  payment-network, registry, or hosting-provider rule.

**Enforcement.** Where the Maintainer reasonably believes this Section has been
breached, it may — immediately, without notice, and in addition to every right
in Section 14 — suspend or terminate your account, revoke or rotate your Project
Keys, disable affected installs through the Service's activation and kill-switch
mechanisms, and refuse further service. No refund is due for a suspension or
termination under this Section. The Maintainer may also preserve and disclose
records where it is required to do so by law, or where it reasonably believes
disclosure is necessary to prevent serious harm, and will cooperate with lawful
requests from competent authorities.

**Reporting misuse.** If you believe Boosthis is being used in breach of this
Section, email <support@boosthis.com> with "abuse" in the subject. Include
whatever detail you can; reports are reviewed by the Maintainer.

**What the Maintainer can and cannot see.** Boosthis receives performance and
crash telemetry only — never your source code, your files, or your end users'
data (see Part B). The Maintainer therefore cannot inspect, supervise, or police
what your application does, and nothing in this Section is a representation that
it does so. Operating your own application lawfully is your responsibility
alone; this Section defines what is forbidden on the Service and what the
Maintainer will do when it learns of a breach.

## 6. Boosthis intellectual property

- As between you and the Maintainer, the Maintainer owns all right, title, and
  interest in and to Boosthis — including the software, the rule book, the
  detectors, the scoring model, the documentation, the name "Boosthis", and all
  associated logos, trademarks, and trade dress — except for the rights
  expressly granted to you under the `LICENSE`.
- Nothing in these Terms transfers any Boosthis intellectual property to you,
  and no rights are granted by implication or estoppel.

## 7. Ownership and assignment of Telemetry

By installing, enabling, or otherwise using Boosthis with a registered app, you
agree to the following about **everything your app sends to Boosthis** — the
**issue signatures**, the **fix-resolution signals**, and any **optional full
performance samples** you choose to turn on — **and everything submitted to the
rule book through the MCP server or the HTTP API**, including a draft rule an
AI you have connected writes and submits on your behalf (together, the
"Telemetry"):

- **You assign it all to Boosthis.** You hereby waive, and irrevocably assign
  to the Maintainer, all right, title, and interest you may have in and to the
  Telemetry, and to any rules, thresholds, aggregated statistics, models, or
  other improvements derived from it. **This applies to every piece of
  Telemetry you send — whether it is the always-on issue & fix signals or the
  optional full performance samples you enable with `enableTelemetry()` /
  `boosthis.enable_telemetry()`. Turning full data on does not give you any
  ownership claim over what you send.** You will not claim ownership of — or any
  royalty or other compensation for — the Telemetry or anything Boosthis builds
  from it.
- **This expressly includes every issue, error, bug, and fix.** When an issue,
  error, bug, defect, crash, or performance problem is surfaced by the Telemetry
  your app sends, or is identified, reproduced, diagnosed, derived, or generated
  by Boosthis — together with every fix, patch, workaround, rule, remediation,
  signature, insight, or rule-book entry Boosthis creates from or about it — all
  right, title, and interest in that Telemetry, finding, fix, rule, and rule-book
  entry belongs to the Maintainer, and you irrevocably waive, and will not
  assert, any ownership, authorship, inventorship, royalty, or other claim over
  them. This is about Boosthis's record and remediation of the problem, not the
  underlying defect in your own code: as stated below, the assignment never
  reaches your source code, diffs, file contents, or application, which remain
  entirely yours.
- **The doorway does not change the terms.** It makes no difference whether
  material reaches the rule book from your app, from the **MCP server**, or
  from the **HTTP API**, and it makes no difference whether a person or an AI
  you connected wrote it. A draft rule submitted through any of those doorways
  — its title, when it applies, its fix text, and the pattern it was drafted
  from — is assigned on exactly the same terms as an issue signature,
  including everything Boosthis builds from it, surviving erasure and
  termination, with no royalty, authorship, or other claim left behind.
- **You warrant that it was yours to send.** For anything you, or an AI you
  connected, submits to Boosthis, you represent and warrant that it was yours
  to send: that it is **your own material, or material you are free to
  contribute**; that it is **not any third party's proprietary, confidential,
  or licensed material**; and that it **contains no personal data**. You are
  responsible for everything submitted under your Project Keys, including
  material an AI you connected generated. A breach of this warranty is
  indemnified under Section 13.
- **This covers only what you send — never your code or app.** The Telemetry is
  the data described in Part B (the issue & fix signals, plus, in full mode,
  route labels, durations, and ratings), together with anything submitted to
  the rule book through the MCP server or the HTTP API. Boosthis never receives your source
  code, diffs, file contents, or your application — and this assignment gives
  the Maintainer **no rights over any of them**. Your code and your app remain
  entirely yours.
- **The assignment survives erasure and termination.** Running `forget` stops
  the Telemetry tied to your install going forward, but it does not claw back
  rules or aggregate improvements already derived and shipped from Telemetry
  you contributed while using Boosthis. This Section survives termination of
  these Terms.
- **Why this exists.** Boosthis's purpose is a shared, ever-improving rule
  book. This Section makes clear that the Maintainer may keep, aggregate, and
  ship those improvements to everyone without any later ownership claim being
  made over them.

## 8. Feedback

If you send the Maintainer any feedback, ideas, suggestions, or bug reports
about Boosthis, you grant the Maintainer a perpetual, irrevocable, worldwide,
royalty-free, fully sublicensable license to **use, copy, edit, adapt,
publish, distribute, and sublicense** them for any purpose, without
obligation, attribution, confidentiality, or compensation to you. This Section
is the catch-all: anything that reaches the Maintainer outside the doorways
named in Section 7 is covered here.

## 9. AI-generated suggestions are not approvals

Boosthis surfaces performance rules and AI-assisted fix suggestions through its
MCP server, HTTP API, and documentation. **These are suggestions, not
approvals.** You are solely responsible for reviewing, testing, and deciding
whether to apply any suggested change before you ship it. The Maintainer is not
responsible for any outcome of an applied suggestion.

**Rule text may be machine-generated.** A rule's title and fix text may be
written by the Maintainer's own model, by an AI a developer connected to
Boosthis, or by the Maintainer, and an approved rule may be served **as
written**. Every rule a developer can fetch is **human-reviewed**: a person
decides that it may be served. That review is a **review, not a verification**
— it is not a check that the rule is correct, safe, or suitable for your
application, and no Protected Party warrants that it is.

**Applying a fix automatically is your decision and your risk.** If you let an
AI assistant, an agent, or any other automated tool fetch a fix and apply it to
your code without a person reading it first, that is your choice and the risk
is yours. Boosthis never applies a change to your code.

**A rule may change or be withdrawn at any time, without notice.** A rule
served through the MCP server or the HTTP API may be re-worded, replaced, or
withdrawn at any time and without notice to you, and the Maintainer is under no
obligation to keep serving a rule you have previously received.

**The "Ask Boosthis" assistant.** The dashboard assistant answers questions
about your own telemetry. It is **software, not a person**; it is **not
professional advice of any kind** — medical, psychological, legal, or
financial; and it is **not an emergency or crisis service**. If what you type
appears to describe self-harm or harm to another person, the assistant replies
with crisis-line information instead of an AI answer. That reply is
**automatic**: nothing is monitored, no one is alerted, and no one is watching
the box. In an emergency, contact your local emergency number or a crisis line
directly.

## 10. Third-party dependencies and services

Boosthis may rely on third-party software, registries, AI providers, and
hosting platforms. Your use of those is subject to their own terms, and the
Maintainer is not responsible for them.

## 11. Disclaimer of warranties

BOOSTHIS IS PROVIDED **"AS IS" AND "AS AVAILABLE," WITH ALL FAULTS AND WITHOUT
WARRANTY OF ANY KIND**, whether express, implied, or statutory, including
without limitation any implied warranties of merchantability, fitness for a
particular purpose, title, non-infringement, accuracy, and any warranties
arising from course of dealing or usage of trade. **No Protected Party**
warrants that Boosthis will be uninterrupted, timely, error-free, secure, or
free of harmful components, that any defect will be corrected, or that the PII
guard will catch every identifier or secret — the PII guard is a **best-effort
defence against common identifier and secret field names**, not a guarantee. No
advice or information, oral or written, obtained from any Protected Party
creates any warranty not expressly stated here. You assume the entire risk as
to the quality, performance, and results of Boosthis and use it **entirely at
your own risk**. Boosthis is **not designed, intended, or licensed for use in
high-risk activities** — including medical or life-support systems, aviation,
nuclear facilities, weapons systems, or any other environment where a failure
could lead to death, personal injury, or severe physical or environmental
damage — and no Protected Party is liable for any use of Boosthis in such
activities. This restates and supplements the warranty disclaimer in the
`LICENSE` (Apache License 2.0, Section 7); to the extent of any conflict, the
broader disclaimer applies to your use of the Service. Some jurisdictions do
not allow the exclusion of certain warranties, so some exclusions may not apply
to you.

## 12. Limitation of liability, assumption of risk, and release

**Use at your own risk — no liability.** You use Boosthis entirely at your own
risk. To the maximum extent permitted by law, none of the Protected Parties
(defined in Section 2 — including the owner, creator, and administrator of
Boosthis, its maintainers and contributors) will have any liability of any kind
to you or anyone else for any loss or damage arising from or relating to
Boosthis — including any data exposure, leak, loss, corruption, downtime,
security incident, or business loss. This clause works together with Sections
11–13. If you do not accept this, do not install or use Boosthis.

**No liability.** TO THE FULLEST EXTENT PERMITTED BY LAW, IN NO EVENT WILL ANY
PROTECTED PARTY (INCLUDING THE OWNER, CREATOR, AND ADMINISTRATOR OF BOOSTHIS) BE
**LIABLE TO YOU OR ANY THIRD PARTY** FOR ANY INDIRECT, INCIDENTAL, SPECIAL,
CONSEQUENTIAL, EXEMPLARY, OR PUNITIVE DAMAGES, OR FOR ANY LOSS OF PROFITS,
REVENUE, BUSINESS, DATA, GOODWILL, OR OTHER INTANGIBLE LOSSES, OR FOR ANY **DATA
EXPOSURE, LEAK, LOSS, CORRUPTION, DOWNTIME, SECURITY INCIDENT, OR REGULATORY
PENALTY**, ARISING OUT OF OR RELATED TO BOOSTHIS OR THESE TERMS — WHETHER BASED
ON WARRANTY, CONTRACT, TORT (INCLUDING NEGLIGENCE), STRICT LIABILITY, STATUTE,
OR ANY OTHER LEGAL THEORY, AND WHETHER OR NOT ANY PROTECTED PARTY HAS BEEN
ADVISED OF THE POSSIBILITY OF SUCH DAMAGES, AND EVEN IF A REMEDY FAILS OF ITS
ESSENTIAL PURPOSE.

**Liability cap.** TO THE EXTENT ANY LIABILITY CANNOT BE FULLY EXCLUDED, THE
PROTECTED PARTIES' TOTAL AGGREGATE LIABILITY FOR ALL CLAIMS WILL NOT EXCEED
**THE GREATER OF (A) THE TOTAL AMOUNT YOU ACTUALLY PAID THE MAINTAINER FOR
BOOSTHIS IN THE TWELVE MONTHS BEFORE THE CLAIM AND (B) SAR 100.** THIS CAP IS
AGGREGATE ACROSS ALL CLAIMS, NOT PER-INCIDENT. FOR ANY NO-CHARGE PRODUCT THE
LOWER CAP IN SECTION 16 APPLIES INSTEAD.

**Assumption of risk.** You acknowledge that Boosthis is an automated
performance tool that runs inside your own application, that you alone control
what data your application handles and exposes, and that you **knowingly and
voluntarily assume all risk** arising from your use of Boosthis and from any
change you make in reliance on it.

**Release.** To the fullest extent permitted by law, you **release and forever
discharge the Protected Parties** from any and all claims, demands, and
liabilities of every kind, known or unknown, arising out of or related to your
use of Boosthis, the Telemetry, or any AI-suggested change.

This Section restates and supplements the limitation of liability in the
`LICENSE` (Apache License 2.0, Section 8); to the extent of any conflict, the
broader limitation applies to your use of the Service. Some jurisdictions do not
allow certain limitations, so some of the above may not apply to you; in that
case the Protected Parties' liability is limited to the minimum permitted by
law.

## 13. Indemnification

To the fullest extent permitted by law, you agree to **defend, indemnify, and
hold harmless** the Protected Parties from and against any claims,
damages, liabilities, losses, and expenses (including reasonable legal fees)
arising out of or related to: (a) your use of Boosthis; (b) your application,
source code, data, or users; (c) the data you place into route names, labels,
or metadata; (d) your violation of these Terms or any law; (e) your violation
of any third party's rights; or (f) any material you, or an AI you connected,
submitted to Boosthis — including any breach of the warranty in Section 7 that
it was yours to send, that it is not any third party's proprietary,
confidential, or licensed material, and that it contains no personal data.

## 14. Term, suspension, and termination

- These Terms apply for as long as you use Boosthis.
- The Maintainer may **suspend or terminate** your access (including your
  Account, your Project Keys, and telemetry participation) at any time, with or
  without notice, for any reason, including suspected abuse — without liability
  to you.
- **Tampering is a material breach.** Tampering with, removing, disabling, or
  circumventing the Service's activation, entitlement, kill-switch, or
  integrity-verification mechanisms (or assisting or directing anyone or
  anything to do so) is a **material breach** of these Terms and results in
  **immediate termination** of your access, Project Keys, and telemetry
  participation, without notice or liability. The Maintainer **may** also
  pursue any remedy available at law or in equity for such breach; no remedy
  is exclusive, and declining to pursue a remedy is not a waiver.
- You may stop at any time: disable optional samples, set the kill-switch
  `BOOSTHIS_DISABLED=1`, or run `forget` to erase your data (see Part B).
- **Non-payment is a freeze, never deletion.** If a Paid Plan lapses, your
  account and data are **frozen, not deleted**: growth actions pause, but
  everything you already collected stays viewable, nothing is erased, and
  everything unlocks the moment payment resumes (see Section 15).
- **Revocation or suspension starts a deletion countdown.** If your access is
  revoked or suspended, a **60-day countdown to deletion** of the associated
  stored data begins. Restoring your access before it ends cancels the
  countdown. Running `forget` deletes immediately at any time. Learning
  signals survive deletion only in permanently de-identified form, as
  described in Section 7 and Part B.
- Sections that by their nature should survive termination — including 6, 7, 8,
  11, 12, 13, 15 (for fees already owed), 16, 17, 20, 22, and 26 — survive.

## 15. Subscriptions, billing, and payment

Boosthis offers optional **Paid Plans** — subscriptions billed either monthly
or yearly, whichever you choose at checkout, that unlock higher limits and
additional features on the hosted service. The plans, prices, the billing
period, and what each includes are shown on the billing page at the time of
purchase.

- **Payment is handled by the Payment Processor.** Checkout happens on the
  Payment Processor's own hosted pages, under its own terms and privacy
  policy. **Your card details go to the Payment Processor and never touch
  Boosthis's servers.** Boosthis stores which plan you
  are on, a reference to your subscription, and — so renewals work and you can
  recognize the card — your card's brand, its last four digits, and a reusable
  payment token issued by the Payment Processor (never the card number
  itself).
- **Changing plans.** An **upgrade** takes effect immediately: you pay only the
  prorated difference for the remainder of the current period, and your renewal
  date does not move. A **downgrade** takes effect at your next renewal, so you
  keep what you paid for until then. If a quoted upgrade price is no longer
  valid by the time payment completes, the payment is refunded rather than
  applied.
- **Failed or declined renewals.** If a renewal payment is declined we email
  the Account address and the plan lapses into the frozen state described
  below — nothing is deleted, and paying resumes full access.
- **Refunds, when issued**, are returned through the Payment Processor to the
  original payment method.
- **Plans renew automatically** at the end of each billing period — every month
  on a monthly plan, every year on a yearly one — until you cancel. You can
  cancel at any time from the billing page or with the Payment Processor;
  cancellation takes effect at the end of the current billing period, and you
  keep the paid features until then.
- **Free trials.** Where a free trial is offered, **no payment card is
  required and nothing is charged to start one**. It is limited to **one trial
  per Account and one per confirmed email address**. **When the trial period
  ends (currently 7 days, as stated on the billing page at the time you start),
  the trial simply ends: nothing is charged, and no payment is attempted.** The
  paid features stop, and your projects, project keys and everything already
  measured stay in your Account; subscribing at any time afterwards switches
  them back on. You may end the trial earlier from the billing page. If you
  subscribe while the trial is still running, you are charged once at the
  ordinary price and the period you have paid for begins on the day the trial
  would have ended. The Maintainer emails the Account address when the trial
  starts, during the trial, and when it ends, and may withdraw or change trial
  offers at any time for Accounts that have not yet started one. Trials started
  before 19 September 2026 took a payment card, and where such a trial is still
  running the terms stated at the time it started continue to apply to it.
- **Fees are paid in advance and are non-refundable**, except where a refund is
  required by applicable law or granted by the Maintainer at its discretion.
- **Taxes and currency.** All Fees are charged in Saudi Riyal (SAR), and the
  SAR figure shown at purchase is the total amount charged; it includes VAT
  and any similar taxes where they apply under the laws of the Kingdom of
  Saudi Arabia. Where a price is also shown in US dollars, that figure is an
  indicative conversion at a fixed rate and is never the amount charged: your
  card issuer converts the SAR amount at its own rate, which may include a
  fee. If the Maintainer is or becomes VAT-registered with the Zakat, Tax
  and Customs Authority (ZATCA), VAT will be charged and invoiced in
  accordance with ZATCA regulations at the then-current rate. If you purchase
  from outside Saudi Arabia, you are responsible for any taxes, levies, or
  duties imposed by your own jurisdiction.
- **Price changes.** The Maintainer may change plan prices or features; changes
  take effect at your next renewal, and any change to what you pay is emailed
  to you at least 14 days before the first renewal charged at the new price —
  a renewal falling inside those 14 days is charged at the old price.
  Continued renewal after a change means you accept it.
- **Non-payment never deletes your data.** A lapsed subscription only pauses
  growth actions (such as adding new projects or new connections); everything
  already collected stays viewable, and full access returns instantly on
  payment (see Section 14).
- **A Payment Processor outage never downgrades you.** If the Payment Processor
  cannot be reached, your last known plan state remains in effect.
- **Complimentary access.** The Maintainer may grant free or complimentary
  access at its discretion; such access is a no-charge product under
  Section 16 and may be modified or withdrawn at any time.

## 16. Free, beta, and no-charge products

Parts of Boosthis may be provided at no charge — including free features,
trials, beta or pre-release versions (such as TestFlight builds of the mobile
app), and complimentary or maintainer-granted access (together, **"No-Charge
Products"**).

- No-Charge Products are provided **"AS IS", with no warranty, no support
  commitment, and no availability commitment**. They may be changed, limited,
  suspended, or discontinued at any time, and may never become generally
  available.
- Beta and pre-release versions are by definition unfinished and may contain
  defects; do not rely on them for production-critical decisions.
- NOTWITHSTANDING SECTION 12, THE PROTECTED PARTIES' TOTAL AGGREGATE LIABILITY
  ARISING OUT OF OR RELATED TO ANY NO-CHARGE PRODUCT WILL NOT EXCEED **SAR
  50**.

## 17. Confidentiality, publicity, and benchmarks

- Parts of Boosthis are **not public**. Non-public information you receive
  through it — including the contents of the rule book and fix corpus,
  non-public documentation, Project Keys and other credentials, non-public
  dashboards, roadmaps, and pricing not publicly listed — is
  **Confidential Information** of the Maintainer.
- You will use Confidential Information only to use Boosthis as permitted by
  these Terms, and will not disclose or publish it.
- You will not publicly disclose **benchmarks or comparative analyses** of
  Boosthis, and will not publicly announce or advertise your use of Boosthis
  (in marketing, press, talks, or public repositories), without the
  Maintainer's prior written consent.
- These obligations do not apply to information that becomes public through no
  fault of yours, or that you are required to disclose by law (with prior
  notice to the Maintainer where lawful). This Section survives termination.

## 18. Export controls and sanctions

You represent that you are not located in, or ordinarily resident in, any
country or territory subject to comprehensive sanctions or embargoes, and that
you are not on any government sanctions or denied-party list. You agree to
comply with all applicable export-control and sanctions laws in your use of
Boosthis.

## 19. Changes to Boosthis and to these Terms

- The Maintainer may modify, suspend, or discontinue any part of Boosthis at any
  time without liability.
- The Maintainer may update these Terms; the "Last updated" date will change and
  material changes will be noted in the release. Your continued use after a
  change means you accept the updated Terms. As stated in Part B, a new consent
  is requested for any new **use** of telemetry beyond rule-book improvement.

## 20. Governing law and dispute resolution

- These Terms are governed by the laws of the Kingdom of Saudi Arabia, without
  regard to conflict-of-laws rules.
- **Informal resolution first.** Before filing any claim, you agree to contact
  the Maintainer and attempt in good faith to resolve the dispute informally for
  at least 30 days.
- **Binding arbitration.** Any dispute, claim, or controversy arising out of or
  relating to Boosthis or these Terms that is not resolved informally will be
  finally settled by binding arbitration administered by the **Saudi Center for
  Commercial Arbitration (SCCA)** under its Arbitration Rules, before a single
  arbitrator, seated in Riyadh, Kingdom of Saudi Arabia, conducted in English
  unless the parties agree otherwise. The award is final and binding, and
  judgment on it may be entered in any court of competent jurisdiction.
- **Equitable relief.** Nothing in these Terms prevents the Maintainer from
  seeking injunctive or other equitable relief in any court of competent
  jurisdiction to protect its intellectual property or confidential
  information.
- **Attorneys' fees.** In any action or proceeding to enforce these Terms, the
  prevailing party will be entitled to recover its reasonable attorneys' fees
  and costs, in addition to any other relief awarded.
- **Time limit on claims.** To the fullest extent permitted by law, any claim
  arising out of or related to Boosthis or these Terms must be filed within
  **twelve (12) months** after the claim accrued; otherwise it is permanently
  barred.

## 21. Severability

If any provision of these Terms is held unenforceable, that provision will be
limited or removed to the minimum extent necessary, and the remaining
provisions will remain in full force.

## 22. No waiver

The Maintainer's failure to enforce any provision is not a waiver of its right
to do so later.

## 23. Assignment

You may not assign or transfer these Terms, your Account, or your Project
Keys without the
Maintainer's prior written consent. The Maintainer may assign these Terms
freely, including in connection with a merger, acquisition, or sale of assets.

## 24. Force majeure

The Maintainer is not liable for any delay or failure caused by events beyond
its reasonable control, including outages of third-party infrastructure,
network failures, or acts of nature.

## 25. Entire agreement

These Terms (together with the `LICENSE` and the privacy policy in Part B) are
the entire agreement between you and the Maintainer regarding Boosthis and
supersede any prior understanding on the subject.

## 26. Notices

- Any notice you are required or permitted to give the Maintainer under these
  Terms must be given in writing through the contact channel identified in the
  **Contact** section of Part B (**support@boosthis.com**), and is deemed
  given when actually received.
- The Maintainer may give you any notice under these Terms by email to the
  address on your account (if any), by a message or banner inside Boosthis or
  its dashboards, or by a note in the release notes; such notice is deemed
  given when sent or posted.

## 27. Copyright and other intellectual-property complaints

**What this section is for.** Boosthis publishes nothing you send it. There is
no public feed, no public profile, and no gallery of customer material; Part B
sets out what the Maintainer holds and who can see it. A few paths do accept
material you supply — build and source maps you upload, the free text of a
rule you propose, messages to the "Ask Boosthis" assistant, mail to support,
and the display names you choose for a project or an app. If you believe
anything held on the Service infringes your copyright or another
intellectual-property right of yours, this section is how to say so and what
the Maintainer will do about it.

**Where to send a complaint.** Email <support@boosthis.com> with "copyright"
in the subject, addressed to the **Copyright Complaints contact, Veqtara Tech
Company**. The same complaint may be sent by post to the registered address in
the **Contact** section of Part B. That contact is published on the public
Terms page at <https://www.boosthis.com/terms> and in the Arabic reading of
these Terms, so a rights holder never needs an account to find it.

**What a complaint must contain.** So that the Maintainer can act on a
complaint rather than write back asking, include all of the following:

- your name, postal address, email address, and a telephone number where you
  have one;
- identification of the work you say is infringed — a copy, a link, or enough
  description to identify it;
- identification of the material on the Service you say infringes it, in
  enough detail to find it: normally the project, the install, the upload, or
  the exact text complained of;
- a statement that you believe in good faith that the use complained of is not
  authorized by the rights holder, its agent, or the law;
- a statement that the information in the complaint is accurate and that you
  are the rights holder or are authorized to act for them; and
- your signature, electronic or physical.

A complaint missing something is not discarded. The Maintainer will say what
is missing and hold the complaint open until you answer.

**What happens next.** The Maintainer acknowledges a complete complaint within
**5 business days**. Where it concludes the material is likely infringing it
removes or disables that material, and tells the customer who supplied it what
was removed, why, and that they may reply. Because nothing here is visible to
anyone outside the account that sent it, removal means deletion from the
Maintainer's systems rather than hiding. The Maintainer may act sooner, and
without waiting for a reply, where a complaint is plainly well founded, and
may refuse a complaint it concludes is not well founded, saying why.

**Counter-notice.** If your material was removed and you believe that was
wrong, email the same address with "copyright counter-notice" in the subject.
Identify what was removed and where it was, say why you believe the removal
was a mistake or a misidentification, and give your name, postal address,
email address, and signature. The Maintainer passes a counter-notice to the
complainant. Where the complainant does not say within **10 business days**
that it is taking the matter further, the Maintainer may restore the material;
where restoration is impossible because the material was deleted, it will tell
you so rather than leave you waiting.

**Repeat infringers.** An account that is the subject of repeated upheld
infringement complaints has its access terminated under Section 14, and the
Maintainer may revoke its Project Keys under Section 5. What counts as
repeated is a judgement on the facts rather than a counter: one deliberate,
large-scale infringement can be enough, and a complaint that is not upheld
never counts toward it.

**Misusing this channel.** A complaint made in bad faith, or a
misrepresentation that material is infringing, is itself a breach of Section 5
and may cost you your account. It may also leave you liable to the person
whose material was removed.

**This is a process, not a forum.** Nothing in this Section changes Section 20,
which continues to govern these Terms and every dispute under them. Where the
law of a country the Maintainer is subject to prescribes a particular
procedure for such complaints, that procedure applies in addition to this one;
this Section is the floor, not the ceiling.

## 28. Applications directed at children

**The case this Section is about.** Section 1 sets the age at which you may
hold an account and the age at which you may buy a Paid Plan. Neither says
anything about the people who use *your* application. Boosthis measures your
application while real people are using it, and your application may be
directed at children. That is your decision, and these are the terms on which
you may make it while running Boosthis.

**Yours, not the Maintainer's.** Where your application is directed at
children, or you know that children use it, you are the party responsible for
every notice and consent the law where your users are requires — including any
verifiable parental or guardian consent — before Boosthis runs inside it and
sends measurements. The Maintainer has no relationship with your users, cannot
see who they are, and cannot obtain a consent on your behalf. Part B and the
Maintainer's published statement of what the toolkit collects, at
<https://www.boosthis.com/docs/what-boosthis-collects>, give you the facts you
need to describe Boosthis accurately in your own notice; neither tells you
what your notice must say.

**What you must not send.** The prohibition in Section 5 on sending personal
data through Telemetry applies to your users' personal data whatever their
age, and applies with no exception to a child's. Do not put a user's name,
email address, account identifier, or any other value that identifies a person
into a route label, a screen name, a job name, a project or application
display name, a rule proposal, or any other field the toolkit carries. Those
fields are for labels, not values, and the Maintainer holds what arrives in
them as written.

**Telling the Maintainer an application is child-directed.** You may tell the
Maintainer that a project's application is directed at children by emailing
<support@boosthis.com> with "child-directed" in the subject and naming the
project. On being told, the Maintainer will: switch that project's optional
detailed modes off and record that they may not be switched back on without a
fresh instruction from you; treat any request to erase that project's
telemetry as immediate; and answer a guardian's request about that project
through you, because the Maintainer cannot identify an individual user and you
can. The Maintainer does not verify the statement and does not police your
application — Section 5 says why it cannot.

**Where the Maintainer learns of it another way.** If the Maintainer comes to
believe that data identifying a child has reached it through your Telemetry,
it may delete that data without notice, suspend the affected project's
reporting, and require you to confirm what you have corrected before
reporting resumes. No refund is due for a suspension under this Section.

---

# Part B — Privacy Policy

**Boosthis is a developer toolkit.** It runs inside your
own app on your own machine. For a **general install**, nothing leaves your
device. For a **registered app**, a
tiny, anonymous stream of *issue & fix signals* is reported automatically so
the shared rule book improves — see exactly what, and how to stop it, below.
Separately from the toolkit, anyone can create a
**developer account** (name, email, and password) to use the hosted web and
mobile dashboards and manage billing — what we store for an account is described
under **"Account data"** below, and it is never merged with telemetry.

> **Scope of this policy — please read.** Boosthis is a library that runs
> inside the host application's own process. It is **not** a separate service or
> data processor under GDPR Article 28; the operator of the host application
> remains the controller and processor of any data the host app handles. The
> PII protections described below are **best-effort defences against common
> identifier and secret field names** — they are not a substitute for the host
> application's own privacy review, Data Protection Impact Assessment, or
> compliance posture. Treat Boosthis samples like any other application log:
> review what your code puts into route names, metadata fields, and exception
> messages before exposing them to AI tools or third-party endpoints.

> **Use at your own risk — no liability for data exposure.** Boosthis is
> provided **"as is," with no warranty of any kind**, and you use it **entirely
> at your own risk** (see Part A, Sections 11–12). Because Boosthis runs inside
> your own application, **you remain solely responsible** for the data your app
> handles, for what you place into route names and metadata, and for reviewing
> any AI-suggested change before you ship it.

Boosthis sends two very different things, and they are controlled differently.
This document tells you exactly what each is, what is never sent, what it is
used for, and how to stop it.

> **1. Issue & fix signals — always on for registered apps.** Once your app is
> registered, Boosthis automatically
> reports the *candidate rule signatures* and *fix-resolution signals* described
> below (rule kind + bucketed severity + bucketed count, plus a before→after
> rating bucket when an issue is fixed). This is how the shared rule book
> improves, and it is **not** a per-app on/off setting — it cannot be turned off
> from inside the app. It stops only if you **erase your data** (`forget`) or set
> the emergency kill-switch `BOOSTHIS_DISABLED=1`. This applies **only** to
> registered apps — a general install reports nothing.
>
> **2. Full performance details — optional, off unless you turn it on.**
> Separately, you may switch on fuller per-route performance samples (route
> label, duration, rating) by calling `enableTelemetry()` /
> `boosthis.enable_telemetry()`. This is the **only** channel that
> `disableTelemetry()` / `boosthis.disable_telemetry()` controls — turning
> telemetry "off" stops these full samples but does **not** stop the always-on
> issue & fix signals above. The "Route label" and "Duration" items below apply
> only to this full-details mode. In issues-only registrations
> (`issuesOnly: true`) this channel is never enabled: `transmit()` is a hard
> no-op and the production sampler is never granted consent.
>
> The emergency kill-switch `BOOSTHIS_DISABLED=1` silences the entire runtime —
> issue signals included — and always wins over everything else.

## What we use your data for

We use opted-in telemetry for **one purpose, and one purpose only**:

> **To improve the Boosthis rule book** — adding new perf rules, refining
> thresholds on existing rules, and spotting performance patterns that recur
> across many apps so Boosthis catches them: in the next release of a kit, or
> straight away through the rule book Boosthis serves over its API.

We do **not**:

- Sell your data to anyone, ever
- Share it with advertisers, data brokers, or marketing platforms
- Use it for advertising, profiling, lead generation, or any commercial purpose
  unrelated to the rule book
- Train general-purpose AI / large language models on it
- Link it to your identity, IP address, account, email, or device
- Combine it with data from any other source (including your developer account
  — account data and telemetry are kept apart)

If we ever want to use telemetry for anything beyond rule-book improvement, we
will ship a new major version of Boosthis that asks for your consent again —
your existing opt-in does not roll forward to new uses.

## Ownership of what you send

Everything you send to Boosthis (the "Telemetry") is **assigned to Boosthis**.
You waive any ownership claim over the issue & fix signals and the optional full
performance samples, and over any rules or improvements derived from them — even
if you turn on full data mode. This covers only what you send, never your source
code or application. The full clause is in **Part A, Section 7 (Ownership and
assignment of Telemetry)**.

## What we collect

- **An anonymous install ID** — a random UUIDv4 generated on your machine the
  first time your app registers. It is not linked to your IP, your email, your
  account, your hostname, or anything else.
- **Candidate rule signatures** (auto-submitted; always on for registered
  apps) — when the engine sees a recurring perf pattern that doesn't map to any
  shipped rule, it produces a privacy-safe signature of the form
  `<detector-kind>:<severity-bucket>:<count-bucket>` (e.g.
  `ghost-mount:med:<10`). The screen name is deliberately dropped — only the
  detector kind, a 3-level severity bucket derived from p95, and a coarse
  occurrence-count bucket are kept. The runtime auto-submits these signatures
  the moment they cross the local recurrence threshold — you don't have to click
  anything, and turning telemetry "off" does not stop them (only `forget` or
  `BOOSTHIS_DISABLED=1` does). For a general (unregistered) install, nothing ever
  leaves your device. Server stores one row per `(install, signature)` — repeat
  submissions of the same signature from the same install just bump a counter.
- **Fix-resolution signals** (auto-submitted; always on for registered apps) —
  when a rule the engine was tracking later **improves** (its severity bucket
  drops, e.g. a screen that was rated `poor` becomes `needs-work` or `good`),
  the runtime auto-submits a privacy-safe *resolution*: the rule kind, and the
  before→after **rating bucket only** (`good` / `needs-work` / `poor`). This is
  "the fix landed" metadata, **not the fix itself** — Boosthis never sees or
  sends your source code, diffs, file paths, screen names, durations, or any
  values. The server stores one row per `(install, rule)`. Like candidate
  signatures, it is not user-toggleable; only `forget` or `BOOSTHIS_DISABLED=1`
  stops it.
- **Crash reports** (auto-submitted; always on for registered apps) — when your
  app hits an uncaught error, an unhandled promise rejection, or a render crash,
  Boosthis records a privacy-safe **crash code**: the error *type* (e.g.
  `TypeError`), a **redacted code location** (the top stack frame reduced to a
  function name + file *basename* + line number — never an absolute path, URL,
  query string, argument, or value), a stable hashed signature derived **only**
  from those code-defined tokens (never from the error message text), and an
  occurrence count (how many times that same crash has recurred since the last
  report). This is the minimum Boosthis needs to identify
  and fix the crash — it is the crash's *code*, not your users' data, your
  values, your message text, or your source, all of which are dropped. Like every
  other finding, a crash Boosthis records and the fix it derives become a Boosthis
  rule (see Part A, Section 7). The server stores one row per
  `(install, signature)`; repeats just bump a counter. Not user-toggleable — only
  `forget` or `BOOSTHIS_DISABLED=1` stops it, and nothing is ever sent for a
  general (unregistered) install.
  A current kit also sends a short-lived, non-readable fingerprint for each
  crash batch so a lost response can be retried without double-counting. This
  operational duplicate-suppression metadata is not customer history, is
  excluded from exports, is kept for no more than 30 days and 1,024 receipts
  per install, and is erased with the install.
- **Learning from experience — new bugs, issues & fixes** (auto-submitted;
  always on for registered apps) — when Boosthis runs into a slow or buggy
  pattern it doesn't yet have a rule for, it may send a small, privacy-safe
  note about the *kind* of problem: the runtime, a coarse category, a severity
  bucket, a timing bucket, a hashed signature, and a recurrence count. Exactly
  like a candidate rule signature, this is buckets and a hash — **never** a
  screen name, a value, your users' data, or your source. If you have connected
  your own AI to Boosthis, that AI may additionally attach a *suggested* rule (a
  short title, when it applies, and a suggested fix); every part of a suggestion
  is put through the same PII / secret / URL guard before it leaves the device,
  and it is assigned under **Part A, Section 7** (anything arriving outside that
  Section's doorways is covered by the feedback licence in **Section 8**).
  **Nothing a consumer sends ever becomes a live rule automatically** — a
  suggestion only lands in the maintainer's private review queue, and a human
  decides whether it becomes a rule. An approved suggestion may be served **as
  written**, straight away through the API, rather than waiting for a kit
  release (see "How candidate rules become real rules" below and
  **Part A, Sections 7–9**).
- **Package version + runtime** — e.g. `boosthis 0.2.0 (py)`.
- **Your application's own version** — on a phone, read straight out of the
  build your app shipped in (`CFBundleShortVersionString` on iOS,
  `versionName` on Android) and sent **beside** the Boosthis version above,
  never merged with it. It is the version string you ship and nothing else:
  not the build's contents, not its signing identity, not a build number, not
  anything about the device.
- **Scheduled job names, and how each run went** — if your app runs work on a
  schedule, the job's name as written in your source, how long each run took,
  whether it succeeded, and — if your code states one — how often it should
  run. Never an argument, a payload, a result, a log line, or anything the job
  touched.
- **Anonymous daily reach tag (React Native)** — a small random value your
  device mints fresh each calendar day, **never** derived from your device,
  user, account, or install ID. The server folds it into a coarse rolling
  estimate of roughly how many distinct devices hit the same issue this week
  ("≈1", "≈2–9", "≈10+") and **never stores the tag itself**. Because it
  rotates daily it cannot track a device across days, and erasing your data
  (`forget`) also deletes the tag on your device.


### The names you choose yourself

Neither of these is sent by every install, so they are listed apart from the
section above rather than inside it:

- **The name your app registers under** — sent only if your own code or build
  gives the kit one. The field is optional, and an install that sends none is
  listed by its identifier instead.
- **The name you type for the project** — typed on your Boosthis dashboard and
  sent by nothing. No kit ever sees it.

Both are held **as written**, apart from surrounding spaces being dropped and a
60-character clamp. Nothing else normalises, rewrites or shortens them, because
a rewritten name would make your dashboard, your alerts and our AI answers
disagree with one another. They are free-text fields: **a name containing a
person's name is held as written**, so choose them as you would any label you
publish internally. Two things do happen to a name on the way out — one
carrying a value rather than a label (an address, a phone number, a token or a
long numeric id) is withheld from answers rather than repeated back, and a
registration whose name trips the same identifier guard that screens telemetry
is refused at the door rather than stored.

### Only in the optional full-details mode (`enableTelemetry()`)

These are sent **only** if you explicitly switch on full performance samples,
and they stop the moment you call `disableTelemetry()` /
`boosthis.disable_telemetry()`:

- **Route label** — the code-defined name of the function or route you
  instrumented (e.g. `/api/users`, `get_orders`). Boosthis's PII guard refuses
  to transmit this field if it contains an email, IP, JWT, phone number, or
  other identifier. If you put user content in your route names, the sample is
  dropped.
- **Duration in milliseconds.**
- **Rating** — one of `good`, `needs-work`, `poor`.
- **Rule ID** (optional) — if you attached a Boosthis rule to the sample.

The page map described below is **not** one of these fields: it has a switch of
its own and travels with the health-meter snapshot, on either of that
snapshot's two switches.

### The page map (web and React Native)

Boosthis's server can already draw the map of an app's *server* side, because
it can see the calls one part makes to another. It cannot see the front: a
browser page or a phone screen moving to another one makes no request, so
nothing tells the server that your checkout screen is reached from the basket
and never from search. The page map is the front half of that picture, and the
browser and React Native kits are the only ones that can draw it.

It is drawn **progressively, from your app's own traffic** — it is whatever
paths people have actually walked, not a route table read out of your config —
and it is a picture of how your app is navigated **now**, not a history: each
upload replaces what that copy of your app previously said.

**What travels: the route labels of the screens your app moves between, and
which of them leads to which** — the same code-defined labels as the route
label above, in pairs, with a count of how often each path was taken. They go
through the same PII guard, so a label carrying an email, IP, token or other
identifier is refused exactly as a sample's would be.

**What never travels: the name or caption of any control, and the gesture that
moved you.** Not "the *Pay now* button", not "swiped", not "tapped at these
coordinates". This is not a setting you could turn the wrong way — the kits
have no wire shape that can carry those things, so they cannot be swept along
by accident. Nothing about who navigated, when they did it, or what they typed
is collected either.

**It has a switch of its own, and it rides the health-meter snapshot.**
Nothing is drawn or sent unless your own code asks for it (`sendPageMap`), and
nothing we send back can turn that on. With it on, the map travels with the
snapshot described below and on that snapshot's two switches: it is sent only
when full details are on, or when you explicitly switch on meter sharing so a
connected AI can read the live picture. In issues-only mode with neither of
those on, no map leaves the device. `disableTelemetry()` /
`boosthis.disable_telemetry()`, `forget` and `BOOSTHIS_DISABLED=1` each stop
it. A path that stops being reported is
deleted after 45 days, and `forget` deletes your map immediately along with
everything else that project sent.

### Health-meter numbers (the dashboard meter page)

Boosthis scores your app on a set of named **health meters** — the tiles you see
on the dashboard (the boot ladder, Speed, Smoothness, Scroll, Stability, Render,
Frustration and Idle, plus the runtime-specific meters such as event-loop lag,
garbage-collection pressure, memory growth, worker health and startup cost). The
whole meter page is captured as one small snapshot so you — and an AI you choose
to connect — can read your own numbers back.

It rides the **same optional full-details channel** as the samples above: the
snapshot is uploaded only when full details are on, or when you explicitly switch
on meter sharing so a connected AI can read the live picture. `disableTelemetry()`
/ `boosthis.disable_telemetry()`, `forget`, and `BOOSTHIS_DISABLED=1` all stop it.

Every meter reports the same three things and nothing else:

- **A score** (0–100) and a **rating bucket** — `good`, `needs-work`, or `poor`.
- **A handful of plain numbers** — counts, timings, rates, percentages and sizes.
  For example: milliseconds spent loading modules before startup finished and how
  many modules were loaded, kilobytes of memory growth per request, worker
  restarts per hour, subprocess spawns per minute, how many errors were logged in
  the window, queue and lock waits in milliseconds, garbage-collection pauses,
  pending task counts, thread and worker counts, hours of heap headroom left,
  megabytes read from and written to storage, major page-fault and
  context-switch counts, or how many open descriptors are sockets versus files.
- **Nothing else.** No message text, no log lines, no file, module, thread,
  worker or command names, no source code, no values from your app, and nothing
  about your users.

**The meter set grows over time, and the boundary does not.** New meters ship
regularly and they differ per runtime (a Python service reports things a browser
cannot, and the other way round). Every meter — the ones shipped today and the
ones added later — stays inside the same limit: a score, a rating bucket, and
numeric counts and timings. That limit is enforced, not just promised. The server
keeps a fixed allowlist of numeric meter fields, silently drops any field that is
not on it, and rejects any allowlisted field whose value is not a plain number —
so no prose can ride in a slot that should hold a measurement. Even the one-line
caption a kit computes for its own on-device display is dropped at the door and
rebuilt on our side from the numbers. A genuinely new *category* of data would
require a change to this document, not just a new meter.

Two promises spelled out, because these meters count things that sound sensitive:

- **A logged-error meter counts errors; it never carries the log.** We receive
  how many errors your app logged and survived during the window and the rate per
  hour — never the message, the log line, the logger name, the level, the stack,
  or any value.
- **A subprocess meter counts spawns; it never carries the command.** We receive
  how many subprocesses were spawned per minute and the total for the window —
  never the command line, its arguments, its path, its environment, or its
  output. In the same way, thread, worker, handle, timer, module and descriptor
  meters report only *how many*, never their names — a descriptor meter says
  how many open descriptors are sockets, pipes or files, never what any of them
  points at.


### The whole route list (the app map, including pages nobody visited)

Alongside the meter snapshot, and on **exactly the same two switches**, some
kits send **the list of routes your app has** — not only the ones traffic
reached. The kit asks your own running framework for its route table (Express,
Fastify, Koa and Hapi; FastAPI, Starlette, Flask and Django; Vue Router or
React Router if you hand the kit your router; React Navigation on React Native
if you hand the kit your navigator's screen configuration). It is a question
put to a running framework through the framework's
own interface. **Boosthis never reads your source code, never opens a file of
yours, and never crawls or clicks through your app to discover pages.**

**Be aware of what this adds, because it is the one thing here that travels
before any traffic does.** Until now, a route label only reached us because a
real request went to it. A route list includes paths your traffic would never
have revealed — an admin page, an internal webhook, a feature not launched
yet, a route left behind. If the *existence* of a path is sensitive to you,
this is the setting to turn off.

- **What each entry is.** A code-defined route template and nothing else — for
  example `GET /orders/:id`, `/admin/users/<int:user_id>`, or a screen name
  like `Checkout`. Parameters arrive as the template your framework holds, so
  a real order number or user id is never in it. Each entry also records
  whether it came from the framework, from a list you declared by hand, or
  both.
- **What each entry is not.** No timings, no counts, no scores and no ratings
  ride on a route the kit has not measured — an unvisited route carries a
  name and nothing else. We never send query strings, handler names, file
  paths, source, or anything a request carried.
- **The same guard, and a stricter disposal.** Every label goes through the
  same PII guard that governs route labels on samples. A label that fails it
  is **dropped entirely** rather than redacted, because a redacted route
  would otherwise draw on your map as a page of your app.
- **It is capped, and says so.** At most 200 routes travel; the list states
  how many the kit found, so a larger app reads as "at least this many"
  rather than as a complete picture.
- **How to turn it off.** `BOOSTHIS_ROUTE_LIST=0` (Node and Python), or
  `setRouteListEnabled(false)` (browser and React Native). With it off, the
  kit says so rather than going quiet, so your map can tell "switched off"
  from "this kit cannot ask". `disableTelemetry()` /
  `boosthis.disable_telemetry()`, `forget`, and `BOOSTHIS_DISABLED=1` all stop
  it like everything else.
- **Where it cannot be done at all.** Only the kits named above can ask. Every
  other runtime's map shows the pages traffic reached and says so on the page,
  rather than implying your app has no others.
- **A quiet route is never called broken.** A route the framework lists and
  traffic never reached is shown as **not seen** — never "dead", "unused" or
  "broken". We cannot know why it is quiet, and we do not guess.


### The controls on a page (the pins on the map)

On **exactly the same two switches** as the meter snapshot, a kit that lives
inside a rendered interface also sends **a count of the controls on a
rendered page** — and which of them have never been pressed. It is the same
question the route list puts to your framework, one level down: the kit is
already inside your page, so it asks the page how many interactive elements
it has. **Boosthis never reads your source code, never opens a file of yours,
and never crawls, clicks or drives anything to discover controls.**

**Structure travels; text never does.** Every product that can tell you which
button is broken earns that by recording the user's screen. This one does not
look.

- **How a control is identified.** By **a hash of its position in the page**
  and nothing else. Not its text, not its label, not its accessibility name,
  not its value or placeholder, not its id or class — nothing a person typed
  is read at all, and what travels is a short opaque string no name, id or
  value can produce. Boosthis has a count and a set of positions, and **no
  name for any of your controls**.
- **What rides alongside it.** Only what kind of control it is, from a fixed
  list of five words (press, link, field, choice, other), whether it has been
  pressed while Boosthis was watching, and whether pressing it opened another
  page. Where it did, **the route name a control was seen to open** travels —
  a route name of exactly the kind your app already sends, through exactly
  the same guard, dropped entirely rather than redacted if it fails. A
  control that was pressed and left you where you were records that it
  stayed, which is a fact about the control and not a fault with it.
- **Which control was pressed and did nothing.** A kit inside your page has
  always counted how many presses on it did nothing at all — no navigation,
  no change, no scroll. That whole-page count is unchanged. What is added is
  *which control it was*: alongside a control's position, the entry carries
  **a count of presses that did nothing**, a count of presses that changed
  something, and a count of presses the kit could not judge either way. A
  control that changed something is reported as having **responded** — never
  as *correct*: Boosthis sees that something changed, never whether it was
  the right thing. Nothing about the press itself travels: no coordinate, no
  time of day, no order, nothing about the person who made it. A press we
  could not attribute is counted apart from both and never as a fault.
- **It is capped, and says when it stopped early.** **At most 300 controls**
  travel individually. The scan also stops on a very large page, or at its
  own few-millisecond budget, and says which — so the count reads as "at
  least this many" rather than as the whole page. A control behind a closed
  menu or an unopened dialog is not rendered, so it is not in the count.
- **How to turn it off.** `setControlCensusEnabled(false)` (browser and React
  Native), or `BOOSTHIS_CONTROL_CENSUS=0`. With it off, nothing about controls
  is collected at all, and the kit says it is off rather than going quiet, so
  your map can tell "switched off" from "this kit cannot ask".
  `disableTelemetry()`, `forget`, and `BOOSTHIS_DISABLED=1` all stop it like
  everything else.
- **Where it cannot be done at all.** Only a kit inside a rendered interface
  can ask. The React Native kit sends the block and says in it that it sees a
  control was pressed but not *which* one — its touch observation carries a
  timestamp and nothing else, so it reports no dead presses against any
  control rather than a number you could not act on. For every server-side
  kit — Node, Python, Java,
  Go, PHP, .NET, Ruby, Rust, Elixir and edge — there is no interface to ask,
  so **nothing at all is sent**, and your map says that in words for that
  part of your app rather than showing a page with no controls on it.
- **An unpressed control is never called broken.** A control the page has and
  nobody has pressed is shown as **never pressed** — never "dead", "unused"
  or "broken". We cannot know why it is untouched, and we do not guess.

### What the phone platform allows (phone apps only)

The four phone kits — React Native, Flutter, Swift/iOS and Android/Kotlin —
report, alongside the meter snapshot and on **exactly the same two switches**,
a short closed list of observations about **what the operating system let the
app do**. Nothing here is about your app, your handset, or the person holding
it.

Why it exists: a phone app is held to more rules than any server — background
execution windows, doze and app-standby, per-app memory ceilings the system
enforces by killing you, timers that quietly stop — those rules changed with
almost every OS release, and the published documentation is frequently wrong.
So each kit records what actually **happened** on the platform it was running
on, we pool it across installs, and the rules Boosthis gives you for phones are
then written from what the platform really does rather than from what it says
about itself.

Exactly this travels, and nothing else:

- **The platform's name and one version number** — the operating system word
  (`ios`, `ipados` or `android`) and either the iOS major version or the
  Android API level. The version is here for one reason: a rule that changed
  between two OS releases, filed under a single name, is a *wrong* answer
  rather than a vague one. Both are read from the operating system's own
  version report; nothing is inferred about the handset.
- **The manufacturer — Android only, and only from a short fixed list** —
  Samsung, Xiaomi, Google, Sony and a handful of others whose own battery
  management genuinely changes whether a background job runs at all. A maker
  not on the list is dropped rather than sent, because a rare name is closer to
  naming one handset than to naming a platform. Never the model, the device
  name, a device identifier, the OS build string, the carrier, the locale, the
  screen, the battery, or anything else about the phone.
- **Up to seven yes/no observations, each of something the kit watched
  happen** — whether work already in flight finished while the app was in the
  background, whether a timer already scheduled fired while it was away,
  whether the system warned the app before it ran short of memory, whether the
  app could read its own memory ceiling and its own CPU clock, and, on Apple
  platforms, whether the system will say how much background time is left and
  whether that number counts down. Each arrives as a plain `1` or `0`, and a
  fact this kit could not observe is **left out of the block entirely** rather
  than reported as a `0` — a limit we never watched being reached is not a
  limit we may claim. No measurement is taken to answer these: each one is a
  by-product of work the kit was doing anyway.

**These are facts about a platform, not about a device.** They are pooled per
platform band: what survives is "on Android API 34, this many installs saw a
background job run in the background and this many did not". No line is shown
until at least **three separate installs** have reported it, each install
counts once per fact however many times it uploads, and nothing in the pooled
record can be traced to a particular phone. Your own install row additionally
remembers **which band and maker it reported** — that, and nothing more about
the device — so your dashboard can tell you which platform your project runs
on; `forget` and account closure delete it with the rest of the row.


### Three phone readings the React Native kit takes without being asked

Most of what the React Native kit measures, it measures because your code
asked it to — you time a screen, you wrap a call. **Three readings are
different: the kit starts them itself** when `enableTelemetry()` runs, with no
setting to pass. They ride the meter snapshot and its **same two switches**,
they stay inside the meter boundary above — a score, a rating bucket and plain
numbers — and each one can be refused by name.

They start themselves because the alternative was worse. Left as switches, all
three sat off in ordinary apps for ever, and their meters said "measuring…"
about something no code had started. A reading nobody can get is not a choice
we were offering.

- **How long your app's own requests take, and how they ended** — the kit
  observes your app's global `fetch` and `XMLHttpRequest` and records two
  values per attempt: a duration in milliseconds, and one word for how it
  finished (`ok`, `error`, `timeout` or `near-hang`). There is **no field
  anywhere in that path** for a URL, a host, a path, a query string, a header,
  a request or response body, or a status code — not dropped later, not
  redacted: never read. Your app's behaviour is unchanged, because the kit
  calls your transport's own implementation, hands back the exact object it
  returned, and re-throws its exact error. Boosthis's own uploads are excluded,
  so an app that makes no requests of its own reports nothing here.
- **A count of unhandled errors and unhandled promise rejections** — how many
  of each happened, and nothing about any of them: never the error, never its
  message, never its stack, never its type. The kit **chains** the app's
  existing handler rather than replacing it — yours is always called, with the
  original fatal/non-fatal flag — so your own crash reporter and React
  Native's red box behave exactly as they did before.
- **How much bigger an image was than the space it filled** — for React Native
  `<Image>` elements the kit reads four numbers as each image loads and lays
  out: the decoded width and height, and the displayed width and height. Never
  the image, never its URL or file name, never anything drawn in it.
- **How to refuse any of them.** Pass `autoWrapNetwork: false`,
  `trackUnhandledErrors: false` or `trackImageWeight: false` to
  `enableTelemetry()`. The kit then wires nothing for that reading, **and the
  meter says it is switched off in this build** rather than going quiet or
  claiming to measure. `disableTelemetry()`, `forget`, and
  `BOOSTHIS_DISABLED=1` stop all three like everything else.

### The React Native screen circuit (off unless switched on)

`traceScreens` is absent or false by default. With it off, nothing changes:
there are no screen spans, no call spans and no trace header on any request.
The blind Network axis is separate and unchanged; it still reads no URL, host,
path or status.

- **What a traced screen sends** — the navigator's **route name** for each
  screen the app moves to, how long it took and its place after the previous
  screen. A route name is code the developer wrote in the navigator. The kit
  never reads screen params, titles or anything a person typed.
- **What a call from that screen sends** — its method, an **id-redacted path**,
  how long it took and whether it failed. Never its query string, request or
  response body, headers, response status number or destination host. The trace
  header is written only to a host the developer named in `propagateTo`; name
  no host and it is written nowhere.

### Only in the optional detailed crash mode (opt-in, per app)

A specific app can be switched into **detailed crash mode**. When it is, a crash
report may additionally include — still scrubbed and still passed through the PII
guard before it leaves the device:

- **The first line of the error message — but only if it passes the PII guard.**
  Boosthis takes just the first line; if the guard detects anything
  identifier-shaped in it (an email, token, IP, phone number, etc.) the **entire
  line is dropped** rather than sent. It is never partially redacted, and only
  the first line is ever considered.
- **Sanitized stack frames** — each reduced to a function name + file *basename* +
  line/column. Absolute paths, URLs, query strings, function arguments, and local
  variables are never included.

This mode is off unless you turn it on, and even then it never adds your source
code, raw stack traces, or any user value.


### Build maps you upload (so crash frames read as source)

A production crash frame reads as a compiled position — `a.b.c
(bundle.js:1:48291)`. To make it read as your own file and line, a developer
can upload the **build map** their build already produced: a JavaScript source
map, an Android `mapping.txt`, an Apple dSYM, a Dart symbols file or a Portable
PDB. **Nothing is uploaded unless a developer does it** — by hand from the
project page, or by a step added to your own build. **No kit fetches a build
map's contents**, and Boosthis never asks your build system for one; a project
that has uploaded none holds none. One kit asks a question *about* a map rather
than for its contents: the browser kit's development-posture check makes one
HEAD request to your own site for the first script's `.map` address and keeps
only whether the answer was OK, as a 1 or a 0. Nothing is downloaded, and the
address is neither stored nor sent.

**What is kept, and what is discarded on arrival.** The file is parsed the
moment it arrives and only the parts needed to place a frame survive:

- **The original source paths that build recorded** — or, for an Android
  mapping file, the original class names. They keep the directory chain the
  build wrote, because a crash frame already carries the file's basename and a
  resolved frame that adds no directory adds nothing. Bundler schemes are
  stripped, an absolute machine path is cut back to its last few segments, each
  path is length-bounded, and every one is screened by the same PII guard that
  screens telemetry — a path that trips it is replaced with a visible
  "withheld" marker rather than stored.
- **The original identifier, method or symbol names** from that build.
- **The table that joins those to positions in the compiled output.**

**The uploaded file itself is not kept**, and `sourcesContent` — the copy of
your actual source text that a source map may embed — is **discarded at the
door and never written down**. What a build map hands back, it hands back only
to you: your own crash frames resolved to your file, line and original
identifier name, and — in your data export — the list of maps held with their
source paths and expiry dates. The uploaded file is never served back to
anyone, the mapping table and the table of identifier names are never handed
over as tables, and no build map is ever an answer to anybody but you.

**How long.** Each build map is kept for **90 days from the day it was
uploaded**, then deleted. Uploading the same app version again replaces the map
already held rather than adding a second one, and restarts that clock. The
project page lists every map the project holds — which release, which generated
file, when it arrived and the date it ages out — warns 14 days before one ages
out, and says when a release crashes are still arriving under has no map. Your
data export lists the maps held, for which build, with your own source paths
and each one's expiry date. The reasoning behind both the clock and keeping the
full path is written down in `docs/decisions/build-map-retention-and-paths.md`.

### Reading your own data back over MCP (the "Connect your AI" card)

When you connect an AI agent — the dev-only "Connect your AI" card in the app,
or the "Connect AI" button in the web dashboard — the data your app has already
uploaded can be **read back** by that agent, so it can answer "which screen is
slowest, and what should I fix first?" with your real numbers instead of a
placeholder. The agent can read your whole performance picture for the app: the
per-screen meters you see in the dashboard (boot ladder, Speed, Smoothness,
Scroll, Stability, Render, the Frustration and Idle axes), the health-meter
numbers described above (a named set that grows over time — always scores,
rating buckets, counts and timings), the per-route rows, the session summary,
and the budgets. This is opt-in and stays under your control:

- **A dedicated, read-only token.** Reading is authorized by a **separate
  read-only token** — not the token that erases your data. The read token can
  read **only your own install's** already-uploaded data; it **cannot** delete
  your data (`forget`), submit new telemetry, or touch any other install. Erasing
  your data still requires the separate delete token.
- It only happens when **you** copy your install's read credentials
  (`install_id` + the read token, shown in the in-app dashboard's dev-only
  "Connect your AI" card) into your AI's MCP configuration. Nothing is read back
  until you do this.
- The agent reads exactly the data your app already sent (per-screen meters,
  route labels, durations, ratings, and the crash codes already recorded) —
  **no new categories of data** are collected or exposed by this feature. The
  same PII guard still applies to every read.
- **Your recorded crashes too — the "potential crashes" feed.** The connected
  agent can also read back the crash codes your app has *already* recorded
  (described above under **Crash reports** — the error type, the redacted code
  location, and how often each recurred), newest first, joined with your app's
  stability (app-not-responding) signal — so it can answer "what is most likely
  to crash, and what should I harden first?". This reads the **same** always-on
  crash data already collected for a registered app — **no new categories of
  data** — and is **read-only and scoped to your own install**, using the same
  read-only token and the same PII guard as every other read.
- **The hosted Boosthis MCP server can serve this read** when — and only when —
  your read credentials are presented **as per-call arguments** on the request.
  The hosted server never stores those credentials between calls and never reads
  one app's data on behalf of another: each call only ever sees the install named
  by the credentials passed into that call. With no credentials, the hosted
  server returns the on-device note instead of any live data.
- **Treat the read token like a password — and note it will appear in your AI's
  transcript.** Because you paste it into your AI agent's tool configuration, the
  read token (and your install id) will, by design, appear in that agent's tool
  call history / transcript. Anyone who can read that transcript or copy the
  token can read your install's uploaded performance data (read-only — they still
  cannot delete it or send data as you). Keep it on your own machine and to
  agents you trust. `forget` / `BOOSTHIS_DISABLED=1` stop all of this like
  everything else.
- **What a tool reply can contain — the exact identifier categories.** A reply
  served to a connected AI is limited to: your **anonymous install ID** (the
  same UUID described above — the connection-status tool lists it per app so
  you can tell your installs apart and match the "Connect your AI" card);
  **trace and span correlation IDs** (a random 32-character hex value minted
  inside your app purely to stitch one user action's spans into a waterfall —
  never derived from your device, user, account, or install ID); code-defined
  route/screen labels; durations, ratings, and scores; crash signatures (error
  type plus a redacted code location); **coarse timestamps** (when a sample or
  crash was recorded, and when an app last checked in); **connection-status
  facts** (each install's coarse connection status, when it was registered,
  whether it has ever checked in, whether a measurement has ever arrived from
  it, when the most recent one arrived and a coarse band of how many have
  arrived, and a yes/no of whether server-side credentials are on file — never
  any credential value, and never a measurement value itself); the **project
  name and short code the reply is about** (the name you gave that project on
  your dashboard, plus the first characters of its project key's fingerprint,
  so a connected AI holding two connections can always say which project a set
  of numbers belongs to); the **app name and runtime label of the install being
  read** (the name your app reported when it registered, so a connected AI can
  tell one measured place of a project from another instead of judging the
  whole project on half its numbers); rule IDs
  and fix guidance from the rule book; and — inside the integration-kit reply only —
  an echo of the **same project key that connection itself presented** (so the
  kit can self-register; never anyone else's key or any other credential).
  Tool replies never include account data, emails, IP addresses, internal
  database row IDs, or any other install's data.

## Checking a sentence your assistant wrote

Nothing your assistant writes is sent to be checked on its own, and Boosthis
never reads a session to get it: a sentence arrives for checking only when one
of two things you set up sends it. The first is the Claude Code Stop hook Boosthis publishes on
your dashboard and in the private setup guide — configuration you add to your
own Claude Code settings, needing both your project key and your owner account
token. Until you add it the hook sends nothing, and deleting it from those
settings stops it. The second is the claim-check tool offered to any assistant
you connect to Boosthis: the assistant can call it with one sentence at a
time, and only the sentence it was asked about is sent. Neither lets Boosthis
take anything that was not submitted to it.

What the hook sends is at most 4 of the closing lines your assistant has just
written, each no longer than 240 characters, with fenced code blocks removed
on your own machine before anything leaves it. Not the conversation, not your
prompts, not a file, not your repository. They are the closing lines as they
were written, so if your assistant ended a turn with a line of code or a diff
line that was not inside a code fence, that line can be one of them. Each line
then goes through the same screen a written promise goes through — length,
credentials, personal data — before it is read against your project's
measurements or answered, and one that fails the screen is not read, not
answered and not stored. One thing is taken from a line before that screen
runs, and only where your project has no kit installed: whether the line
states a number or a length of time. That single yes-or-no fact is recorded in
the row described next, with no part of the line beside it, and nothing else
happens to the line — with nothing measured to check it against, it is not
read, not answered and not kept.

The sentence itself is not kept, with one exception set out next. What is
stored is how it was read: one row holding which of a closed list of outcome
words applied, which reader ran, and a few yes-or-no facts about the
sentence's shape — no account, no project, no install, no subject and no text,
so the row cannot be tied back to you or your project by anyone holding the
whole table. Where Boosthis worked out what the change was aimed at, it also
records that aim in your project's own list of witnessed changes: the measured
subject's label, which measurement of it, the line it was held to and the
judgement. That is what Boosthis read the sentence as, in its own bounded
words, and again never the sentence.

The exception is a sentence that reads as a promise about something your
project measures and that Boosthis cannot judge yet. That one is recorded, in
its own words, as a standing promise on your project — screened first, capped
in number, listed on your project page beside everything else you have
promised, and removable there at any time. Standing promises are covered
above: they appear in your data export and are deleted when the project is
erased or the account closed. Where the wording cannot be matched to something
your project measures, nothing of it is recorded.

A sentence the plain word match cannot read may be sent once to the AI
provider named above, to be mapped onto one subject your project already
measures, and only on a call that proved the account owns the project. What
the hook sends never goes that way: it is read by the plain word match alone
and reaches no model provider.

Checking a sentence is not the only thing a connected assistant can send in
words it wrote itself. Holding your project key and your owner account token,
it can also file a one-line summary of a change it has just made (at most 300
characters, with an optional screen, endpoint or job name of at most 80), name
one thing it is about to build so your project page can later say whether it
appeared (at most 80 characters, at most 24 outstanding at a time), and write
down a standing promise as described above (at most 240 characters). Each is
one short line the assistant chose to send, never a transcript, a file or a
diff, and each goes through the same screen for personal data and credentials
before it is stored — one that fails is refused whole and nothing of it is
kept. What is stored is kept on your project, shown to you on its own pages,
and deleted when you erase the project (forget) or close your account.

## Reading your own chat messages in your editor

The Boosthis extension for Visual Studio Code can read the messages you typed
to an AI assistant, on your own machine, and turn them into the list of things
you asked for on your project's board. It is off until you turn it on, it is
turned on for one project at a time rather than for your machine, and there is
one switch. Nothing is read for a project you have not switched on, and
switching it off stops the reading.

Only your own messages are read. Your assistant's replies are not read, not
parsed and not kept, anywhere, including on your own machine — they are where
code, diffs, stack traces, database rows and credentials end up, and an
assistant's account of having finished something is no evidence in any case.
The extension opens the conversation files your assistant already keeps on your
disk, reads them, and writes nothing back to them.

No conversation text reaches Boosthis. Your messages are read, the names of the
things you asked for are worked out, and each name is screened for personal
data, credentials and addresses — all of it on your machine, before anything is
sent. What is then sent is the same thing the connection already accepts from
an assistant: a work-item name of at most 80 characters, the time it was asked
for, and that it is open. Never a message, a prompt, your reasoning, a file, a
diff or any code. A name that fails the screen is not sent at all, and no
shortened version of it is sent instead. You can see the exact list before it
goes.

Every item on your board says where it came from — told by your assistant
through the connection, or read from your own messages — on the wire, in
storage and on every screen that shows it. The two are never merged into one
list of items that simply appeared.

Nothing read from a conversation can ever mark a piece of work as finished. An
item is closed only when Boosthis has seen that part running in your app, or
when you close it yourself. A name read from your own messages is a phrase you
typed rather than a route your app registers, so in practice you are the one
who closes it. Boosthis also publishes which assistants this
reader can read and which it cannot, with the reason for each, so a tool it
cannot read is never left to look like a tool with nothing to report.

A tool Boosthis cannot read is not a tool with nothing to report. Where there
is no reader, nothing about that assistant's conversations reaches your board
at all, and the reason is stated rather than left as a blank row.

| Assistant | Read? | What is read, or what is missing |
|---|---|---|
| GitHub Copilot Chat (Visual Studio Code) | Read | Your own messages: `requests[].message.text` (with `requests[].message.parts[].text` where the editor split it), and the request's own `timestamp`. Not read: `requests[].response`, `requests[].result` and every other field of the entry: that is the assistant's half. |
| Claude Code (command line) | Read | Your own messages: Lines with `type: "user"`, taking `message.content` when it is a string and the `text` of its `type: "text"` blocks when it is a list. Not read: Every line with `type: "assistant"`, and every other line type in the file. A `user` line carrying a tool result, or marked `isMeta`, is written by the tool rather than typed by the developer, and is skipped. The directory name flattens the project's path, so two projects whose paths differ only in punctuation share one directory; a session is read only where the session's own `cwd` records this project, and one recording another project, or none at all, is skipped. |
| OpenAI Codex CLI | Read | Your own messages: Lines whose message has `role: "user"`, taking the `text` of their `input_text` content parts, whether the message sits at the top level or under `payload`. Not read: Every line whose message has any other role, including the assistant's own. |
| Continue (Visual Studio Code) | Read | Your own messages: `history[].message.content` where `history[].message.role` is `user`, taking the `text` of its parts when the content is a list. Not read: Every turn with any other role, and the `contextItems` beside each turn — those carry file content. |
| Aider | Read | Your own messages: Lines beginning `#### `, which is the prefix Aider writes in front of what the developer typed and in front of nothing else. Not read: Every unprefixed line — the assistant's prose — and every fenced block. The transcript carries no timestamps, so an item read from it is stamped with the time of the read rather than the time it was asked for, and the preview says so. |
| Cline (Visual Studio Code) | **Not read** | What is missing: anything beside the conversation saying which project the task belonged to. Its tasks are readable — the panel's own events are in a JSON file, and the developer's replies are marked in it — but every task for every project sits in one directory, and Boosthis has found nothing stored beside a task that records the folder it ran in. Reading them would put asks typed in another repository onto this project's board, and this switch is per project. There is a second problem even where that is solved: the message that OPENS a task is stored with the same marker as the assistant's own prose, so a developer's first ask cannot be told apart from a reply. |
| Cursor | **Not read** | What is missing: a conversation store in a file that can be read without a database driver. Cursor keeps its conversations inside a SQLite database in its own user directory rather than in files. Reading it would mean shipping a native database driver inside the extension, and the keys the conversations sit under are undocumented and have changed between releases — a reader written against them would quietly start reporting the wrong half of a conversation the first time they moved. |
| Windsurf | **Not read** | What is missing: an on-disk conversation store on the developer's machine. The conversation is held by the service rather than written to the developer's disk, so there is nothing local to read. Anything Boosthis could obtain would have to come from the provider, which would put Boosthis inside the conversation — the one thing this feature refuses to do. |
| Gemini CLI | **Not read** | What is missing: a store whose shape we have verified, with the developer's own turns marked in it. It writes session files under the home directory, but the layout is undocumented and has changed between releases, and Boosthis has not verified which entries are the developer's own typing. A reader written against a guess would either miss the asks or report the assistant's words as the developer's, and both are worse than an empty row that says so. |
| JetBrains AI Assistant and Junie | **Not read** | What is missing: a Boosthis extension in that editor. This reader ships inside the Boosthis extension for Visual Studio Code, and these run in the JetBrains IDEs. Nothing about their store has been examined, and nothing is read from a machine using them. |
| Assistants used in a browser (ChatGPT, Claude, Gemini on the web) | **Not read** | What is missing: any local copy of the conversation at all. The conversation exists only on the provider's servers. There is no file on the developer's machine to read, and Boosthis does not ask a provider for one. |

## Account data (the optional developer account)

If you create a developer account to use the hosted web or mobile dashboard,
we store — separately from telemetry, and only to operate your account:

- **Your full name, your email address, and a password hash.** The name and
  email are required to create an account: the name identifies the account
  holder and becomes the "Billed to" name on receipts, and the email is how you
  sign in and how we reach you. An older account without a name on file is
  asked for one on the dashboard; nothing else about the account changes, and
  the name is never shown to the projects you monitor. The password itself is never stored, only a
  secure one-way hash. Sign-in uses short-lived, single-use one-time codes
  (emailed, or from an authenticator app if you enroll one, plus one-time
  recovery codes if you generate them), also stored only as hashes.
- **The Maintainer can see your account.** In the admin dashboard the
  Maintainer sees the account holder's **name and email address**, along with
  the plan, the number of projects, and the account's status (active, frozen,
  suspended). This is how accounts are supported, billing questions are
  answered, and abuse is spotted. It is never sold, shared with advertisers, or
  joined to telemetry.
- **Teammates and the workspace activity log.** If you invite a teammate, we
  store the email address you invited and, once they join, the link between
  their account and your workspace. Actions taken in a workspace (who invited
  or removed whom, who created or revoked a key, who changed the plan) are
  recorded in an activity log visible to the workspace owner and admins, kept
  for up to a year. No IP addresses are stored in it.
- **AI connection credentials (OAuth sign-in and "Connect AI" tokens).** If
  you connect an AI assistant through the OAuth sign-in flow, or mint an
  account AI token from your dashboard, we store that authorization so the
  assistant can act with its **own revocable, read-only credential** instead
  of your password or project key. These credentials are stored only in
  protected form (hashed, or encrypted where the sign-in flow requires it) —
  never as plain text — and they inherit the status of the key they were
  authorized with: revoking that project key immediately ends that assistant's
  access (dashboard-minted AI tokens can additionally be revoked individually
  from your dashboard). They grant no more than the read-only tool access
  described in the sections above.
- **AI read events (for the "AI impact" view).** When a connected AI assistant
  actually reads a project's data with one of the credentials above, we record
  the time of that read (collapsed so bursts count once per hour) together
  with a snapshot of the project's open crash-group counters at that moment.
  This exists solely to power the dashboard's honest before/after "AI impact"
  view; it contains only identifiers and counters we already store — no new
  content, and never anything about what the AI itself said or did. These
  events are deleted with the project's other operational data on erasure and
  pruned after a year.
- **Session records and essential cookies.** The web dashboard sets only
  essential cookies: your sign-in session, which workspace you are currently
  viewing, a short-lived cookie during the one-time-code step, a short-lived
  cookie during the code step that confirms a billing change, a short-lived
  cookie that carries a team invitation through sign-in, which environment
  you last chose to look at (development or production), a short-lived cookie
  during the step that connects your Slack workspace, and, only in a
  browser signed in as a Boosthis administrator, a marker that tells our own
  pages to measure themselves — never set for a customer, and it grants
  access to nothing. There are **no advertising, analytics, or cross-site
  tracking cookies**. The dashboard uses a small amount of your browser's own storage for
   convenience — for example, remembering that you dismissed the "update
   available" banner — not for tracking.
- **Account emails.** We send account emails — a welcome note when you sign
  up, sign-in codes, password resets, receipts and renewal reminders,
  plain-language notices when a payment is declined or your access or plan
  changes, performance-alert emails if you turn alerts on, an optional weekly
  summary of your projects, and occasional low-frequency update notes about the
  toolkit — through **Resend**, our email delivery provider, which processes
  your email address for delivery only (see "The companies that can see your
  data" below). If you configure an alert webhook, the
  alerts you asked for are also POSTed to the URL you supply; where that URL
  goes, and what the receiving service does with it, is your responsibility.
  If you connect a chat workspace instead, those same alerts are posted into
  the channel you picked there.
- **Billing status.** If you subscribe to a Paid Plan, we store which plan
  you are on, a reference to your subscription with the Payment Processor,
  and — so renewals work and you can recognize the card — your card's brand,
  its last four digits, and a reusable payment token issued by the Payment
  Processor (never the card number itself). **Your card details never touch Boosthis's servers** — checkout
  happens on the Payment Processor's own hosted pages under its own privacy
  policy (see Part A, Section 15).
- **A security log.** Sensitive account and maintainer actions are recorded in
  an append-only security log (including the acting IP address) so we can
  investigate abuse. It is a security
  record, not telemetry, and is never shown to other users.
- **Support messages.** If you write to support — by email or from the
  dashboard — we keep your message (your address, the subject, and the text) so
  we can reply and keep a record of the request. Once your request is closed we
  keep it for **365 days** and then delete it automatically; while a request is
  still open we keep it until it is closed, because we cannot answer you
  otherwise. Support mail is never joined to telemetry.
- **Questions you ask the assistant.** Questions put to "Ask Boosthis" and the
  answers returned are stored against your account so the conversation and your
  usage allowance work; see the next section for what is sent to the AI
  provider.
- **Standing promises you set on a project.** A project can hold a small set of
  short, plain-language **standing promises** — things you want kept as the
  project changes, such as "the list screen stays under a second". You write
  them on your dashboard, a connected AI assistant can write one down on your
  behalf, or you accept one Boosthis offers from your own numbers. We store the
  sentence itself, who wrote it, whether you have confirmed it, and the single
  measurement and threshold it maps to when there is one — and where there is
  none, it is labelled as remembered only, never as something we check. Promises
  are held against the **project key**, so every runtime of that project and
  every teammate on the account sees one shared set, and they are handed to any
  AI assistant that connects with that key. They are screened going in and
  coming out: a promise containing a credential, a token or someone's personal
  data is refused whole and nothing is stored. Chat transcripts, source files
  and documents are never stored this way — each promise is capped at a couple
  of sentences and a project holds only a handful. You can correct or delete any
  promise at any time; promises appear in your data export, are deleted with the
  project or the account, and are **never** deleted for non-payment.

Account data is **never merged with telemetry**: telemetry is keyed to
anonymous install IDs, account data to your email, and we do not join them for
profiling, advertising, or any other purpose. You can **delete your account
yourself**, without asking anyone: **Dashboard → Delete account**, or the same
option in the Boosthis phone app. You are shown exactly what is deleted and
what we must keep before you confirm, and you can download everything we hold
about you first (**Dashboard → Your data → Download**). `forget` erases your
telemetry at any time, independently of your account.

## The companies that can see your data

Boosthis is operated by **Veqtara Tech Company**. A small number of outside
companies are involved in running it, and each one is named here. This is the
whole list of companies **Boosthis engages**, and no other company we engage
receives your account data or your telemetry. Three destinations sit outside
that list because **you** chose them rather than us — an **alert webhook** you
configure, a **chat workspace** you connect, and an **AI assistant** you
connect — and each of those does receive what you point it at. They are
described immediately after the list.

- **Moyasar** — *payments.* Hosts checkout and processes subscription charges;
  this is the **Payment Processor** defined in **Part A, Section 2** and
  described in **Part A, Section 15**. **What reaches it:** your card details,
  entered on Moyasar's own pages and never on Boosthis's servers; the amount
  and currency of each charge; the billing name and email on the payment; and
  the reusable payment token that makes a renewal work. **Where it runs:** the
  **Kingdom of Saudi Arabia**. If you pay with Apple Pay, the payment sheet is
  additionally between your own device, Apple and Moyasar — Boosthis's servers
  only ask Moyasar to validate the session.
- **Resend** — *email delivery.* Sends the account emails listed under "Account
  data" above. **What reaches it:** your email address, the subject, and the
  body of that message. No telemetry, and nothing else about your account, goes
  with it. **Where it runs:** the **United States**.
- **OpenAI**, reached through a gateway operated by **Replit** — *AI
  processing.* Generates the answers from "Ask Boosthis" and helps draft
  candidate rules on our side. **Two companies sit in this path and both are
  named here, so the answer to "who processes this" needs no
  cross-referencing:** the call leaves Boosthis to Replit's AI Integrations
  gateway, on Replit's own account and credential, and Replit passes it to
  OpenAI, which generates the answer. Replit is also the hosting recipient
  named below; on this path it is the intermediary, and what it can see is the
  call itself. **What reaches it:** from your account, the
  two things described under "The 'Ask Boosthis' assistant" below — your
  screened question and a privacy-safe digest of numbers — and, for rule
  drafting, the aggregated privacy-safe signals described there. One thing more, on a different path: a single short sentence a developer
  or their assistant wrote — a promise being written down, or a claim
  submitted to be checked — sent once, after the wording screen has passed
  it, so the model can map it onto one subject that project already
  measures; see "Checking a sentence your assistant wrote" above. It also
  receives **the text of our own rule book**, which is ours and not yours:
  rule titles, the conditions that decide when a rule applies and the
  detector patterns behind them, and — on the maintainer-side drafting and
  review calls that cannot do their work without it — the prescriptive fix
  text itself. **OpenAI publishes that what its API receives is not used to
  train its models** — the provider's published position, not a setting we
  can show you for the account we reach it on; see "What the AI provider may
  keep, and what we cannot show" below. **Where it runs:** OpenAI is a **United States** company, and
  its published residency options do not include the Kingdom — so what
  reaches it leaves. Which country actually processes it is **not
  confirmed**, because we reach the model through Replit's gateway rather
  than an OpenAI account of our own; see "Where Boosthis runs, and whether
  your data crosses a border" below.
- **Replit** — *hosting and the database.* Runs the Boosthis servers and the
  managed PostgreSQL database, and operates the AI gateway above. **What
  reaches it:** everything this document describes as stored — account data,
  telemetry, and the operational database as a whole. **Where it runs:** on
  Google Cloud infrastructure in the **United States**.
- **Google Cloud** — *backup storage.* Holds the routine backups of the
  operational database described under "Retention" below, in a private bucket
  provided through Replit's App Storage. **What reaches it:** a copy of the
  operational database. **Where it runs:** the **United States**.

The three named in that sentence are **not** on the list above, because they
are yours rather than ours, and here is what each one is: an **alert webhook**
you configure (the alerts go to whatever
URL you supply, and what the receiving service does with them is your
responsibility), a **chat workspace you connect** (if you connect Slack, the
alerts you route there are posted into the channel you picked, inside your own
workspace, using a credential you granted us and can revoke at any time — Slack
is a company you chose, not one we engaged, and what it does with what it
receives is governed by your own agreement with it),
and an **AI assistant you connect** through "Connect AI" or the OAuth sign-in
flow (that assistant is your tool, reading your data with a credential you can
revoke at any time; whoever operates it is a company you chose, not one we
engaged).

**We never sell your data.** Personal information — your name, your email
address, your account, your billing details — is never sold, rented, traded, or
shared for anyone else's marketing or advertising, and neither is telemetry. No
money and no data change hands for advertising purposes. Each company above may
use what it receives only to do the job named beside it.

**Notice before this list changes.** Before another company starts receiving
account data or telemetry, it is named here first — with the revision date at
the top of this document moved — at least **14 days** before it begins, unless
a change has to be made sooner to keep the service running or secure, in which
case it is named here as soon as it takes effect. This document is the notice:
the same list stays in the same place, so a change is visible by comparing it.

## Where Boosthis runs, and whether your data crosses a border

Two questions, two answers. They are easy to blur, and the difference is
exactly what matters, so both are stated separately here. Both are taken from
the recipient list above and held to it by an automated check, so they cannot
drift from it, from each other, or from the same two sentences on the served
`/terms` page.

- Where the service runs: the Boosthis servers and the database behind them run
  in the United States.
- Whether your data crosses a border: some of it does. Moyasar holds what
  reaches it in the Kingdom of Saudi Arabia; Resend, Replit and Google Cloud
  hold what reaches them in the United States; what reaches OpenAI leaves the
  Kingdom of Saudi Arabia too, but which country processes it is not
  confirmed.

These are two answers to two questions, and neither one implies the other. That
the service runs in the United States does not mean nothing leaves it, and that
something leaves it does not mean the service runs anywhere else.

Every company outside the Kingdom is on that list because a part of the service
cannot be provided without it, and each receives only what is named beside it.
The basis for that processing is set out under "Your rights over your data"
below. If you would rather nothing crossed a border at all, a **general
install** of the toolkit sends nothing anywhere.

## The "Ask Boosthis" assistant (AI processing)

The dashboard includes an optional assistant you can ask plain-English
questions about your app's performance. When you use it, two things — and only
those two things — are processed by **OpenAI**, reached through Replit's AI
Integrations gateway (see "The companies that can see your data" above), to
generate the answer:

- **Your typed question.** It is checked first: a question containing personal
  data or a link is rejected rather than sent.
- **A privacy-safe digest of your app's already-uploaded performance
  snapshot** — numbers, ratings, and code-defined labels only. Free-form
  prose and anything identifier-shaped is dropped or blocked before it leaves
  the server, and the answer that comes back is screened again before you see
  it.

Nothing new is collected for the assistant, and it runs only when you ask a
question. OpenAI's published position for its API is that what it receives is
not used to train its models — that is the provider's published position, and
what we can and cannot show you about it for this account is set out under
"What the AI provider may keep, and what we cannot show" below.

**Rule drafting on our side.** Separately from the assistant, the Maintainer
uses the same provider — OpenAI, through the same gateway — on a schedule to
help draft candidate rules from the aggregated, privacy-safe signals described
above — rule kinds,
severity and timing buckets, redacted code locations, and occurrence counts;
never route labels from full samples, never your question, and never anything
identifier-shaped. Drafts only ever land in the maintainer's private review
queue, and a human decides what ships (see "How candidate rules become real
rules"). This is rule-book improvement — the single purpose described above.
The provider's published position on training covers this traffic too, subject
to the same limit on what we can show you.

**What the AI provider may keep, and what we cannot show.** The sentence above
rests on **OpenAI's own published position for its API**, which says that what
it receives is not used to train its models. We read that position on
**14 September 2026**. It is the provider's default for API traffic, and
switching it off is a choice made on an account — and we reach the model on
**Replit's gateway account**, not one of our own. So we can show you the
provider's published default; we cannot show you that this particular account
runs on it.

**How long a prompt survives is not established, and we record that as unknown
rather than assume it.** OpenAI's published default deletes an API input after
**at most 30 days** of abuse monitoring, with immediate deletion available only
by prior arrangement, which we have not sought. Whether the gateway operator's
account runs on that default is not published. The gateway operator does
publish a position of its own, which we read on **15 September 2026**, and it
settles nothing here: it states that training is disabled for paid model
providers, and that zero-retention endpoints are enforced for **Enterprise
accounts only**. Neither branch names the plan this path runs on, and the
gateway publishes nothing about what it logs or for how long. A prompt may
therefore survive in
two places on this path, and we are not in a position to tell you for how long
at either. Settling it needs an answer from the gateway operator; until then
this paragraph says so.

**Does your own contribution reach a third-party model?** What a customer sends
— a question, a measurement — is covered above. The rule book's own text is
ours rather than yours, and it does reach the provider when we draft and review
rules: the prescriptive fix wording for a rule under review, and for the
language passes, the rule being translated. Every call that carries it is
listed in our code, with the reason the answer needs it, and the sending of it
is bounded — a drafting call carries one rule, or two fixed examples, never a
sweep of the book.

## Security reports from our own website

Pages on the Boosthis website carry a **report-only** browser security policy.
If a page tries to load something that policy does not list — most often a
browser extension rewriting the page, occasionally the signature of injected
code — your browser sends us a short report. Nothing is ever blocked on your
side; the policy only reports.

From that report we keep three coarse things: which kind of content was
involved (for example a script or an image), the **host name only** of where it
came from (never a full link, path, or query string), and a stripped-down label
for the page it happened on (identifier-shaped parts replaced). No IP address,
no account, no session, no device, and nothing else about you is stored, and
identical reports are counted rather than stored twice. These reports are about
the security of our own website, are visible only to the Maintainer, and are
deleted after 90 days.

## Counting visits to our website

We count visits to the public Boosthis website — the pages anyone can open
without signing in — so we can tell how many people come and what they read.
It is a counter, not tracking: **no cookie is set on any visitor**, no script
of ours runs in your browser on those pages, and nothing kept can be tied back
to you.

For each public page we answer we add one to a small set of counts, kept per
hour: which page it was, which of our public addresses it was opened on, the
**host name only** of the site that linked to you (never the full link), an
approximate **two-letter country**, a coarse **device class** (phone, tablet or
computer) plus a broad browser family, and whether the request named itself as
a known crawler. That is the whole of it — counts against those labels, never a
record of a visit, and only counts for pages we already publish by name
(anything else is counted as simply "other").

Your **IP address is never stored**. It is used in memory for two things and
then discarded: working out the two-letter country (on our own server, from a
table that ships with the software — your address is never sent to anyone
else), and building a **daily de-duplication tag** so the same visitor counts
as one person that day rather than one per page. That tag is a one-way hash
salted with a secret *and with the day itself*, cannot be turned back into an
address, and is deleted within days — which is exactly why we cannot tell a
returning visitor from a new one, and cannot follow anyone from one day to the
next. The full browser description and the full referring link are discarded in
the same breath; neither is ever written down.

The country is approximate and can be wrong (a VPN or a company network moves
it), and where it cannot be told it reads "unknown" rather than being guessed.
These counts are visible only to the Maintainer.

## What the toolkit stores on a device

The browser kit keeps a small amount of state in the browser's own **local
storage**, on the site it is running on. **The kit sets no cookie in the
application it runs in**, and nothing it writes is readable by any other site.

Every entry's name begins with `boosthis:`, so the kit never collides with —
and never enumerates — the application's own storage. What is kept is:

- the install's own identifier, the project it reports to, and the token that
  lets that application read its own measurements back;
- the version of the Boosthis notice the user agreed to, where the
  application shows one;
- measurements taken but not yet uploaded — a crash waiting to be sent, a
  page-view tally, the page map, and the baselines a candidate rule is
  compared against;
- the cached answer to whether the install is still switched on, so that a
  page load does not have to ask again.

None of it is a profile and none of it is shared: it is the state one install
needs in order to keep working across reloads. Where the browser refuses
storage — private mode, a sandboxed frame, storage switched off — the kit
falls back to memory that dies with the page rather than failing. Clearing the
site's data removes all of it, and so does the kit's own `forget()`, which
additionally erases what has already been uploaded.

## What we never collect

- IP addresses — never as part of telemetry, and never used to identify you
  or joined to your telemetry. (Like nearly every web service, the server
  does briefly count requests per connecting IP address purely for
  abuse-prevention rate limiting, and keeps routine, short-lived operational
  request logs; both expire automatically and are never used for profiling
  or joined to telemetry. The website visit counts described above also read
  it in memory — for an approximate country and a same-day de-duplication
  tag — and never store it.)
- User IDs, emails, names, phone numbers, addresses, device IDs, or any other
  identifier — the 83-entry PII denylist blocks them at both ends. (The one
  email we ever hold is the one you give us if you create an optional
  developer account — stored separately, never joined to telemetry; see
  "Account data" above.)
- Request bodies, response bodies, query strings, headers — none of it is
  collected, and none of it is uploaded.
  Reading something inside your process and collecting it are two different
  things, and this is the whole of the first. The one exception is the AI-call
  meter: to tell repeated prompts apart it reads up to the first 4,096
  characters of an outbound request body to an AI provider, inside your own
  process, and reduces it immediately to a single number — no body text is
  stored, uploaded or recoverable. Eight other bounded reads work the same way
  — inside your own process, reduced on the spot to a number, a flag or a
  label from a fixed list: the same meter reads the provider's reply as it
  streams back, holding at most 8,192 characters of a partial line at a time,
  to take the token counts the provider states in it; when a provider refuses
  a call, the meter takes its own copy of that error reply to say which kind
  of refusal it was — the Python kit stops accumulating at 8,192 characters,
  and in Node and the browser the runtime hands the reply over as one string,
  of which anything longer than 8,192 characters is dropped without being
  examined; the leak watch reads up to the first 8,192 bytes of an ERROR
  response your app is about to return, to see whether a stack trace or a
  secret-shaped string is about to reach one of your users; in the browser,
  and only if you switch it on, a copy of a JSON answer to one of your app's
  own calls is read to see whether it is error-shaped despite its 200: the
  copy is taken only where the server declared a length of 65,536 bytes or
  less, and because a declared length can understate what arrives, the copy
  the browser hands back is checked again and dropped unexamined past 65,536
  characters; on an MCP endpoint your app serves, up to the first 64 KB of the
  JSON-RPC request is mirrored as your app reads it, to take the method name
  out of the envelope — never the arguments, and never a byte of what the app
  itself receives is changed; the cookie check reads the Set-Cookie headers
  your app sends — at most 20 per response, and at most 2,048 characters of
  the attribute tail on each, which is 2,048 bytes in the kits whose strings
  are bytes — for the Secure, HttpOnly and SameSite flags and the size, never
  the name and never the value; the Cookie header your users send is not read
  at all; a fixed list of headers is looked up by name and turned straight
  into a number or a fixed label: the content type, length and encoding, the
  Accept header your user's browser sent, whether your reply is marked as a
  download (Content-Disposition), the cache instructions and validators on
  your app's own reply (Cache-Control, ETag, Last-Modified), the
  elapsed-milliseconds offset a previous instrumented hop wrote, the
  queue-start timestamp a proxy adds, the presence (never the value) of a
  reverse proxy's forwarding headers (X-Forwarded-For, X-Forwarded-Host,
  X-Forwarded-Proto, X-Real-IP), a hosting platform's cache verdict, an AI
  provider's rate-limit headroom, retry delay and service-side processing
  time, and whether an authorization header or an authentication challenge is
  present — never its value, and never a header nobody named: the list is the
  claim, and a kit that starts reading a header outside it fails our own build
  before it ships; and the query values the kit reads on its way OUT are the
  ones in URLs the kit itself builds for its own uploads, read key by key so
  the guard can refuse anything identifier-shaped before it leaves your
  process; your app's own query strings are cut off before a route label is
  made and are never read. One bounded read is not a reduction at all, because
  what it holds is your own page on its way to your own visitor: where the
  dashboard bubble is switched on, an HTML page is buffered so the one script
  tag can be written into it — up to 2 MB, 4 MB on .NET, past which the page
  is passed through untouched. Those bytes are handed back with one script tag
  added and nothing taken from them. One read is neither narrowed to a label
  from a fixed list nor limited in length, because the value IS the question
  being asked of the kit's own endpoint: the kit's own local read endpoints —
  the panel and the JSON reads it serves under its own path, behind their
  loopback or key gate — read the query of requests made TO THEM: how many
  rows to return, and which route name to filter to; the row count becomes a
  number, the route name is compared, exactly as the caller wrote it and with
  no limit on its length, against the route names the kit is already holding,
  so a name matching none of them simply returns nothing; neither value is
  recorded, and no other query string in your app is read. Nothing checks it
  against a list first, no cap is put on how long it may be, and nothing is
  derived from it or kept once the reply has gone. One bounded read does not
  stay inside your process, and the difference is set out here rather than
  left to be inferred: on a request arriving at your app, the kits on your
  servers read two headers for their VALUE rather than for a label — the trace
  id and the caller's span id that another Boosthis-instrumented service of
  yours wrote on it. A value is taken only if it is exactly the shape the kits
  mint (32 hexadecimal characters for the trace, 16 for the span), and
  anything else is discarded, the trace id replaced with a fresh random one.
  An adopted id then IS that request's trace id: it is put on the hops your
  app makes while handling the request, and it is uploaded with that request's
  timings, which is the only way one action crossing several of your services
  can be shown as one line rather than several unrelated ones. It is the one
  thing read here that leaves your process, it is an identifier the kits mint
  and nothing is derived from it, and no other header's value is read. One
  read is neither bounded nor reduced to a number, and you ask for it
  yourself: one endpoint the Python kit's panel serves takes a POST — the rule
  matcher — and it reads that request's body in full, because the code you
  paste in IS the question being asked: it is matched against the rule book
  inside your own process, behind the same loopback or key gate as the rest of
  the panel, the answer is a list of rule names, and neither the code nor any
  part of it is stored, logged or uploaded. Apart from a trace id another
  instrumented service already put on the request, none of what is read here
  is stored, uploaded or recoverable from what is uploaded, and the guard on
  the upload path refuses outright any payload carrying a request or response
  body, a header, a cookie or a query value — and that id is uploaded as a
  field of its own, never as the header it arrived in.
- Raw stack traces, raw error messages, or log lines — a crash report records
  only a redacted code location (function + file basename + line) plus the
  error *type*, and, only if you opt an app into detailed crash mode, the first
  line of the message *and only if it passes the PII guard*; never a raw stack,
  never a value. A health meter that watches logged errors records only **how
  many** and how often — never the line, the message, the logger, or the level.
- Command lines, arguments, environments, or output of processes your app
  spawns — the subprocess meter records only how many spawns happened and how
  often
- The **names** of the things meters count — threads, workers, modules,
  handles, timers, files, pools or queues. A meter reports *how many* and *how
  long*, never *which*.
- Your source code. Boosthis will **never read your codebase** — no kit opens,
  scans, indexes or uploads a source file, and no source text is stored on our
  servers, including from a build map, whose embedded copy of your source is
  discarded the moment the map arrives and is never written down.
- Hostnames and environment variables are never sent. One environment variable
  is read inside your own process and does not leave it: the browser kit's
  development-posture check reads `NODE_ENV` to report whether a development
  build is running, as a 1 or a 0, never the value. File paths are never read
  from your machine either — with one exception, named here rather than further down the
  page: a build map a developer uploads keeps its own original source paths,
  for as long as that map is kept (see "Build maps you upload" above). Nothing
  else carries one: a crash frame's file is a basename, and an absolute path is
  cut back before it leaves the device.
- Screen contents, screenshots, video, audio

## How your connection and our pages are protected

<!-- These two paragraphs are generated from
     artifacts/api-server/src/lib/transportSecurityPosture.ts and checked
     against the headers the server actually sets. Edit that module, not this
     text. -->

Every connection to Boosthis is HTTPS. Our hosting edge sends a two-year
Strict-Transport-Security instruction covering this domain and its subdomains,
so a browser that has seen us once refuses to talk to us over plain HTTP again.
We deliberately do not send a second copy of that header ourselves: a reader
that receives two of them acts on only the first, so adding ours would change
nothing and would make the response look malformed to a scanner. We check every
day that the instruction is still arriving, and we are told if it stops.

Our pages carry a Content-Security-Policy that forbids any site from framing
them, which is what stops clickjacking. The wider policy that describes every
script, stylesheet and connection our pages legitimately use runs in
report-only mode: the browser does not block on it, it reports to us when a
page loads something outside it. It is report-only because our dashboards
render scripts and styles inline, and a blocking policy that permits inline
code would not be blocking anything. That is an honest limit: a script injected
inline into one of our pages would not be reported, while a script loaded from
anywhere we do not use would be.

## Defense in depth

The same PII denylist runs on the client AND on the server. A malicious or
out-of-date client cannot bypass the guard by renaming fields — both layers
reject the whole batch on the first hit, and the server logs the offending field
name (but not its value) for diagnostics. The PII guard runs **even when the
kill-switch (`BOOSTHIS_DISABLED=1`) is active** — disabling the runtime cannot be
used to smuggle PII past the chokepoint.

## How candidate rules become real rules

The submission is automatic, the promotion is not.

1. The runtime watches your local perf events. When the same privacy-safe
   signature recurs and your app is registered, the signature is uploaded
   automatically — no click required, no review queue on your side.
2. The server upserts on `(install, signature)`, so the same signature from the
   same install can never inflate the corpus. Aggregation happens across distinct
   installs.
3. A human maintainer reads aggregated signatures (never any one install's
   data) and decides whether they become a rule. The words themselves may be
   written by the Maintainer's own model, by the developer's own connected AI,
   or by the maintainer; what a maintainer approves may be the words as
   submitted, or the maintainer's own rewrite of them.
4. An approved rule reaches developers by one of two routes. It may be
   **hand-landed in the shipped rule book**, arriving in the next Boosthis
   release for everyone; or it may be **served as written, immediately, over
   the HTTP API and the MCP server**, with no kit release involved. Which
   route a rule takes is the maintainer's decision at the moment of approval.
   Either way the rule is **human-reviewed** before any developer can fetch
   it: it takes an explicit, per-proposal approval by the maintainer, and the
   wording has passed the same privacy and safety screen as everything else
   Boosthis stores or serves. A served rule may be re-worded or withdrawn at
   any time without notice (Part A, Section 9).
5. After a signature has been turned into a rule, the aggregated row
   stays on the server so it can keep informing rule-usage and trend
   analysis; it is not deleted when the rule ships. It is permanently
   de-identified if you erase your data (`forget`) — see Retention below.

We do **not** and will not auto-publish unreviewed rules. The shortlisting is
automatic; a human decides every rule that reaches a developer.

## Retention

- Full performance samples and cross-runtime traces: retained while your
  install participates in the program — deleted immediately by `forget`, and
  by the 60-day countdown after a revocation or suspension; spans are capped
  per trace so no trace grows without bound.
- Candidate rule signatures: kept while your install participates — one row
  per install and signature, and repeats just add to a counter; they are
  not deleted on a fixed schedule or when a rule ships. If you erase your data (`forget`) first, the signature is
  retained but permanently de-identified (see below).
- Crash reports: one row per recurring crash signature. If you erase your data
  (`forget`), the row is permanently de-identified — your install ID is replaced
  with a random anonymous key that cannot be traced back to you, and any opt-in
  detailed fields (message summary, stack frames) are removed — and the
  remaining code-derived signature is retained as part of the shared rule book
  (Part A, Section 7).
- Fix-resolution signals and learned rules follow the same rule on erasure:
  retained, permanently de-identified.
- Claim-check readings: one row per sentence Boosthis was asked to read, holding
  only how it was read and never the sentence. Pruned after 31 days. They carry
  no account, project or install, so erasing a project or closing an account
  leaves nothing in them to delete.
- Changes Boosthis witnessed: one row per aim a checked sentence resolved to —
  the subject, the measurement, the line and the judgement, never the sentence.
  There is no timed expiry: a row is kept while the project is on the service,
  and is deleted when you erase the project (forget) or close your account.
- Changes your assistant filed: one row per change an assistant said it had
  made on your project — the line it wrote and, where it gave one, the screen,
  endpoint or job it names. There is no timed expiry: a row is kept while the
  project is on the service, appears in your data export, and is deleted when
  you erase the project (forget) or close your account.
- Things an assistant said it would build: one row per name declared — the
  name as the app would register it, and nothing else. A name nothing has been
  heard about for 45 days stops counting as outstanding but is not deleted; a
  row is kept while the project is on the service and is deleted when you
  erase the project (forget) or close your account.

- Dashboard snapshots: only the **latest** snapshot per app is kept — each
  upload replaces the previous one; `forget` deletes it.
- The **register of your app's parts**: the route and screen names your
  project has been **observed** to have, pooled across your installs so that
  replacing an install does not restart a part's history. It is built only
  from readings that arrived — never from a route list your framework or
  navigator declares — and each entry is kept for **180 days** after that part
  was last seen, so a part that has gone quiet stays on the list, marked
  quiet, with the date it was last seen. `forget` on your project's last
  install deletes the register, and so does closing your account.
- **Which kits your project needs**: the list of Boosthis kit names your
  project was worked out to need — the kit names only, from our own published
  list. It arrives when you ask the Boosthis connection which kits this
  project needs, or when you deliberately send the hosted scanner's answer,
  and it is kept for **365 days** after it was last given so your project row
  can show the languages that are not reporting yet. No file paths, no
  manifest contents, no dependency names and no source code are read, sent or
  stored for it. Closing your account deletes it, and so does erasing the
  project.
- Aggregated rule-usage counts with no install ID attached may be retained
  longer for trend analysis.
- Short-lived operational rows (sessions, one-time codes, rate-limit
  counters, checkout working state) expire and are pruned automatically within
  hours to days.
- Routine backups of the operational database are kept for up to **30 days**
  and rotate out automatically — data you erase disappears from backups as
  they rotate. Two exceptions, both narrow. A particular backup is occasionally
  **held past that window** when it is the last remaining evidence in an open
  question about our own records; every hold is recorded with its reason and
  lifted once the question is answered. And a small, separate copy of the
  records we are **required by law to keep** — payment and receipt records —
  is kept for as long as that obligation lasts and does not rotate out with
  the rest; it contains nothing beyond those records. Backups are held in a
  private bucket on **Google Cloud**, in the **United States**.
- Where the service itself runs, and whether anything crosses a border, are
  two separate answers, both set out under "Where Boosthis runs, and whether
  your data crosses a border" above; the basis for processing outside the
  Kingdom is under "Your rights over your data" below.
- Account data (name, email, password hash, sessions, billing status,
  receipts): kept while your account is active. You can **close your account
  yourself** from **Dashboard → Delete account** or from the Boosthis phone
  app — no email to support required. Doing so needs your password and a typed
  confirmation, ends every other sign-in, cancels any live subscription, and
  is held for **24 hours** first: we email you a link to stop it, and until
  that window closes nothing has been deleted. Take your **data download**
  first if you want a copy (**Dashboard → Your data → Download**) — after
  deletion we cannot produce one. When it goes ahead we delete your projects
  and everything tied to them exactly as `forget` does, stop your project keys
  working, remove your sessions, password and payment card details, and strip
  your name and email from the account record. The only things kept are one-way
  fingerprints of the email address, and of any card used for a trial started
  before 19 September 2026, which cannot be turned back into an address, a card
  or a person — they exist solely so the **one free trial per Account and per
  confirmed email address** limit above still holds after an account is closed.
  Records we must keep for tax
  and accounting purposes (payment and receipt records, and the billing name
  and address on them where a payment was actually made) are retained for as
  long as the law requires, even after an account is closed. As with `forget`,
  learning signals already contributed survive only in permanently
  de-identified form (Part A, Section 7).
- Workspace activity-log entries are pruned after a year; a support message is
  deleted **365 days** after your request is closed, and while a request is
  still open we keep it until it is closed, because we cannot answer you
  otherwise; website security reports are deleted after 90 days.
- If your access is revoked or suspended, the associated stored data is
  deleted after the **60-day countdown** described in Part A, Section 14
  (restoring access cancels it; `forget` is immediate).

## How to stop reporting / delete your data

**Stop the optional full samples** — stops per-route samples but keeps the
always-on issue & fix signals (and your install ID):

```bash
boosthis telemetry disable
```

or in code: `boosthis.disable_telemetry()` / `disableTelemetry()`.

**Stop everything immediately** — the emergency kill-switch silences the entire
runtime, issue & fix signals included, with no code change:

```bash
BOOSTHIS_DISABLED=1
```

Set it in your app's environment. The PII guard still runs first, so the
kill-switch can never be used to smuggle data past the chokepoint, and it always
wins over every other setting.

**Delete everything** — wipe your install ID and all data we hold:

```bash
boosthis telemetry forget
```

This calls `POST /api/installs/forget` with your install ID; the server deletes
the install row and every sample, snapshot, trace, and alert associated with
it, and permanently de-identifies the aggregated learning signals (candidate
signatures, fix-resolutions, crash signatures with detailed fields removed,
and learned rules) by replacing your install ID with a random anonymous key —
after erasure, nothing we hold can be linked to you, your app, or your install.
Then we delete the local config file. Irreversible. (As stated in Part A,
Section 7, the assignment of Telemetry survives erasure: de-identified learning
signals and rules already derived are retained.)

## Your rights over your data

Everything you can ask us to do is listed here. Two of these need nobody's
permission — they are buttons — and the rest are one message away.

- **Get a copy of what we hold about you.** **Dashboard → Your data →
  Download**, at any time, without asking anyone. It is a machine-readable
  export of your account and your projects' data. Take it before you close your
  account: after deletion we cannot produce one.
- **Have it corrected.** Your account-holder name and email address are
  editable from your dashboard. For anything else that is wrong, write to us
  and we will correct it.
- **Have it deleted.** **Dashboard → Delete account** (or the same option in
  the Boosthis phone app) closes your account and deletes your data as
  described under "Retention"; `boosthis telemetry forget` erases an install's
  telemetry immediately, independently of any account. Neither needs an email
  to support.
- **Object to a use, or ask us to stop one.** Telemetry can be stopped from
  inside your own app at any time — `disableTelemetry()` for the optional full
  details, `BOOSTHIS_DISABLED=1` for everything. For any other use described in
  this document, write to us and name it; we will stop it or explain why we
  cannot, and where we cannot you can delete the data instead.
- **Withdraw a consent you gave.** Consent to this document is asked for again
  at each new major version, so withdrawing it means declining the new version.
  The levers above remain available either way.
- **Complain.** If our answer does not satisfy you, you can complain to the
  **Saudi Data & AI Authority (SDAIA)**, the supervisory authority for personal
  data in the Kingdom of Saudi Arabia, or to the data-protection authority of
  the country you live in.

**How to ask, and how long we take.** Email **support@boosthis.com**, or write
to the postal address under "Contact" below. We answer within **30 days** of
receiving a request. If we need something to identify you — normally only that
you write from the email address on the account — we will ask for it, and the
30 days run from when we have it. There is no charge. The self-serve levers
above are immediate and do not go through this route at all.

**Processing outside the Kingdom.** Boosthis is operated from the Kingdom of
Saudi Arabia. As "Where Boosthis runs, and whether your data crosses a border"
above sets out, some of the companies that run it hold what reaches them
outside the Kingdom. That processing is **necessary in order to provide the
hosted service you asked for** — there is no version of the hosted dashboard,
email or assistant that does without them — and by creating an account or
registering a project you agree to it. Each company receives only what is
listed beside its name and may use it only for the purpose named there, and for
each one we keep a written record of the ground relied on and the safeguards
that apply. If you would rather nothing crossed a border, a **general install**
of the toolkit sends nothing anywhere.

## Children

Boosthis is a developer tool. It is not intended for users under 16, and we do
not knowingly collect data from anyone under 16. Holding an account requires
you to be at least 16; buying a paid plan requires you to be at least 18, or
older where the age of majority where you live is higher. Section 1 of Part A
says why those two ages are different questions with different answers.

**Children using your application.** Boosthis measures your application while
real people are using it, and those people are not our users. We have no
relationship with them, cannot see who they are, and never receive anything
that identifies them. Where your application is directed at children, or you
know that children use it, the notice and consent that requires is yours to
obtain and not ours — **Section 28** of Part A sets out what that means, what
you must not send us, and what we do when you tell us an application is
child-directed. You can tell us by emailing **support@boosthis.com** with
"child-directed" in the subject and naming the project.

## Changes

If we change this policy, we will bump Boosthis's version and note it in the
release notes. Your existing consent only applies to the version you opted into;
new major versions ask again. A change to the list of companies under "The
companies that can see your data" is announced there in advance, as that section
describes.

## Contact

Deleting your account and getting a copy of what we hold about you are both
**self-serve** — you do not need to email anyone. Use **Dashboard → Delete
account** (also in the Boosthis phone app) and **Dashboard → Your data →
Download**. Everything else you can ask for is under "Your rights over your
data" above.

For any other terms or privacy question, email **support@boosthis.com**. If you
email support, we keep your message (address, subject, and text) so we can
reply and keep a record of the request; support mail is never joined to
telemetry. Once your request is closed we keep it for **365 days** and then
delete it automatically, and while a request is still open we keep it until it
is closed.

By post: **Veqtara Tech Company** (trading as Boosthis), commercial
registration 7054832287, Rabwa District, Al Noaim Street, Riyadh, Kingdom of
Saudi Arabia.
