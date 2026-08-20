/**
 * Scheme normalization for URLs handed to Chrome via AppleScript.
 *
 * Chrome's `make new tab with properties {URL:...}` needs an absolute URL and
 * rejects a bare host with "Invalid URL entered. (5)". Only the omnibox infers
 * a scheme, which is why the same address works once it is typed there.
 */

// `tel:911` and `localhost:3000` are the same shape, and being dotted does not
// separate them either: a reverse-DNS scheme (`com.example:123`) is dotted, and
// that is the ordinary shape of an app deep link. So infer an authority only
// where the input CANNOT be a scheme — an IP literal, which cannot start a
// scheme name — plus `localhost`, named because it is the one bare host that
// also parses as one. A dotted `host:port` stays ambiguous and keeps its input
// form; qualifying it needs an explicit scheme. Preservation wins ties, because
// rewriting a deep link sends it to the wrong origin.
const HOST_PORT =
	/^(?:localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-f:]+\]):\d+(?:[/?#]|$)/i;
const SCHEME = /^[a-z][a-z0-9+.-]*:/i;

/** Return `url` unchanged if it already carries a scheme, else prefix https://. */
export function withScheme(url: string): string {
	if (HOST_PORT.test(url)) return `https://${url}`;
	if (SCHEME.test(url)) return url;
	return `https://${url}`;
}
