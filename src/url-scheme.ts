/**
 * Scheme normalization for URLs handed to Chrome via AppleScript.
 *
 * Chrome's `make new tab with properties {URL:...}` needs an absolute URL and
 * rejects a bare host with "Invalid URL entered. (5)". Only the omnibox infers
 * a scheme, which is why the same address works once it is typed there.
 */

// `host:port` is not a scheme: a real scheme is followed by "//" or by opaque
// content, never by a port number.
const HIERARCHICAL = /^[a-z][a-z0-9+.-]*:\/\//i;
const OPAQUE = /^[a-z][a-z0-9+.-]*:[^/\d]/i;

/** Return `url` unchanged if it already carries a scheme, else prefix https://. */
export function withScheme(url: string): string {
	if (HIERARCHICAL.test(url) || OPAQUE.test(url)) return url;
	return `https://${url}`;
}
