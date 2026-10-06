# transit-cal

Transit schedules → Calendar feeds

## Related

- [Transical](https://github.com/solanto/transical) - "transical converts publically-available [GTFS](https://gtfs.org/) transit schedules into calendar events, bundled into [iCalendar](https://en.wikipedia.org/wiki/ICalendar) (`.ics`) files. By [Andrew Sòlanto](https://dandelion.computer/posts/transical)

## Future

- Install mise in `bin/setup` with [packslip](https://mise.jdx.dev/installing-mise.html), which verifies mise's Sigstore signature, instead of piping `https://mise.run` into `sh`.
- Consider precompressed feeds or edge rate limiting if traffic grows.
