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
bin/        setup, lint, test, ci; build-site, run-site, build-image, test-image, test-feeds;
            build-feeds and run-image run the production image locally against out/feeds
docs/       memory and plans
src/        transit_cal Python package: GTFS to .ics feeds, operator catalog
tests/      pytest suite; fixtures/synthetic/ is an invented GTFS feed and catalog
tests-image/ pytest suite for the production image, run by bin/test-image and never by bin/test
tools/      in-image scripts (entrypoint, build-feeds, ics-validate) and the ical4j validator source and jar lock
web/        11ty site: config, package.json, source/
Caddyfile   production web server
Dockerfile  production image: site, Caddy, Python package, JRE and validator
```

## Stack

| Layer   | Choice                                 |
| ------- | -------------------------------------- |
| Data    | 511.org regional GTFS, via Transitland |
| Build   | Python (feeds), 11ty in `web/` (site)  |
| Output  | `.ics` feeds + static website          |
| Hosting | Railway: one container and a volume    |
| O11y    | Caddy JSON access logs                 |

## Vendor decisions

- Transitland: feed registry, Onestop IDs, GTFS download.
- No dynamic server. Static files only.
- ical4j validates feeds against RFC 5545 in `bin/test-feeds`. It is an independent implementation; `icalendar` parses too leniently to validate. It runs in strict mode, which reports every rule violation as an error.
- The production image carries ical4j, so CI and production run one validator. The image build fetches the jars in `tools/ics-validate.lock` by SHA-256 and compiles `IcsValidate.java` in a JDK stage; the final stage has only the JRE. `bin/test-feeds` runs it offline (`--network none`), so it runs from `bin/ci`, never `bin/test`.
- The image is Temurin's JRE on a digest-pinned Alpine, with Caddy's static binary copied in. Temurin is the vendor-neutral OpenJDK build. The full JRE (~190 MB unpacked) is deliberate: validation runs briefly once a night, so a `jlink` runtime would save only disk while adding module-inference risk.
- s6 (Alpine's `s6`) supervises the container. The root entrypoint hands a fresh `/feeds` volume to the builder (UID 10002), moves `TRANSITLAND_API_KEY` into a root-only envdir under `/tmp`, unsets it, and execs `s6-svscan`. It runs two services from `tools/s6`: Caddy as UID 10001 and `transit-cal-schedule` as UID 10002, the only process given the key. s6 restarts either if it dies; a crashed scheduler waits 60 s first. Its writable service dirs are a root-only copy on `/tmp` whose scripts link back to `/etc/s6`, since `/tmp` may be noexec.
- Caddy has `admin off` and `persist_config off`, and its data dir is on `/tmp`; `bin/test-image` starts the image with a read-only root and fails on any logged Caddy warning or error.
- The scheduler runs `build-feeds` at 05:00, 11:00, 17:00 and 23:00 UTC, plus at startup when `/feeds/current` is missing or at least 12 hours old. A failed run waits for the next slot, and a run past 30 minutes is stopped. It imports only the standard library and `publish`, so it idles at ~10 MB. SIGTERM stops a running build's whole process group, then the scheduler.
- `transit-cal fetch` asks Transitland's feed record for the latest version's SHA-1 and reuses a matching zip on the volume, so only the first run after a new version downloads the 59 MB file. The 511 feed gets a new version almost daily.
- Caddy serves only paths shaped like `/<onestop_id>/<route>-<direction>.ics` from `/feeds/current`; everything else comes from `/srv`. Releases, staging and GTFS zips on the volume are never served.
- `transit-cal fetch` refuses a download over 512 MiB, follows at most 3 redirects, each to HTTPS, and never reads a redirect's body; the key never follows a redirect. Redirect targets are not checked for private addresses: that would only matter if Transitland itself were compromised. The GTFS loader refuses a zip whose parsed files unpack past 2 GiB, and `zipfile` never reads past a member's declared size.
- `publish` accepts only directories and regular `.ics` files, never a symlink, and refuses a stage missing any feed path the served release has, so a subscribed URL never starts returning 404. Releases are read-only (files 0444, directories 0555) once published; `prune` makes a release writable again only to delete it.
- Caddy sends HSTS for one year and a CSP of `default-src 'none'` plus same-origin styles and images. The site has no scripts: the footer year is rendered at build time.
- Containment: only `s6-svscan` and its supervisors run as root, and the image has no setuid or setgid files (`tests-image` checks). Caddy (UID 10001) can read `/srv` and `/feeds`, write `/tmp` and reach the network; the builder (UID 10002) can also write the volume. Removing tools such as `wget` or `nc` would gain little, since `python3` and the JRE must stay and can do the same. Accepted risk: the scheduler holds the Transitland key under the same UID that parses GTFS and runs ical4j, so code execution through a parser bug could read it; it is a free-tier key with no billing attached.
- Python is Alpine's `python3` 3.14, pinned by major version in the digest-pinned base: Alpine removes superseded package revisions, so exact pins break rebuilds. uv builds the virtualenv in a separate stage and is not in the final image.

## Conventions

- Transit terms follow GTFS. A **leg** is one ride on one vehicle, boarding to alighting; it can span an in-seat transfer. A **block** is one vehicle's sequence of trips. A **service day** is GTFS's. Rider-facing text says "departures". Code and comments use no mode-specific words such as "sailing" or "vessel". Sources: [GTFS reference](https://gtfs.org/documentation/schedule/reference/), transitland-lib's `Itinerary` and `Leg`.
- Feeds ask clients to poll at most daily (`REFRESH-INTERVAL` and `X-PUBLISHED-TTL` of `P1D`). Upstream operator schedules change rarely, and RFC 7986 makes the value a minimum, not a schedule.

<How we do things: version pinning, linting, testing, git hooks, CI runner, deploy flow.>
