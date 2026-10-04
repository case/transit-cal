---
title: Architecture
summary: A scheduled build turns GTFS into static .ics feeds and a website
created: 2026-10-03
author: Eric Case
tags: [architecture, stack, codebase, conventions]
---

# Architecture

The current implementation of transit-cal. Replace any of this and the [product](product.md) is unaffected. The product is "above the user experience abstraction layer," and the architecture is "below the abstraction layer."

## Repo layout

```
bin/        setup, lint, build-site, run-site
docs/       memory and plans
web/        11ty site: config, package.json, source/
Caddyfile   production web server
```

## Stack

| Layer   | Choice                                 |
| ------- | -------------------------------------- |
| Data    | 511.org regional GTFS, via Transitland |
| Build   | Python (feeds), 11ty in `web/` (site)  |
| Output  | `.ics` feeds + static website          |
| Hosting | Railway: Caddy container               |
| O11y    | Caddy JSON access logs                 |

## Vendor decisions

- Transitland: feed registry, Onestop IDs, GTFS download.
- No dynamic server. Static files only.

## Conventions

<How we do things: version pinning, linting, testing, git hooks, CI runner, deploy flow.>
