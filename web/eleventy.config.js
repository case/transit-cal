import { existsSync } from "node:fs";

// Written by transit-cal site-data, which bin/build-site and bin/run-site run first
const CATALOG = new URL("./source/_data/catalog.json", import.meta.url);

export default function(eleventyConfig) {
  eleventyConfig.on("eleventy.before", () => {
    if (!existsSync(CATALOG)) {
      throw new Error("source/_data/catalog.json is missing. Run bin/build-site or bin/run-site.");
    }
  });
  // Rendered at build time, so pages need no script and the CSP can forbid them
  eleventyConfig.addShortcode("year", () => String(new Date().getFullYear()));
  // A feed's full URL, from its catalog path (no leading slash) and the site origin
  eleventyConfig.addFilter("feedUrl", (path, origin) => new URL(`/${path}`, origin).href);
  // The URL API refuses to switch http(s) to webcal, so swap the scheme in the string
  eleventyConfig.addFilter("webcal", (url) => url.replace(/^https?:/, "webcal:"));
  eleventyConfig.addPassthroughCopy({
    "node_modules/@picocss/pico/css/pico.min.css": "css/pico.min.css",
  });
}

export const config = {
  dir: {
    input: "source",
  },
};
