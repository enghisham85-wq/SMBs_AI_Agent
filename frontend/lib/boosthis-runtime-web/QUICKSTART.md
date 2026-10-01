# Boosthis on a page with NO build step

**Read this first if your project has no bundler** — a single `index.html`, a
page an AI wrote for you in a chat, or a hosted site builder (Webflow, Framer,
Squarespace, Wix, Shopify, WordPress, Carrd) whose only extension point is a
"custom code / header" box.

The TypeScript files beside this one are the module install: they have to be
compiled by a bundler, and `@workspace/boosthis-runtime-web` is this folder's
own package name, not a package on any registry. If you have no bundler, you do
not need one, and you do not need these files at all. The **same runtime** is
served pre-built at one fixed address.

## The whole install

Paste this into every page's `<head>`:

```html
<script defer src="https://www.boosthis.com/kit.js" data-key="<the project key>" data-install="<a UUID v4 you generate ONCE>" data-project="<friendly name>" onerror="console.error('[boosthis] Boosthis did not load: '+this.src+' never arrived, so nothing is measuring. Check the address, the network, and any Content-Security-Policy.')"></script>
```

Fill in the three values:

- `data-key` — your project key, from your Boosthis dashboard. Without it the
  kit runs and measures in the browser but never reports anything.
- `data-install` — a UUID v4 you generate **once** (`uuidgen`, or
  `crypto.randomUUID()` in any browser console) and then reuse forever. It
  identifies the **project**, not the visitor. A new value does not repair a
  connection: it registers a second install alongside the real one.
- `data-project` — a friendly name. Use the **same** name on every page; that
  name is what groups the whole site into one project.

## The address

`https://www.boosthis.com/kit.js` is the only address there is. There is no CDN copy
under any other name and no per-account address. An address that looks
plausible but was not read from this file or from the install guide does not
resolve, and a page pointed at one measures nothing at all while looking
perfectly installed.

## Keep the `onerror` handler

It is the only piece of Boosthis that lives in your own page, so it is the only
thing that can speak if the Boosthis file never arrives. Without it, a blocked
or mistyped address looks exactly like a page with nothing installed.

## What a working install looks like

Open the page and look at the browser console:

- One line, `[boosthis] Boosthis starting: …`, the moment the kit runs. **No
  line means the script never loaded** — check the address, then the network,
  then any Content-Security-Policy, and read whatever the `onerror` handler
  printed.
- A small Boosthis badge appears in the corner straight away, before the key is
  read and before registration is attempted. A badge means the kit is running;
  read the badge for what happened next.
- The site then appears under **Your Projects** on your Boosthis dashboard. A
  brand-new install can take a few seconds to be confirmed, and the kit says on
  the console how that wait ended.

## If your page sends a Content-Security-Policy

Only then — most plain pages send none. Add the Boosthis host to both
`script-src` and `connect-src`, or the browser blocks the tag and the console
shows a "Refused to load/connect" line.

## Two optional attributes

- `data-bubble="off"` hides the badge.
- `data-private="on"` reports issues and crashes only, with no per-page
  timings.

## A preview pane is not a fair test

Some AI chat preview panes cut off outside network access entirely, so the page
can look silent there while the published page reports perfectly. Check the
page where it actually runs, and settle it from your Boosthis dashboard rather
than from the preview.

## If you DO have a bundler

Ignore all of the above and see `README.md` beside this file — the module
install measures the same things and is configured in code instead of in
attributes.
