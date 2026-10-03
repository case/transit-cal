---
title: Architecture
summary: Scheduled build: GTFS in, static .ics feeds and website out
created: 2026-10-03
author: Eric Case
tags: [architecture, stack, codebase, conventions]
---

# Architecture

The current implementation of transit-cal. Replace any of this and the [product](product.md) is unaffected. The product is "above the user experience abstraction layer," and the architecture is "below the abstraction layer."

## Repo layout

<Directory tree - top-level dirs and what they hold.>

## Stack

| Layer   | Choice                                 |
| ------- | -------------------------------------- |
| Data    | 511.org regional GTFS, via Transitland |
| Build   | Python                                 |
| Output  | `.ics` feeds + static website          |
| Hosting | Static files. Host not chosen.         |

## Vendor decisions

- Transitland: feed registry, Onestop IDs, GTFS download.
- No dynamic server. Static files only.

## Conventions

<How we do things: version pinning, linting, testing, git hooks, CI runner, deploy flow.>
