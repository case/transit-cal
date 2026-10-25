// The public origin every feed link is built from; bin/run-site sets SITE_URL to its own address
const value = process.env.SITE_URL || "https://transit-cal.vedana.net";
const url = new URL(value);
if (!["http:", "https:"].includes(url.protocol) || url.href !== `${url.origin}/`) {
  throw new Error(`SITE_URL must be a bare http(s) origin such as https://example.net, not ${value}`);
}

export default { url: url.origin };
