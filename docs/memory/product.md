---
title: Product
summary: Transit schedules → calendar feeds.
created: 2026-10-03
author: Eric Case
tags: [product, scope, goals]
---

# Product

Transit schedules → calendar feeds.

## Why this exists

You can see transit schedules next to everything else on your calendar. Subscribe to a route, toggle it on to plan a trip, then toggle it off until you need it again.

## Who it's for

- Anyone with a calendar app.
- First operators: SF Bay Ferry, Golden Gate Ferry.

## What it's not

- Not realtime. Published schedules only.
- Not authoritative, though we'll do our best to confirm that the data is current. Transit operators remain the source of truth.

## Implementation independence

The product is durable across two kinds of change: **how it's built** (stack, codebase) and **where it runs** (hosting, infrastructure, operational setup). Either can change without altering what the product _is_. See [`architecture.md`](architecture.md) for the current implementation.

The product is "above the user experience abstraction layer," and the architecture is "below the abstraction layer."

For this project, that means the iCal feeds are the "product" - and how they are generated could change in the future.
