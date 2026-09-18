/**
 * Request shapes a tool declares as its own, so the work bridge refuses them and hands the
 * model the tool that owns the shape instead of spawning a background task for it.
 *
 * Core names no skill here. A claim arrives as data from a loaded skill's manifest, and the
 * registry only ever holds claims whose tool actually loaded on this host — so a host without
 * that skill keeps the unclaimed behaviour with nothing to configure.
 */

export type ToolClaim = {
	/** The loaded tool that owns the shape. Named back to the model verbatim. */
	tool: string;
	/** Pattern the task must match, case-insensitive, for the claim to apply. */
	match: string;
	/** Pattern that withdraws the claim — typically work ON the tool's own subject. */
	unless?: string;
	/** What the model is told in place of the refusal. */
	message: string;
};

type CompiledClaim = ToolClaim & { _match: RegExp; _unless?: RegExp };

let registry: CompiledClaim[] = [];

/** A manifest is user-supplied data: one unusable entry is dropped, never thrown. */
function compile(claim: ToolClaim, warn: (msg: string) => void): CompiledClaim | null {
	try {
		return {
			...claim,
			_match: new RegExp(claim.match, 'i'),
			_unless: claim.unless ? new RegExp(claim.unless, 'i') : undefined,
		};
	} catch (err) {
		warn(`[tool-claims] ${claim.tool}: unusable pattern, claim ignored: ${err instanceof Error ? err.message : err}`);
		return null;
	}
}

/**
 * Validate raw `claims` from a manifest, keeping only well-formed entries whose tool is in
 * `loadedToolNames`. A claim for a tool that did not load would refuse a task and name
 * something the model cannot call.
 */
export function parseToolClaims(
	raw: unknown,
	loadedToolNames: Iterable<string>,
	warn: (msg: string) => void = (m) => console.warn(m),
): ToolClaim[] {
	if (!Array.isArray(raw)) return [];
	const loaded = new Set(loadedToolNames);
	const out: ToolClaim[] = [];
	for (const entry of raw) {
		if (!entry || typeof entry !== 'object') continue;
		const { tool, match, unless, message } = entry as Record<string, unknown>;
		if (typeof tool !== 'string' || typeof match !== 'string' || typeof message !== 'string'
			|| !tool || !match || !message) {
			warn('[tool-claims] claim needs string tool, match and message — ignored');
			continue;
		}
		if (unless !== undefined && typeof unless !== 'string') {
			warn(`[tool-claims] ${tool}: unless must be a string — claim ignored`);
			continue;
		}
		if (!loaded.has(tool)) {
			warn(`[tool-claims] ${tool}: claimed a request shape but did not load — claim ignored`);
			continue;
		}
		out.push(unless === undefined ? { tool, match, message } : { tool, match, unless, message });
	}
	return out;
}

/** Add claims to the process-wide registry the work bridge consults. */
export function registerToolClaims(
	claims: readonly ToolClaim[],
	warn: (msg: string) => void = (m) => console.warn(m),
): void {
	for (const claim of claims) {
		const compiled = compile(claim, warn);
		if (compiled) registry.push(compiled);
	}
}

/** The registry, for diagnostics. */
export function registeredToolClaims(): ToolClaim[] {
	return registry.map(({ _match, _unless, ...claim }) => claim);
}

/** Empty the registry. For tests and for a reload. */
export function clearToolClaims(): void {
	registry = [];
}

/**
 * The claim that owns this task, or null. Pass `claims` to evaluate a table directly — the
 * registry is the default so callers need no wiring.
 */
export function claimFor(
	task: string,
	claims?: readonly ToolClaim[],
	warn: (msg: string) => void = (m) => console.warn(m),
): ToolClaim | null {
	const table: CompiledClaim[] = claims === undefined
		? registry
		: claims.map((c) => compile(c, warn)).filter((c): c is CompiledClaim => c !== null);
	for (const claim of table) {
		if (claim._match.test(task) && !(claim._unless?.test(task))) {
			const { _match, _unless, ...plain } = claim;
			return plain;
		}
	}
	return null;
}
