/**
 * Scheme normalization for URLs handed to Chrome via AppleScript.
 *
 * Chrome's `make new tab with properties {URL:...}` needs an absolute URL and
 * rejects a bare host with "Invalid URL entered. (5)". Only the omnibox infers
 * a scheme, which is why the same address works once it is typed there.
 */

// `tel:911` and `localhost:3000` are the same shape, so the colon cannot
// classify them. The host does: an authority is dotted or literally localhost,
// while a scheme name is neither. Matching host:port positively keeps the
// scheme rule open-ended, so an unlisted scheme is never rewritten.
const HOST_PORT = /^(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+):\d+(?:[/?#]|$)/i;
const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/** Return `url` unchanged if it already carries a scheme, else prefix https://. */
export function withScheme(url: string): string {
	if (HOST_PORT.test(url)) return `https://${url}`;
	if (SCHEME.test(url)) return url;
	return `https://${url}`;
}
