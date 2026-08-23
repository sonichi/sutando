/**
 * Scheme normalization for URLs handed to Chrome via AppleScript.
 *
 * Chrome's `make new tab with properties {URL:...}` needs an absolute URL and
 * rejects a bare host with "Invalid URL entered. (5)". Only the omnibox infers
 * a scheme, which is why the same address works once it is typed there.
 */

// A scheme name may be dotted (`com.example:123`), so only an IP literal —
// which cannot start one — plus `localhost` are inferable authorities.
const HOST_PORT =
	/^(?:localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:]+\]):\d+(?:[/?#]|$)/i;
const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/** Return `url` unchanged if it already carries a scheme, else prefix https://. */
export function withScheme(url: string): string {
	if (HOST_PORT.test(url)) return `https://${url}`;
	if (SCHEME.test(url)) return url;
	return `https://${url}`;
}
