// Lets a skill loaded from outside the engine tree import the engine's dependencies, as a
// shipped skill does: a bare import that fails from an out-of-tree skill retries from the engine.
import * as nodeModule from 'node:module';
import { realpathSync } from 'node:fs';
import { join, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

type ResolveCtx = { conditions?: string[]; parentURL?: string };
type ResolveResult = { url: string; shortCircuit?: boolean; format?: string | null };
type NextResolve = (specifier: string, ctx?: ResolveCtx) => ResolveResult;
type RegisterHooks = (hooks: { resolve: (s: string, c: ResolveCtx, n: NextResolve) => ResolveResult }) => unknown;

const roots = new Set<string>();
let installed = false;

function isBare(specifier: string): boolean {
	return !/^(\.{0,2}\/|file:|node:|data:|[A-Za-z]:[\\/])/.test(specifier) && specifier !== '.' && specifier !== '..';
}

function insideRoot(parentURL: string | undefined): boolean {
	if (!parentURL?.startsWith('file:')) return false;
	let parent: string;
	try { parent = fileURLToPath(parentURL); } catch { return false; }
	for (const root of roots) if (parent === root || parent.startsWith(root + sep)) return true;
	return false;
}

/** Resolve `specifier` as the engine would, or null. Pure: exported for tests. */
export function engineFallback(repoRoot: string, specifier: string, ctx: ResolveCtx, next: NextResolve): ResolveResult | null {
	if (!isBare(specifier) || !insideRoot(ctx.parentURL)) return null;
	const anchor = pathToFileURL(join(repoRoot, 'package.json')).href;
	// The default resolver ignores parentURL for a require(), so resolve that one here.
	if (ctx.conditions?.includes('require')) {
		return { url: pathToFileURL(nodeModule.createRequire(anchor).resolve(specifier)).href, shortCircuit: true };
	}
	return next(specifier, { ...ctx, parentURL: anchor });
}

/**
 * Mark `skillDir` as an out-of-tree skill root. Installs the hook on first use;
 * returns false when this Node has no in-thread module hooks (< 22.15) or installing them fails.
 */
export function allowEngineDependencies(
	repoRoot: string,
	skillDir: string,
	host: { registerHooks?: RegisterHooks } = nodeModule as unknown as { registerHooks?: RegisterHooks },
): boolean {
	const register = host.registerHooks;
	if (typeof register !== 'function') return false;
	try { roots.add(realpathSync(skillDir)); } catch { return false; }
	if (!installed) {
		try {
			register({
				resolve(specifier, ctx, next) {
					try {
						return next(specifier, ctx);
					} catch (err) {
						let fallback: ResolveResult | null = null;
						try { fallback = engineFallback(repoRoot, specifier, ctx, next); } catch { /* report the original */ }
						if (fallback) return fallback;
						throw err;
					}
				},
			});
		} catch {
			return false;
		}
		installed = true;
	}
	return true;
}
