export default function(eleventyConfig) {
  // Rendered at build time, so pages need no script and the CSP can forbid them
  eleventyConfig.addShortcode("year", () => String(new Date().getFullYear()));
  eleventyConfig.addPassthroughCopy({
    "node_modules/@picocss/pico/css/pico.min.css": "css/pico.min.css",
  });
}

export const config = {
  dir: {
    input: "source",
  },
};
